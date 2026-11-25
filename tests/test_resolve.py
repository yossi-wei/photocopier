"""Closing out triage: `photocopier triage` and `photocopier resolve`.

Before these existed, the only way out of triage was filing by hand and then an UPDATE
in SQLite. The scheduled run flags any triaged item as a problem, so an item nobody can
close out keeps the alert email firing forever.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from photocopier.cli import main
from photocopier.errors import GuardError, LedgerError
from photocopier.flush import flush
from photocopier.ingest import ingest
from photocopier.layout import file_digest, variant_dest
from photocopier.ledger import Ledger, State
from photocopier.process import process
from photocopier.rclone import Rclone
from photocopier.resolve import (
    filed_location,
    pending,
    render_pending,
    resolve_discard,
    resolve_filed,
    resolve_keep_both,
)
from photocopier.spool import Spool
from photocopier.triage import DEST_CONFLICT, NO_DATE, Triage, conflict_detail, conflict_target

from .conftest import FAKE_RCLONE, FIXTURES, make_config, populate
from .test_flush import SimulatedSigkill, kill_on_call

CAMERA_ROLL = "Camera Roll"
UNDATED = "no_date_anywhere/1J1A3188.jpg"


@pytest.fixture
def nas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A mounted share stand-in; see the fixture of the same name in test_flush."""
    root = tmp_path / "nas"
    (root / "photos").mkdir(parents=True)
    (root / "video").mkdir(parents=True)
    (root / ".photocopier-marker").touch()

    real_ismount = os.path.ismount
    monkeypatch.setattr(
        os.path, "ismount", lambda p: True if Path(p) == root else real_ismount(p)
    )
    return root


@pytest.fixture
def wired(spool_root: Path, nas: Path, remote_root: Path):
    config = make_config(spool_root, nas=nas)
    spool = Spool.from_config(config.spool)
    spool.ensure()
    return config, spool, nas, remote_root


def pump(config, spool: Spool) -> None:
    with Ledger(spool.ledger_path) as ledger:
        ingest(config, Rclone(config.rclone), ledger, spool)
        process(config, ledger, spool)


def triage_undated(config, spool: Spool, remote: Path) -> Path:
    """Put one undated photo through the pipeline. Returns the triaged copy."""
    target = remote / CAMERA_ROLL / UNDATED
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / "1J1A3188.jpg", target)
    pump(config, spool)
    copy = spool.triage / NO_DATE / "phone-1" / UNDATED
    assert copy.is_file()
    return copy


def only_triaged(spool: Spool):
    with Ledger(spool.ledger_path) as ledger:
        [item] = ledger.items_in_state(State.TRIAGED)
        return item


def row(spool: Spool, item_id: int) -> dict:
    with Ledger(spool.ledger_path) as ledger:
        found = ledger.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(found)


def file_by_hand(copy: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(copy, target)
    return target


class TestList:
    def test_shows_id_reason_path_and_detail(self, wired) -> None:
        config, spool, _nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)

        with Ledger(spool.ledger_path) as ledger:
            items = pending(ledger, Triage(spool.triage))
        output = render_pending(items, spool.triage)

        assert f"#{item.id}  {NO_DATE}  phone-1  {UNDATED}" in output
        assert str(copy) in output
        assert "no date in EXIF" in output
        assert "(missing)" not in output

    def test_empty(self, wired) -> None:
        _config, spool, _nas, _remote = wired
        with Ledger(spool.ledger_path) as ledger:
            assert render_pending(pending(ledger, Triage(spool.triage)), spool.triage) == (
                "nothing in triage"
            )

    def test_kill_while_triaging_still_lists_the_item(self, wired, monkeypatch) -> None:
        config, spool, _nas, remote = wired
        populate(remote / CAMERA_ROLL, {"undated/1J1A3188.jpg": 2048})
        original = (remote / CAMERA_ROLL / "undated/1J1A3188.jpg").read_bytes()

        kill_on_call(monkeypatch, Ledger, "mark_triaged")
        with pytest.raises(SimulatedSigkill):
            pump(config, spool)
        for _ in range(2):
            pump(config, spool)

        with Ledger(spool.ledger_path) as ledger:
            listed = [p for p in pending(ledger, Triage(spool.triage)) if p.present]
        assert [p.item.src_path for p in listed] == ["undated/1J1A3188.jpg"]
        assert listed[0].path.read_bytes() == original
        assert "no date in EXIF" in render_pending(listed, spool.triage)

    def test_kill_while_triaging_lists_the_item_before_another_pass(
        self, wired, monkeypatch
    ) -> None:
        config, spool, _nas, remote = wired
        populate(remote / CAMERA_ROLL, {"undated/1J1A3188.jpg": 2048})
        original = (remote / CAMERA_ROLL / "undated/1J1A3188.jpg").read_bytes()

        kill_on_call(monkeypatch, Ledger, "mark_triaged")
        with pytest.raises(SimulatedSigkill):
            pump(config, spool)

        with Ledger(spool.ledger_path) as ledger:
            listed = pending(ledger, Triage(spool.triage))
        assert [(p.item.src_path, p.present) for p in listed] == [("undated/1J1A3188.jpg", True)]
        assert listed[0].path.read_bytes() == original
        assert "no date in EXIF" in render_pending(listed, spool.triage)

    @pytest.mark.parametrize(
        ("owner", "step"),
        [(Triage, "record"), (Ledger, "record_target"), (Path, "rename")],
    )
    def test_kill_before_the_move_into_triage_lists_the_item_once(
        self, wired, monkeypatch, owner, step
    ) -> None:
        config, spool, _nas, remote = wired
        populate(remote / CAMERA_ROLL, {"undated/1J1A3188.jpg": 2048})

        kill_on_call(monkeypatch, owner, step)
        with pytest.raises(SimulatedSigkill):
            pump(config, spool)
        pump(config, spool)

        with Ledger(spool.ledger_path) as ledger:
            listed = pending(ledger, Triage(spool.triage))
        assert [(p.item.src_path, p.present) for p in listed] == [("undated/1J1A3188.jpg", True)]
        assert "no date in EXIF" in render_pending(listed, spool.triage)
        assert not (spool.incoming / "phone-1").exists()


class TestFiled:
    def test_marks_delivered_and_reclaims_the_spool(self, wired) -> None:
        config, spool, nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)
        filed = file_by_hand(copy, nas / "photos/2019/05/1J1A3188_phone-1.jpg")

        with Ledger(spool.ledger_path) as ledger:
            resolve_filed(config, ledger, spool, item.id, str(filed))

        after = row(spool, item.id)
        assert after["state"] == State.DELIVERED.value
        assert after["spool_path"] is None
        assert after["delivered_at"]
        assert after["dest_path"] == "photos/2019/05/1J1A3188_phone-1.jpg"
        assert after["triage_reason"] == NO_DATE, "why it was triaged stays on record"
        assert not copy.exists()
        assert not (spool.triage / NO_DATE / "phone-1").exists(), "empty dirs are pruned"
        assert filed.is_file()
        assert after["dest_sha256"] == file_digest(filed), "verify compares content later"

    def test_accepts_the_outbox_relative_form(self, wired) -> None:
        config, spool, nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)
        file_by_hand(copy, nas / "photos/2019/05/1J1A3188_phone-1.jpg")

        with Ledger(spool.ledger_path) as ledger:
            resolve_filed(
                config, ledger, spool, item.id, "photos/2019/05/1J1A3188_phone-1.jpg"
            )
        assert row(spool, item.id)["state"] == State.DELIVERED.value

    def test_nothing_at_the_path_is_refused(self, wired) -> None:
        config, spool, nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)

        with Ledger(spool.ledger_path) as ledger, pytest.raises(GuardError, match="nothing at"):
            resolve_filed(config, ledger, spool, item.id, str(nas / "photos/2019/05/x.jpg"))

        assert row(spool, item.id)["state"] == State.TRIAGED.value
        assert copy.is_file()

    def test_path_outside_the_library_is_refused(self, wired, tmp_path: Path) -> None:
        config, spool, _nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)
        elsewhere = file_by_hand(copy, tmp_path / "Desktop/1J1A3188.jpg")

        with Ledger(spool.ledger_path) as ledger, pytest.raises(GuardError, match="not under"):
            resolve_filed(config, ledger, spool, item.id, str(elsewhere))
        assert copy.is_file()

    def test_different_content_is_refused(self, wired) -> None:
        """A typo pointing at some other photo must not cost the only spool copy."""
        config, spool, nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)
        other = nas / "photos/2019/05/someone_else.jpg"
        populate(other.parent, {other.name: 64})

        with Ledger(spool.ledger_path) as ledger, pytest.raises(
            GuardError, match="not the same file"
        ):
            resolve_filed(config, ledger, spool, item.id, str(other))

        assert row(spool, item.id)["state"] == State.TRIAGED.value
        assert copy.is_file()

    def test_unmounted_share_is_refused(self, spool_root: Path, remote_root: Path,
                                        tmp_path: Path) -> None:
        """A copy into an unmounted share's mount point is on the local disk.

        No ismount patch here: the "share" is an ordinary directory, exactly what a
        dropped SMB mount leaves behind.
        """
        local = tmp_path / "nas"
        (local / ".photocopier-marker").parent.mkdir(parents=True)
        (local / ".photocopier-marker").touch()
        config = make_config(spool_root, nas=local)
        spool = Spool.from_config(config.spool)
        spool.ensure()
        copy = triage_undated(config, spool, remote_root)
        item = only_triaged(spool)
        filed = file_by_hand(copy, local / "photos/2019/05/1J1A3188_phone-1.jpg")

        with Ledger(spool.ledger_path) as ledger, pytest.raises(
            GuardError, match="not a mount point"
        ):
            resolve_filed(config, ledger, spool, item.id, str(filed))
        assert copy.is_file()

    def test_triaged_copy_already_moved_by_hand(self, wired) -> None:
        """Moving rather than copying out of triage is the natural thing to do."""
        config, spool, nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)
        filed = nas / "photos/2019/05/1J1A3188_phone-1.jpg"
        filed.parent.mkdir(parents=True)
        copy.rename(filed)

        with Ledger(spool.ledger_path) as ledger:
            result = resolve_filed(config, ledger, spool, item.id, str(filed))

        assert result.removed is None
        assert row(spool, item.id)["state"] == State.DELIVERED.value

    def test_dry_run_changes_nothing(self, wired) -> None:
        config, spool, nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)
        filed = file_by_hand(copy, nas / "photos/2019/05/1J1A3188_phone-1.jpg")

        with Ledger(spool.ledger_path) as ledger:
            resolve_filed(config, ledger, spool, item.id, str(filed), dry_run=True)

        assert row(spool, item.id)["state"] == State.TRIAGED.value
        assert copy.is_file()

    def test_only_triaged_items_can_be_resolved(self, wired) -> None:
        config, spool, _nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260601_112702768.jpg": 128})
        pump(config, spool)

        with Ledger(spool.ledger_path) as ledger:
            [item] = ledger.items_in_state(State.PROCESSED)
            with pytest.raises(LedgerError, match="not triaged"):
                resolve_discard(ledger, spool, item.id)
            with pytest.raises(LedgerError, match="no ledger item"):
                resolve_discard(ledger, spool, 9999)


class TestDiscard:
    def test_drops_the_copy_and_keeps_the_row(self, wired) -> None:
        config, spool, _nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)

        with Ledger(spool.ledger_path) as ledger:
            resolve_discard(ledger, spool, item.id)

        after = row(spool, item.id)
        assert after["state"] == State.DISCARDED.value
        assert after["spool_path"] is None
        assert after["delivered_at"] is None
        assert after["triage_reason"] == NO_DATE
        assert not copy.exists()

    def test_discarded_file_is_never_fetched_again(self, wired) -> None:
        """The whole reason the row is kept. See D1."""
        config, spool, _nas, remote = wired
        triage_undated(config, spool, remote)
        item = only_triaged(spool)
        with Ledger(spool.ledger_path) as ledger:
            resolve_discard(ledger, spool, item.id)

        with Ledger(spool.ledger_path) as ledger:
            result = ingest(config, Rclone(config.rclone), ledger, spool)
            assert result.total_ingested == 0
            assert ledger.count_in_state(State.TRIAGED) == 0

    def test_does_not_need_the_share(self, spool_root: Path, remote_root: Path) -> None:
        config = make_config(spool_root)  # /tmp/nas: not mounted
        spool = Spool.from_config(config.spool)
        spool.ensure()
        triage_undated(config, spool, remote_root)
        item = only_triaged(spool)

        with Ledger(spool.ledger_path) as ledger:
            resolve_discard(ledger, spool, item.id)
        assert row(spool, item.id)["state"] == State.DISCARDED.value

    def test_path_outside_triage_is_never_deleted(self, wired, tmp_path: Path) -> None:
        """A hand-edited row could point anywhere. Deleting it is not this tool's call."""
        config, spool, _nas, remote = wired
        triage_undated(config, spool, remote)
        item = only_triaged(spool)
        precious = tmp_path / "precious.jpg"
        precious.write_bytes(b"keep me")
        with Ledger(spool.ledger_path) as ledger:
            ledger.conn.execute(
                "UPDATE items SET spool_path = ? WHERE id = ?", (str(precious), item.id)
            )
            ledger.conn.commit()
            with pytest.raises(GuardError, match="outside"):
                resolve_discard(ledger, spool, item.id)

        assert precious.read_bytes() == b"keep me"
        assert row(spool, item.id)["state"] == State.TRIAGED.value


# D5's case three: a different file already sits at the target path.
CONFLICT_NAME = "2026/06/PXL_20260615_101500.jpg"
CONFLICT_TARGET = "photos/2026/06/PXL_20260615_101500_phone-1.jpg"


@pytest.fixture
def conflicted(wired):
    config, spool, nas, remote = wired
    populate(remote / CAMERA_ROLL, {CONFLICT_NAME: 128})
    pump(config, spool)
    existing = nas / CONFLICT_TARGET
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"a different photo that happens to share the name")
    with Ledger(spool.ledger_path) as ledger:
        assert flush(config, ledger, spool).conflicts
    copy = spool.triage / DEST_CONFLICT / "phone-1" / CONFLICT_NAME
    assert copy.is_file()
    return config, spool, nas, existing, copy, only_triaged(spool)


class TestDestConflict:
    def test_list_names_the_existing_target(self, conflicted) -> None:
        _config, spool, _nas, existing, copy, item = conflicted
        with Ledger(spool.ledger_path) as ledger:
            output = render_pending(pending(ledger, Triage(spool.triage)), spool.triage)

        assert f"#{item.id}  {DEST_CONFLICT}" in output
        assert str(copy) in output
        assert f"different content already at {existing}" in output

    def test_resolving_onto_the_existing_file_is_refused(self, conflicted) -> None:
        """The existing file is a different photo; this would lose the triaged one."""
        config, spool, _nas, existing, copy, item = conflicted
        original = existing.read_bytes()

        with Ledger(spool.ledger_path) as ledger, pytest.raises(
            GuardError, match="--discard"
        ):
            resolve_filed(config, ledger, spool, item.id, str(existing))

        assert existing.read_bytes() == original
        assert copy.is_file()

    def test_filed_under_another_name(self, conflicted) -> None:
        config, spool, nas, existing, copy, item = conflicted
        original = existing.read_bytes()
        renamed = file_by_hand(copy, nas / "photos/2026/06/PXL_20260615_101500_phone-1_b.jpg")

        with Ledger(spool.ledger_path) as ledger:
            resolve_filed(config, ledger, spool, item.id, str(renamed))

        after = row(spool, item.id)
        assert after["state"] == State.DELIVERED.value
        assert after["dest_path"] == "photos/2026/06/PXL_20260615_101500_phone-1_b.jpg"
        assert not copy.exists()
        assert existing.read_bytes() == original

    def test_keeping_the_existing_file_is_a_discard(self, conflicted) -> None:
        _config, spool, _nas, existing, copy, item = conflicted
        original = existing.read_bytes()

        with Ledger(spool.ledger_path) as ledger:
            resolve_discard(ledger, spool, item.id)

        assert row(spool, item.id)["state"] == State.DISCARDED.value
        assert not copy.exists()
        assert existing.read_bytes() == original


class TestKeepBoth:
    def test_flush_time_conflict_is_kept_then_both_delivered(self, conflicted) -> None:
        config, spool, nas, existing, copy, item = conflicted
        triaged_bytes = copy.read_bytes()
        original = existing.read_bytes()
        assert row(spool, item.id)["dest_path"] is None, "triaged rows never carry a dest_path"

        with Ledger(spool.ledger_path) as ledger:
            result = resolve_keep_both(config, ledger, spool, item.id)

        variant = result.dest_path
        assert re.fullmatch(r"photos/2026/06/PXL_20260615_101500_phone-1~[0-9a-f]{8}\.jpg", variant)
        assert (spool.outbox / variant).read_bytes() == triaged_bytes
        after = row(spool, item.id)
        assert after["state"] == State.PROCESSED.value
        assert after["dest_path"] == variant
        assert after["triage_reason"] == DEST_CONFLICT, "why it was held stays on record"
        assert not copy.exists()
        assert existing.read_bytes() == original

        with Ledger(spool.ledger_path) as ledger:
            flushed = flush(config, ledger, spool)
        assert len(flushed.delivered) == 1
        assert (nas / variant).read_bytes() == triaged_bytes
        assert existing.read_bytes() == original
        after = row(spool, item.id)
        assert after["state"] == State.DELIVERED.value
        assert after["dest_sha256"] == file_digest(nas / variant)

    def test_process_time_conflict_is_kept_with_the_share_unmounted(
        self, spool_root: Path, remote_root: Path
    ) -> None:
        """Two different files staged to one name. Keeping both needs neither share nor flush."""
        config = make_config(spool_root)
        spool = Spool.from_config(config.spool)
        spool.ensure()
        populate(remote_root / CAMERA_ROLL, {
            CONFLICT_NAME: 128,
            "retake/" + CONFLICT_NAME: 200,
        })
        pump(config, spool)
        item = only_triaged(spool)
        copy = Path(item.spool_path)
        triaged_bytes = copy.read_bytes()
        staged_first = (spool.outbox / CONFLICT_TARGET).read_bytes()
        assert staged_first != triaged_bytes

        with Ledger(spool.ledger_path) as ledger:
            result = resolve_keep_both(config, ledger, spool, item.id)

        assert (spool.outbox / result.dest_path).read_bytes() == triaged_bytes
        assert (spool.outbox / CONFLICT_TARGET).read_bytes() == staged_first
        assert row(spool, item.id)["state"] == State.PROCESSED.value
        assert not copy.exists()

    def test_dry_run_changes_nothing(self, conflicted) -> None:
        config, spool, _nas, _existing, copy, item = conflicted
        with Ledger(spool.ledger_path) as ledger:
            result = resolve_keep_both(config, ledger, spool, item.id, dry_run=True)

        assert result.dry_run
        assert not (spool.outbox / result.dest_path).exists()
        assert row(spool, item.id)["state"] == State.TRIAGED.value
        assert copy.is_file()

    def test_other_reasons_are_refused(self, wired) -> None:
        config, spool, _nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)

        with Ledger(spool.ledger_path) as ledger, pytest.raises(
            GuardError, match="only a dest_conflict"
        ):
            resolve_keep_both(config, ledger, spool, item.id)
        assert copy.is_file()
        assert row(spool, item.id)["state"] == State.TRIAGED.value

    def test_missing_triaged_copy_is_refused(self, conflicted) -> None:
        config, spool, _nas, _existing, copy, item = conflicted
        copy.unlink()

        with Ledger(spool.ledger_path) as ledger, pytest.raises(
            GuardError, match="triaged copy is gone"
        ):
            resolve_keep_both(config, ledger, spool, item.id)
        assert row(spool, item.id)["state"] == State.TRIAGED.value

    def test_without_a_manifest_entry_it_points_at_filed_at(self, conflicted) -> None:
        config, spool, _nas, _existing, copy, item = conflicted
        (spool.triage / "manifest.jsonl").unlink()

        with Ledger(spool.ledger_path) as ledger, pytest.raises(GuardError, match="--filed-at"):
            resolve_keep_both(config, ledger, spool, item.id)
        assert copy.is_file()

    def test_identical_variant_already_staged_is_reused(self, conflicted) -> None:
        """A crash after the copy and before the ledger write leaves exactly this state."""
        config, spool, _nas, _existing, copy, item = conflicted
        triaged_bytes = copy.read_bytes()
        staged = spool.outbox / variant_dest(CONFLICT_TARGET, file_digest(copy))
        staged.parent.mkdir(parents=True)
        shutil.copy2(copy, staged)

        with Ledger(spool.ledger_path) as ledger:
            result = resolve_keep_both(config, ledger, spool, item.id)

        assert result.dest_path == staged.relative_to(spool.outbox).as_posix()
        assert staged.read_bytes() == triaged_bytes
        assert row(spool, item.id)["state"] == State.PROCESSED.value
        assert not copy.exists()

    def test_different_variant_already_staged_is_refused(self, conflicted) -> None:
        config, spool, _nas, _existing, copy, item = conflicted
        staged = spool.outbox / variant_dest(CONFLICT_TARGET, file_digest(copy))
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"someone else's bytes")

        with Ledger(spool.ledger_path) as ledger, pytest.raises(
            GuardError, match="different content"
        ):
            resolve_keep_both(config, ledger, spool, item.id)

        assert staged.read_bytes() == b"someone else's bytes"
        assert copy.is_file()
        assert row(spool, item.id)["state"] == State.TRIAGED.value

    def test_crash_before_the_ledger_write_converges_on_rerun(
        self, conflicted, monkeypatch
    ) -> None:
        config, spool, _nas, _existing, copy, item = conflicted
        triaged_bytes = copy.read_bytes()

        kill_on_call(monkeypatch, Ledger, "restage")
        with Ledger(spool.ledger_path) as ledger, pytest.raises(SimulatedSigkill):
            resolve_keep_both(config, ledger, spool, item.id)
        assert row(spool, item.id)["state"] == State.TRIAGED.value
        assert copy.is_file()

        monkeypatch.undo()
        with Ledger(spool.ledger_path) as ledger:
            result = resolve_keep_both(config, ledger, spool, item.id)

        assert (spool.outbox / result.dest_path).read_bytes() == triaged_bytes
        assert row(spool, item.id)["state"] == State.PROCESSED.value
        assert not copy.exists()
        assert not list(spool.outbox.rglob("*.keepboth.part"))


class TestFiledLocation:
    def test_absolute_video_path(self, wired) -> None:
        config, _spool, nas, _remote = wired
        path, dest = filed_location(config, str(nas / "video/2025/clip.mp4"))
        assert path == nas / "video/2025/clip.mp4"
        assert dest == "video/2025/clip.mp4"

    def test_bare_relative_path_is_refused(self, wired) -> None:
        config, *_ = wired
        with pytest.raises(GuardError, match="neither"):
            filed_location(config, "2025/03/x.jpg")

    def test_escaping_the_root_is_refused(self, wired) -> None:
        config, *_ = wired
        with pytest.raises(GuardError, match="not under"):
            filed_location(config, "photos/../../elsewhere/x.jpg")


class TestCommands:
    CONFIG = """
[spool]
path = "{spool}"

[destination]
photos_root = "{nas}/photos"
video_root = "{nas}/video"
mount_point = "{nas}"

[rclone]
remote = "onedrive"
binary = "{rclone}"

[[source]]
id = "phone-1"
path = "Camera Roll"
suffix = "phone-1"
"""

    @pytest.fixture
    def config_file(self, wired, tmp_path: Path) -> Path:
        _config, spool, nas, _remote = wired
        path = tmp_path / "config.toml"
        path.write_text(
            self.CONFIG.format(spool=spool.root, nas=nas, rclone=FAKE_RCLONE), encoding="utf-8"
        )
        return path

    def test_triage_then_resolve_then_status(
        self, wired, config_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config, spool, nas, remote = wired
        copy = triage_undated(config, spool, remote)
        item = only_triaged(spool)
        filed = file_by_hand(copy, nas / "photos/2019/05/1J1A3188_phone-1.jpg")

        assert main(["-c", str(config_file), "triage"]) == 0
        assert f"#{item.id}  {NO_DATE}" in capsys.readouterr().out

        assert main(["-c", str(config_file), "resolve", str(item.id), "--filed-at",
                     str(filed), "--dry-run"]) == 0
        assert "would mark delivered" in capsys.readouterr().out
        assert copy.is_file()

        assert main(["-c", str(config_file), "resolve", str(item.id), "--filed-at",
                     str(filed)]) == 0
        out = capsys.readouterr().out
        assert "marked delivered at photos/2019/05/1J1A3188_phone-1.jpg" in out
        assert "removed triaged copy" in out

        assert main(["-c", str(config_file), "status"]) == 0
        out = capsys.readouterr().out
        assert "triaged      0" in out
        assert "awaiting review" not in out

    def test_discard(
        self, wired, config_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config, spool, _nas, remote = wired
        triage_undated(config, spool, remote)
        item = only_triaged(spool)

        assert main(["-c", str(config_file), "resolve", str(item.id), "--discard"]) == 0
        assert "marked discarded" in capsys.readouterr().out

        assert main(["-c", str(config_file), "triage"]) == 0
        assert "nothing in triage" in capsys.readouterr().out

    def test_errors_are_clean(
        self, wired, config_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config, spool, _nas, remote = wired
        triage_undated(config, spool, remote)

        assert main(["-c", str(config_file), "resolve", "9999", "--discard"]) == 1
        assert "no ledger item #9999" in capsys.readouterr().err

    def test_an_outcome_is_required(self, config_file: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["-c", str(config_file), "resolve", "1"])
        assert exc.value.code == 2


class TestConflictDetail:
    """flush and process write the detail; resolve reads the target back out of it."""

    @pytest.mark.parametrize(
        ("target", "staged"),
        [("/Volumes/media/photos/2026/06/x.jpg", False), ("photos/2026/06/x.jpg", True)],
    )
    def test_target_round_trips(self, target: str, staged: bool) -> None:
        assert conflict_target(conflict_detail(target, staged=staged)) == target

    def test_other_details_name_no_target(self) -> None:
        assert conflict_target("no EXIF date") is None
        assert conflict_target("unrecognised extension: x.bin") is None

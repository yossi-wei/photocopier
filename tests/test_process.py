"""Stage 2 end to end: incoming/ -> outbox/ + triage/."""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from photocopier.ledger import SCHEMA, Item, Ledger, State
from photocopier.process import process
from photocopier.rclone import Rclone
from photocopier.spool import Spool
from photocopier.triage import (
    BEFORE_CUTOFF,
    DEST_CONFLICT,
    NO_DATE,
    UNKNOWN_TYPE,
    UNTRACKED,
    Triage,
)

from .conftest import FIXTURES, make_config, populate, write_file
from .test_flush import (
    PHOTO,
    STAGED,
    SimulatedSigkill,
    kill_on_call,
    nas_files,
    pump,
    run_flush,
)

CAMERA_ROLL = "Camera Roll"


def run_ingest(config, spool: Spool):
    from photocopier.ingest import ingest

    with Ledger(spool.ledger_path) as ledger:
        return ingest(config, Rclone(config.rclone), ledger, spool)


def run_process(config, spool: Spool, *, dry_run: bool = False):
    with Ledger(spool.ledger_path) as ledger:
        return process(config, ledger, spool, dry_run=dry_run)


def outbox_files(spool: Spool) -> set[str]:
    return {
        str(p.relative_to(spool.outbox)) for p in spool.outbox.rglob("*") if p.is_file()
    }


def incoming_files(spool: Spool) -> set[str]:
    return {
        str(p.relative_to(spool.incoming)) for p in spool.incoming.rglob("*") if p.is_file()
    }


def truncated_stat(suffix: str):
    """Path.stat that reports a file ending in `suffix` one byte short, as a cut copy."""
    real = Path.stat

    def stat(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        if str(self).endswith(suffix) and "incoming" in str(self):
            fields = list(result)
            fields[6] -= 1
            return os.stat_result(fields)
        return result

    return stat


def place(remote_root: Path, rel: str, fixture: str) -> None:
    """Copy a real fixture JPEG into the fake remote at a given path."""
    target = remote_root / CAMERA_ROLL / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / fixture, target)


class TestFiling:
    def test_photo_is_filed_by_exif_date(self, config, spool: Spool, remote_root: Path) -> None:
        place(remote_root, "2026/11/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")
        run_ingest(config, spool)

        result = run_process(config, spool)

        assert len(result.filed) == 1
        # EXIF says 31 Oct local; both the filename and the OneDrive folder say November.
        assert outbox_files(spool) == {
            "photos/2026/10/PXL_20261101_021009424_phone-1.jpg"
        }
        assert result.date_sources == {"exif": 1}

    def test_exif_overrides_both_weaker_tiers(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        """The whole point of the tier order, on a real file."""
        place(remote_root, "2026/11/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")
        run_ingest(config, spool)
        run_process(config, spool)
        assert not (spool.outbox / "photos/2026/11").exists()

    def test_video_falls_back_to_filename_and_files_by_year(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        populate(remote_root / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.mp4": 512})
        run_ingest(config, spool)

        result = run_process(config, spool)

        assert outbox_files(spool) == {"video/2026/PXL_20260615_101500_phone-1.mp4"}
        assert result.date_sources == {"filename": 1}

    def test_source_path_is_the_last_resort(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        """A stripped file with no EXIF and no date in its name, like the real 1J1A set."""
        place(remote_root, "2026/07/1J1A3188.jpg", "1J1A3188.jpg")
        run_ingest(config, spool)

        result = run_process(config, spool)

        assert outbox_files(spool) == {"photos/2026/07/1J1A3188_phone-1.jpg"}
        assert result.date_sources == {"srcpath": 1}

    def test_incoming_is_emptied_and_pruned(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        populate(remote_root / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        run_ingest(config, spool)
        run_process(config, spool)

        assert incoming_files(spool) == set()
        assert not (spool.incoming / "phone-1" / "2026" / "06").exists()

    def test_ledger_records_the_destination(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        populate(remote_root / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        run_ingest(config, spool)
        run_process(config, spool)

        with Ledger(spool.ledger_path) as ledger:
            assert ledger.count_in_state(State.PROCESSED) == 1
            row = ledger.conn.execute("SELECT dest_path, resolved_date FROM items").fetchone()
            assert row["dest_path"] == "photos/2026/06/PXL_20260615_101500_phone-1.jpg"
            assert row["resolved_date"] == "2026-06-15"


class TestTriage:
    def test_undated_file_goes_to_triage(self, config, spool: Spool, remote_root: Path) -> None:
        place(remote_root, "no_date_anywhere/1J1A3188.jpg", "1J1A3188.jpg")
        run_ingest(config, spool)

        result = run_process(config, spool)

        assert [reason for _i, reason in result.triaged] == [NO_DATE]
        assert outbox_files(spool) == set()
        assert (spool.triage / NO_DATE / "phone-1" / "no_date_anywhere" / "1J1A3188.jpg").is_file()

    def test_unknown_type_goes_to_triage(self, config, spool: Spool, remote_root: Path) -> None:
        populate(remote_root / CAMERA_ROLL, {"2026/06/notes.txt": 20})
        run_ingest(config, spool)

        result = run_process(config, spool)

        assert [reason for _i, reason in result.triaged] == [UNKNOWN_TYPE]
        assert outbox_files(spool) == set()

    def test_triage_records_a_reason(self, config, spool: Spool, remote_root: Path) -> None:
        place(remote_root, "no_date_anywhere/1J1A3188.jpg", "1J1A3188.jpg")
        run_ingest(config, spool)
        run_process(config, spool)

        entries = Triage(spool.triage).entries()
        assert len(entries) == 1
        assert entries[0].reason == NO_DATE
        assert entries[0].source_id == "phone-1"
        assert "no date" in entries[0].detail

    def test_triaged_items_are_never_staged(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        place(remote_root, "no_date_anywhere/1J1A3188.jpg", "1J1A3188.jpg")
        run_ingest(config, spool)
        run_process(config, spool)

        with Ledger(spool.ledger_path) as ledger:
            assert ledger.count_in_state(State.TRIAGED) == 1
            assert ledger.count_in_state(State.PROCESSED) == 0

    def test_manifest_survives_a_partial_line(self, spool: Spool) -> None:
        """JSON Lines: an interrupted write must not destroy earlier entries."""
        triage = Triage(spool.triage)
        triage.record(
            reason=NO_DATE, source_id="phone-1", src_path="a.jpg",
            triage_path=spool.triage / NO_DATE / "phone-1" / "a.jpg", detail="x",
        )
        with triage.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write('{"reason": "no_da')  # killed mid-write

        assert len(triage.entries()) == 1


class TestConflicts:
    def test_identical_file_already_staged_is_dropped(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        """Case two of D5: routine, not an error."""
        place(remote_root, "2026/11/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")
        run_ingest(config, spool)
        run_process(config, spool)

        # Same file appears again at a different source path, so it is a new ledger item.
        place(remote_root, "2026/12/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")
        run_ingest(config, spool)
        result = run_process(config, spool)

        assert len(result.duplicates) == 1
        assert not result.triaged
        assert len(outbox_files(spool)) == 1

    def test_different_content_at_the_same_name_is_triaged(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        """Case three of D5: never overwrite, never silently skip."""
        place(remote_root, "2026/11/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")
        run_ingest(config, spool)
        run_process(config, spool)
        staged = spool.outbox / "photos/2026/10/PXL_20261101_021009424_phone-1.jpg"
        original_bytes = staged.read_bytes()

        # A different photo that resolves to the same destination name.
        place(remote_root, "2026/12/PXL_20261101_021009424.jpg", "only_datetime.jpg")
        (remote_root / CAMERA_ROLL / "2026/12/PXL_20261101_021009424.jpg").write_bytes(
            (FIXTURES / "PXL_20261101_021009424.jpg").read_bytes() + b"different"
        )
        run_ingest(config, spool)
        result = run_process(config, spool)

        assert [reason for _i, reason in result.triaged] == [DEST_CONFLICT]
        assert staged.read_bytes() == original_bytes, "the staged file must not be overwritten"

    def test_kill_while_dropping_a_duplicate_converges(
        self, config, spool: Spool, remote_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        place(remote_root, "2026/11/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")
        run_ingest(config, spool)
        run_process(config, spool)
        place(remote_root, "2026/12/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")
        run_ingest(config, spool)

        kill_on_call(monkeypatch, Ledger, "mark_processed")
        with pytest.raises(SimulatedSigkill):
            run_process(config, spool)
        result = run_process(config, spool)

        assert len(result.duplicates) == 1 and not result.missing
        assert incoming_files(spool) == set()


class TestStrayIncoming:
    NAME = "PXL_20261101_021009424.jpg"

    def kill_after_dropping_ledger_write(self, config, spool, remote_root, monkeypatch) -> None:
        place(remote_root, f"2026/11/{self.NAME}", self.NAME)
        run_ingest(config, spool)
        run_process(config, spool)
        place(remote_root, f"2026/12/{self.NAME}", self.NAME)
        run_ingest(config, spool)
        kill_on_call(monkeypatch, Ledger, "mark_processed", after=True)
        with pytest.raises(SimulatedSigkill):
            run_process(config, spool)

    def test_kill_between_process_ledger_write_and_incoming_delete_converges(
        self, config, spool: Spool, remote_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.kill_after_dropping_ledger_write(config, spool, remote_root, monkeypatch)

        result = run_process(config, spool)

        assert incoming_files(spool) == set()
        assert len(result.duplicates) == 1
        assert len(outbox_files(spool)) == 1

    def test_stray_is_reclaimed_after_flush_delivered_it(
        self, wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, spool, nas, remote = wired
        self.kill_after_dropping_ledger_write(config, spool, remote, monkeypatch)
        run_flush(config, spool)

        run_process(config, spool)

        assert incoming_files(spool) == set()
        assert len(nas_files(nas)) == 1

    def orphan(self, spool: Spool, rel: str | None = None) -> Path:
        """A file ingest copied into incoming/ but was killed before recording."""
        path = spool.source_incoming("phone-1") / (rel or f"2026/11/{self.NAME}")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(FIXTURES / self.NAME, path)
        return path

    def test_unrecorded_incoming_file_is_triaged_as_untracked(
        self, config, spool: Spool
    ) -> None:
        """It used to be kept in incoming/ with no ledger row, so nothing ever filed it,
        listed it, or counted it."""
        orphan = self.orphan(spool)
        content = orphan.read_bytes()

        result = run_process(config, spool)

        assert [reason for _item, reason in result.triaged] == [UNTRACKED]
        assert incoming_files(spool) == set()
        kept = spool.triage / UNTRACKED / "phone-1" / f"2026/11/{self.NAME}"
        assert kept.read_bytes() == content
        with Ledger(spool.ledger_path) as ledger:
            (item,) = ledger.items_in_state(State.TRIAGED)
        assert (item.triage_reason, item.src_path) == (UNTRACKED, f"2026/11/{self.NAME}")
        assert item.spool_path == str(kept)

    def test_untracked_file_under_a_version_folder_keeps_its_source_path(
        self, config, spool: Spool
    ) -> None:
        self.orphan(spool, f"0123456789abcdef/2026/11/{self.NAME}")
        run_process(config, spool)
        with Ledger(spool.ledger_path) as ledger:
            (item,) = ledger.items_in_state(State.TRIAGED)
        assert item.src_path == f"2026/11/{self.NAME}"

    def test_untracked_file_is_left_alone_by_a_dry_run(self, config, spool: Spool) -> None:
        orphan = self.orphan(spool)
        result = run_process(config, spool, dry_run=True)
        assert [reason for _item, reason in result.triaged] == [UNTRACKED]
        assert orphan.is_file()
        with Ledger(spool.ledger_path) as ledger:
            assert ledger.items_in_state(State.TRIAGED) == []

    def test_untracked_copy_of_a_waiting_version_is_dropped(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        """A kill before the record leaves the first copy; the retry lands the same bytes
        under a version folder and records that one."""
        place(remote_root, f"2026/11/{self.NAME}", self.NAME)
        self.orphan(spool)
        run_ingest(config, spool)

        result = run_process(config, spool)

        assert result.triaged == []
        assert incoming_files(spool) == set()
        assert len(outbox_files(spool)) == 1

    def test_rclone_partial_is_not_admitted(self, config, spool: Spool) -> None:
        partial = self.orphan(spool, f"2026/11/{self.NAME}.partial")
        result = run_process(config, spool)
        assert result.triaged == []
        assert partial.is_file()

    def test_bytes_a_failed_copy_left_are_left_for_the_retry(
        self, config, spool: Spool, remote_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        place(remote_root, f"2026/11/{self.NAME}", self.NAME)
        monkeypatch.setattr(Path, "stat", truncated_stat(f"2026/11/{self.NAME}"))
        run_ingest(config, spool)
        monkeypatch.undo()
        with Ledger(spool.ledger_path) as ledger:
            assert len(ledger.items_in_state(State.FAILED)) == 1

        result = run_process(config, spool)

        assert result.triaged == []
        assert incoming_files(spool) == {f"phone-1/2026/11/{self.NAME}"}


class TestKilledProcess:
    def scheduled_pass(self, config, spool: Spool):
        run_ingest(config, spool)
        result = run_process(config, spool)
        run_flush(config, spool)
        return result

    def test_kill_after_rename_before_ledger_still_delivers(self, wired, monkeypatch) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {PHOTO: 4096})
        original = (remote / CAMERA_ROLL / PHOTO).read_bytes()
        run_ingest(config, spool)

        kill_on_call(monkeypatch, Ledger, "mark_processed")
        with pytest.raises(SimulatedSigkill):
            run_process(config, spool)
        passes = [self.scheduled_pass(config, spool) for _ in range(3)]

        assert [len(p.missing) for p in passes] == [0, 0, 0]
        assert (nas / STAGED).read_bytes() == original

    @pytest.mark.parametrize(
        ("owner", "step"), [(Ledger, "record_target"), (Path, "rename")]
    )
    def test_kill_before_the_rename_converges(self, wired, monkeypatch, owner, step) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {PHOTO: 4096})
        original = (remote / CAMERA_ROLL / PHOTO).read_bytes()
        run_ingest(config, spool)

        kill_on_call(monkeypatch, owner, step)
        with pytest.raises(SimulatedSigkill):
            run_process(config, spool)
        result = self.scheduled_pass(config, spool)

        assert len(result.filed) == 1 and not result.missing
        assert (nas / STAGED).read_bytes() == original
        assert not (spool.incoming / "phone-1").exists()

    def test_kill_while_filing_a_copy_of_a_delivered_photo_converges(
        self, wired, monkeypatch
    ) -> None:
        config, spool, nas, remote = wired
        same = b"S" * 4096
        write_file(remote / CAMERA_ROLL / "2026/06/a/PXL_20260615_101500.jpg", same)
        self.scheduled_pass(config, spool)
        write_file(remote / CAMERA_ROLL / "2026/06/b/PXL_20260615_101500.jpg", same)
        run_ingest(config, spool)

        kill_on_call(monkeypatch, Ledger, "mark_processed")
        with pytest.raises(SimulatedSigkill):
            run_process(config, spool)
        run_flush(config, spool)
        result = self.scheduled_pass(config, spool)

        assert not result.missing
        assert (nas / STAGED).read_bytes() == same
        with Ledger(spool.ledger_path) as ledger:
            assert ledger.count_in_state(State.DELIVERED) == 2

    def test_dry_run_after_a_kill_reports_the_file_and_moves_nothing(
        self, wired, monkeypatch
    ) -> None:
        config, spool, _nas, remote = wired
        populate(remote / CAMERA_ROLL, {PHOTO: 4096})
        run_ingest(config, spool)
        kill_on_call(monkeypatch, Ledger, "mark_processed")
        with pytest.raises(SimulatedSigkill):
            run_process(config, spool)

        result = run_process(config, spool, dry_run=True)

        assert len(result.filed) == 1 and not result.missing
        assert outbox_files(spool) == {STAGED}
        with Ledger(spool.ledger_path) as ledger:
            assert ledger.count_in_state(State.INGESTED) == 1

    def test_ledger_from_an_older_build_is_upgraded_and_recovers(
        self, wired, monkeypatch
    ) -> None:
        config, spool, nas, _remote = wired
        original = b"P" * 4096
        incoming = write_file(spool.incoming / "phone-1" / PHOTO, original)
        with closing(sqlite3.connect(spool.ledger_path)) as conn:
            conn.executescript(
                SCHEMA.replace("  target_path   TEXT,\n  target_identity TEXT,\n", "")
            )
            columns = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
            assert not {"target_path", "target_identity"} & columns
            conn.execute(
                "INSERT INTO items (source_id, src_path, src_size, state, spool_path, "
                "discovered_at, ingested_at) VALUES ('phone-1', ?, ?, 'ingested', ?, ?, ?)",
                (PHOTO, len(original), str(incoming), "2026-11-01", "2026-11-01"),
            )
            conn.commit()

        kill_on_call(monkeypatch, Ledger, "mark_processed")
        with pytest.raises(SimulatedSigkill):
            run_process(config, spool)
        result = self.scheduled_pass(config, spool)

        assert not result.missing
        assert (nas / STAGED).read_bytes() == original


class TestAnotherFileAtARecordedTarget:
    def kill_the_first_move(self, config, spool: Spool, remote: Path, monkeypatch) -> Item:
        write_file(remote / CAMERA_ROLL / PHOTO, b"A" * 4096)
        run_ingest(config, spool)
        with monkeypatch.context() as patch:
            kill_on_call(patch, Path, "rename")
            with pytest.raises(SimulatedSigkill):
                run_process(config, spool)
        with Ledger(spool.ledger_path) as ledger:
            (item,) = ledger.items_in_state(State.INGESTED)
        assert item.target_path and item.spool_path
        return item

    def state_of(self, spool: Spool, item: Item) -> State:
        with Ledger(spool.ledger_path) as ledger:
            return ledger.get(item.id).state

    def test_a_hand_copied_file_is_not_adopted(self, wired, monkeypatch) -> None:
        config, spool, nas, remote = wired
        item = self.kill_the_first_move(config, spool, remote, monkeypatch)
        Path(str(item.spool_path)).unlink()
        write_file(Path(str(item.target_path)), b"Y" * 4096)

        pump(config, spool)
        run_flush(config, spool)

        assert self.state_of(spool, item) == State.INGESTED
        assert not (nas / STAGED).exists()

    def test_a_later_photo_for_the_same_destination_is_not_adopted(
        self, wired, monkeypatch
    ) -> None:
        config, spool, nas, remote = wired
        item = self.kill_the_first_move(config, spool, remote, monkeypatch)
        Path(str(item.spool_path)).unlink()
        write_file(remote / CAMERA_ROLL / "2026/06/A/PXL_20260615_101500.jpg", b"Y" * 4096)

        pump(config, spool)
        pump(config, spool)
        run_flush(config, spool)

        assert self.state_of(spool, item) == State.INGESTED
        assert (nas / STAGED).read_bytes() == b"Y" * 4096
        with Ledger(spool.ledger_path) as ledger:
            assert ledger.count_in_state(State.DELIVERED) == 1


class TestIdempotency:
    def test_second_process_is_a_no_op(self, config, spool: Spool, remote_root: Path) -> None:
        populate(
            remote_root / CAMERA_ROLL,
            {"2026/06/PXL_20260615_101500.jpg": 128, "2026/06/PXL_20260616_101500.mp4": 256},
        )
        run_ingest(config, spool)
        first = run_process(config, spool)
        snapshot = outbox_files(spool)

        second = run_process(config, spool)

        assert len(first.filed) == 2
        assert second.total_seen == 0
        assert outbox_files(spool) == snapshot

    def test_dry_run_changes_nothing(self, config, spool: Spool, remote_root: Path) -> None:
        populate(remote_root / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        run_ingest(config, spool)

        result = run_process(config, spool, dry_run=True)

        assert len(result.filed) == 1
        assert outbox_files(spool) == set()
        assert incoming_files(spool) != set()

        # And the real run afterwards still does the work.
        assert len(run_process(config, spool).filed) == 1


class TestMissingSpoolFile:
    def test_vanished_file_is_reported_not_crashed(
        self, config, spool: Spool, remote_root: Path
    ) -> None:
        populate(remote_root / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        run_ingest(config, spool)
        (spool.incoming / "phone-1/2026/06/PXL_20260615_101500.jpg").unlink()

        result = run_process(config, spool)

        assert len(result.missing) == 1
        assert not result.filed


class TestMultipleSources:
    def test_same_filename_from_two_phones_both_survive(
        self, spool_root: Path, remote_root: Path
    ) -> None:
        config = make_config(
            spool_root,
            sources=[
                {"id": "phone-1", "path": CAMERA_ROLL, "suffix": "phone-1"},
                {"id": "phone-2", "path": "Pictures/Camera Roll", "suffix": "phone-2"},
            ],
        )
        spool = Spool.from_config(config.spool)
        spool.ensure()
        populate(remote_root / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        populate(remote_root / "Pictures/Camera Roll", {"2026/06/PXL_20260615_101500.jpg": 256})

        run_ingest(config, spool)
        result = run_process(config, spool)

        assert len(result.filed) == 2
        assert outbox_files(spool) == {
            "photos/2026/06/PXL_20260615_101500_phone-1.jpg",
            "photos/2026/06/PXL_20260615_101500_phone-2.jpg",
        }


@pytest.mark.parametrize("reason", [NO_DATE, UNKNOWN_TYPE, DEST_CONFLICT])
def test_triage_rejects_unknown_reasons(spool: Spool, reason: str) -> None:
    triage = Triage(spool.triage)
    assert triage.path_for(reason, "phone-1", "a.jpg")
    with pytest.raises(ValueError, match="unknown triage reason"):
        triage.path_for("made_up", "phone-1", "a.jpg")


def test_triage_leaves_no_empty_directories(config, spool: Spool, remote_root: Path) -> None:
    """Filed items pruned their parents but triaged ones did not, so the incoming tree
    slowly accumulated empty skeletons across runs.
    """
    place(remote_root, "1969/1J1A3188.jpg", "1J1A3188.jpg")
    run_ingest(config, spool)
    run_process(config, spool)

    assert not (spool.incoming / "phone-1" / "1969").exists()
    assert not (spool.incoming / "phone-1").exists()


class TestCutoffOnTheResolvedDate:
    """A file whose name and folder carry no date cannot be skipped by ingest's cutoff, so
    one whose EXIF predates the cutoff used to be filed anyway (D14)."""

    CUTOFF = "2026-11-01"
    EXIF_OCT_31 = "PXL_20261101_021009424.jpg"  # EXIF DateTimeOriginal 2026-10-31

    @pytest.fixture
    def with_cutoff(self, spool_root: Path):
        config = make_config(spool_root, sources=[
            {"id": "phone-1", "path": CAMERA_ROLL, "suffix": "phone-1", "cutoff": self.CUTOFF},
        ])
        spool = Spool.from_config(config.spool)
        spool.ensure()
        return config, spool

    def test_undated_name_with_exif_before_the_cutoff_is_triaged(
        self, with_cutoff, remote_root: Path
    ) -> None:
        config, spool = with_cutoff
        place(remote_root, "2026/edited.jpg", self.EXIF_OCT_31)
        run_ingest(config, spool)

        result = run_process(config, spool)

        assert outbox_files(spool) == set()
        assert [reason for _item, reason in result.triaged] == [BEFORE_CUTOFF]
        assert (spool.triage / BEFORE_CUTOFF / "phone-1" / "2026/edited.jpg").is_file()
        entry = Triage(spool.triage).entries()[-1]
        assert entry.detail == "exif dates it 2026-10-31, before the 2026-11-01 cutoff"

    def test_undated_name_with_exif_after_the_cutoff_is_filed(
        self, spool_root: Path, remote_root: Path
    ) -> None:
        config = make_config(spool_root, sources=[
            {"id": "phone-1", "path": CAMERA_ROLL, "suffix": "phone-1", "cutoff": "2026-10-01"},
        ])
        spool = Spool.from_config(config.spool)
        spool.ensure()
        place(remote_root, "2026/edited.jpg", self.EXIF_OCT_31)
        run_ingest(config, spool)
        run_process(config, spool)
        assert outbox_files(spool) == {"photos/2026/10/edited_phone-1.jpg"}

    def test_window_backfill_of_a_named_file_is_still_filed(
        self, with_cutoff, remote_root: Path
    ) -> None:
        """`ingest --since` pulls files from before the cutoff on purpose (D12)."""
        from datetime import date

        from photocopier.ingest import ingest

        config, spool = with_cutoff
        place(remote_root, "IMG_20261031_120000.jpg", self.EXIF_OCT_31)
        with Ledger(spool.ledger_path) as ledger:
            ingest(config, Rclone(config.rclone), ledger, spool, since=date(2026, 10, 1))
        result = run_process(config, spool)
        assert result.triaged == []
        assert outbox_files(spool) == {"photos/2026/10/IMG_20261031_120000_phone-1.jpg"}

    def test_no_cutoff_files_it(self, config, spool: Spool, remote_root: Path) -> None:
        place(remote_root, "2026/edited.jpg", self.EXIF_OCT_31)
        run_ingest(config, spool)
        run_process(config, spool)
        assert outbox_files(spool) == {"photos/2026/10/edited_phone-1.jpg"}

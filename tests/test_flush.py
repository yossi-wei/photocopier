"""Stage 3 end to end: outbox/ -> the NAS.

The unmounted-share test is the most important in the suite. When an SMB share drops,
the mount point remains as an ordinary empty directory, so a naive delivery writes the
entire backlog onto the laptop's boot disk and reports complete success.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from photocopier.errors import GuardError
from photocopier.flush import flush
from photocopier.ingest import ingest
from photocopier.ledger import Ledger, State
from photocopier.process import process
from photocopier.rclone import Rclone
from photocopier.resolve import pending, render_pending, resolve_filed
from photocopier.spool import Spool
from photocopier.triage import DEST_CONFLICT, Triage

from .conftest import FIXTURES, make_config, populate, write_file

CAMERA_ROLL = "Camera Roll"


def pump(config, spool: Spool) -> None:
    """Run ingest and process, leaving everything staged in the outbox."""
    with Ledger(spool.ledger_path) as ledger:
        ingest(config, Rclone(config.rclone), ledger, spool)
        process(config, ledger, spool)


def run_flush(config, spool: Spool, *, dry_run: bool = False):
    with Ledger(spool.ledger_path) as ledger:
        return flush(config, ledger, spool, dry_run=dry_run)


def nas_files(nas: Path) -> set[str]:
    return {
        str(p.relative_to(nas))
        for p in nas.rglob("*")
        if p.is_file() and p.name != ".photocopier-marker"
    }


def outbox_files(spool: Spool) -> set[str]:
    return {str(p.relative_to(spool.outbox)) for p in spool.outbox.rglob("*") if p.is_file()}


class TestDelivery:
    def test_delivers_photos_and_video_to_their_roots(self, wired) -> None:
        config, spool, nas, remote = wired
        populate(
            remote / CAMERA_ROLL,
            {"2026/06/PXL_20260615_101500.jpg": 128, "2026/06/PXL_20260616_101500.mp4": 256},
        )
        pump(config, spool)

        result = run_flush(config, spool)

        assert len(result.delivered) == 2
        assert nas_files(nas) == {
            "photos/2026/06/PXL_20260615_101500_phone-1.jpg",
            "video/2026/PXL_20260616_101500_phone-1.mp4",
        }

    def test_spool_is_reclaimed(self, wired) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)
        run_flush(config, spool)

        assert outbox_files(spool) == set()
        assert not (spool.outbox / "photos" / "2026").exists()

    def test_content_arrives_intact(self, wired) -> None:
        config, spool, nas, remote = wired
        source = remote / CAMERA_ROLL / "2026/06/PXL_20260615_101500.jpg"
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 4096})
        expected = source.read_bytes()
        pump(config, spool)
        run_flush(config, spool)

        landed = nas / "photos/2026/06/PXL_20260615_101500_phone-1.jpg"
        assert landed.read_bytes() == expected

    def test_ledger_marks_delivered_and_clears_the_spool_path(self, wired) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)
        run_flush(config, spool)

        with Ledger(spool.ledger_path) as ledger:
            assert ledger.count_in_state(State.DELIVERED) == 1
            row = ledger.conn.execute("SELECT spool_path, dest_path FROM items").fetchone()
            assert row["spool_path"] is None
            assert row["dest_path"] == "photos/2026/06/PXL_20260615_101500_phone-1.jpg"

    def test_no_partial_files_are_left_behind(self, wired) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)
        run_flush(config, spool)

        assert not list(nas.rglob("*.photocopier-partial"))


class TestUnmountedShare:
    """The failure this whole design is most afraid of."""

    def test_unmounted_share_is_refused(self, spool_root: Path, tmp_path: Path, remote_root: Path):
        looks_fine = tmp_path / "nas"
        (looks_fine / "photos").mkdir(parents=True)
        (looks_fine / ".photocopier-marker").touch()  # marker present but not a mount

        config = make_config(spool_root, nas=looks_fine)
        spool = Spool.from_config(config.spool)
        spool.ensure()
        populate(remote_root / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)

        with pytest.raises(GuardError, match="not a mount point"):
            run_flush(config, spool)

    def test_nothing_is_written_when_the_guard_fires(
        self, spool_root: Path, tmp_path: Path, remote_root: Path
    ) -> None:
        looks_fine = tmp_path / "nas"
        (looks_fine / "photos").mkdir(parents=True)
        (looks_fine / ".photocopier-marker").touch()

        config = make_config(spool_root, nas=looks_fine)
        spool = Spool.from_config(config.spool)
        spool.ensure()
        populate(remote_root / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)

        with pytest.raises(GuardError):
            run_flush(config, spool)

        assert nas_files(looks_fine) == set()
        assert outbox_files(spool) != set(), "the backlog must stay staged, not vanish"

    def test_missing_marker_is_refused(self, wired, nas: Path) -> None:
        """A real mount is not enough — the marker proves it is the right share."""
        config, spool, _nas, remote = wired
        (nas / ".photocopier-marker").unlink()
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)

        with pytest.raises(GuardError, match="marker"):
            run_flush(config, spool)


class TestConflictPolicy:
    def test_identical_file_already_there_is_dropped(self, wired) -> None:
        """Case two of D5 — routine, e.g. filed by hand before the cutoff."""
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)

        target = nas / "photos/2026/06/PXL_20260615_101500_phone-1.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(spool.outbox / "photos/2026/06/PXL_20260615_101500_phone-1.jpg", target)

        result = run_flush(config, spool)

        assert len(result.already_there) == 1
        assert not result.conflicts
        assert outbox_files(spool) == set()

    def test_different_content_is_triaged_never_overwritten(self, wired) -> None:
        """Case three of D5 — the case --ignore-existing would silently discard."""
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)

        target = nas / "photos/2026/06/PXL_20260615_101500_phone-1.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"a completely different photo that happens to share a name")
        original = target.read_bytes()

        result = run_flush(config, spool)

        assert len(result.conflicts) == 1
        assert target.read_bytes() == original, "an existing file must never be overwritten"
        assert Triage(spool.triage).counts() == {DEST_CONFLICT: 1}
        with Ledger(spool.ledger_path) as ledger:
            assert ledger.count_in_state(State.TRIAGED) == 1


class TestVerification:
    def test_corrupted_delivery_is_detected_and_not_left_in_place(
        self, wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated copy over SMB must not be accepted as delivered."""
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 4096})
        pump(config, spool)

        real_copy = shutil.copy2

        def truncating_copy(src, dst, *args, **kwargs):
            real_copy(src, dst, *args, **kwargs)
            Path(dst).write_bytes(Path(dst).read_bytes()[:10])  # arrives short

        monkeypatch.setattr("photocopier.flush.shutil.copy2", truncating_copy)

        result = run_flush(config, spool)

        assert len(result.failed) == 1
        assert "verification failed" in result.failed[0][1]
        assert nas_files(nas) == set(), "nothing half-written may remain at the destination"
        assert outbox_files(spool) != set(), "the file stays staged for a retry"

    def test_failed_delivery_is_retried_next_run(self, wired, monkeypatch) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 4096})
        pump(config, spool)

        real_copy = shutil.copy2
        attempts = {"n": 0}

        def flaky_copy(src, dst, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("network went away mid-transfer")
            return real_copy(src, dst, *args, **kwargs)

        # Not monkeypatch.undo() between the runs: that would also revert the mount
        # patch this fixture installs, and the test would fail on the guard instead.
        monkeypatch.setattr("photocopier.flush.shutil.copy2", flaky_copy)

        assert len(run_flush(config, spool).failed) == 1
        second = run_flush(config, spool)
        assert len(second.delivered) == 1
        assert nas_files(nas) == {"photos/2026/06/PXL_20260615_101500_phone-1.jpg"}


class TestTheRegression:
    """The failure the ledger exists to prevent, now through a real delivery.

    Phase 1 could only simulate this by deleting the spool. With flush implemented the
    full cycle can be exercised: ingest, process, deliver, and then ingest again. The
    spool is legitimately empty at that point, and rclone's own source-vs-destination
    comparison would call the entire library new. See D1.
    """

    def test_flush_then_ingest_does_not_redownload(self, wired) -> None:
        config, spool, nas, remote = wired
        populate(
            remote / CAMERA_ROLL,
            {
                "2026/06/PXL_20260615_101500.jpg": 128,
                "2026/06/PXL_20260616_101500.jpg": 256,
                "2026/06/PXL_20260617_101500.mp4": 512,
            },
        )
        pump(config, spool)
        assert len(run_flush(config, spool).delivered) == 3
        assert outbox_files(spool) == set()

        with Ledger(spool.ledger_path) as ledger:
            again = ingest(config, Rclone(config.rclone), ledger, spool)

        assert again.total_ingested == 0, "an emptied spool must not look like a fresh start"
        assert again.sources[0].already_known == 3
        assert len(nas_files(nas)) == 3

    def test_the_whole_cycle_is_idempotent(self, wired) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})

        for _ in range(3):
            pump(config, spool)
            run_flush(config, spool)

        assert len(nas_files(nas)) == 1
        assert outbox_files(spool) == set()


class TestDryRun:
    def test_reports_without_delivering(self, wired) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)

        result = run_flush(config, spool, dry_run=True)

        assert len(result.delivered) == 1
        assert nas_files(nas) == set()
        assert outbox_files(spool) != set()

    def test_real_run_still_works_afterwards(self, wired) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)
        run_flush(config, spool, dry_run=True)

        assert len(run_flush(config, spool).delivered) == 1


class TestNothingStaged:
    def test_empty_outbox_is_not_an_error(self, wired) -> None:
        config, spool, nas, remote = wired
        result = run_flush(config, spool)
        assert result.total_seen == 0

    def test_vanished_outbox_file_is_reported(self, wired) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260615_101500.jpg": 128})
        pump(config, spool)
        (spool.outbox / "photos/2026/06/PXL_20260615_101500_phone-1.jpg").unlink()

        result = run_flush(config, spool)
        assert len(result.missing) == 1


PHOTO = "2026/06/PXL_20260615_101500.jpg"
STAGED = "photos/2026/06/PXL_20260615_101500_phone-1.jpg"


class SimulatedSigkill(BaseException):
    pass


def kill_on_call(
    monkeypatch: pytest.MonkeyPatch, owner, name: str, *, nth: int = 1, after: bool = False
) -> None:
    original = getattr(owner, name)
    calls = []

    def dying(*args, **kwargs):
        calls.append(name)
        dies = len(calls) == nth
        if dies and not after:
            raise SimulatedSigkill(name)
        result = original(*args, **kwargs)
        if dies:
            raise SimulatedSigkill(name)
        return result

    monkeypatch.setattr(owner, name, dying)


def scheduled_pass(config, spool: Spool):
    pump(config, spool)
    return run_flush(config, spool)


def ledger_states(spool: Spool) -> list[str]:
    with Ledger(spool.ledger_path) as ledger:
        return [row["state"] for row in ledger.conn.execute("SELECT state FROM items ORDER BY id")]


class TestKilledFlush:
    def test_kill_before_the_ledger_write_converges(self, wired, monkeypatch) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {PHOTO: 4096})
        pump(config, spool)

        kill_on_call(monkeypatch, Ledger, "mark_delivered")
        with pytest.raises(SimulatedSigkill):
            run_flush(config, spool)
        flushes = [scheduled_pass(config, spool) for _ in range(3)]

        assert ledger_states(spool) == [State.DELIVERED]
        assert [len(f.missing) for f in flushes] == [0, 0, 0]

    def test_kill_between_ledger_write_and_outbox_delete_converges(
        self, wired, monkeypatch
    ) -> None:
        config, spool, nas, remote = wired
        write_file(remote / CAMERA_ROLL / PHOTO, b"P" * 4096)
        pump(config, spool)

        kill_on_call(monkeypatch, Ledger, "mark_delivered", after=True)
        with pytest.raises(SimulatedSigkill):
            run_flush(config, spool)
        flushes = [scheduled_pass(config, spool) for _ in range(2)]

        assert (nas / STAGED).read_bytes() == b"P" * 4096
        assert ledger_states(spool) == [State.DELIVERED]
        assert outbox_files(spool) == set()
        assert [len(f.already_there) for f in flushes] == [1, 0]

    @pytest.mark.parametrize("nas_copy", [None, b"a different photo"])
    def test_stray_outbox_copy_stays_unless_the_nas_holds_it(
        self, wired, monkeypatch, nas_copy: bytes | None
    ) -> None:
        config, spool, nas, remote = wired
        write_file(remote / CAMERA_ROLL / PHOTO, b"P" * 4096)
        pump(config, spool)
        kill_on_call(monkeypatch, Ledger, "mark_delivered", after=True)
        with pytest.raises(SimulatedSigkill):
            run_flush(config, spool)
        if nas_copy is None:
            (nas / STAGED).unlink()
        else:
            (nas / STAGED).write_bytes(nas_copy)

        scheduled_pass(config, spool)

        assert (spool.outbox / STAGED).read_bytes() == b"P" * 4096

    def test_kill_after_copy_before_spool_delete_converges(self, wired, monkeypatch) -> None:
        import photocopier.flush as flush_module

        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {PHOTO: 4096})
        original = (remote / CAMERA_ROLL / PHOTO).read_bytes()
        pump(config, spool)

        real_deliver = flush_module.deliver_file

        def deliver_then_die(source, target):
            real_deliver(source, target)
            raise SimulatedSigkill("after deliver_file")

        monkeypatch.setattr(flush_module, "deliver_file", deliver_then_die)
        with pytest.raises(SimulatedSigkill):
            run_flush(config, spool)
        monkeypatch.setattr(flush_module, "deliver_file", real_deliver)

        second = scheduled_pass(config, spool)

        assert (nas / STAGED).read_bytes() == original
        assert len(second.already_there) == 1 and not second.missing
        assert ledger_states(spool) == [State.DELIVERED]
        assert outbox_files(spool) == set()

    def test_kill_while_triaging_a_conflict_still_lists_it(self, wired, monkeypatch) -> None:
        config, spool, nas, remote = wired
        populate(remote / CAMERA_ROLL, {PHOTO: 4096})
        original = (remote / CAMERA_ROLL / PHOTO).read_bytes()
        pump(config, spool)
        write_file(nas / STAGED, b"a different photo already filed by hand")

        kill_on_call(monkeypatch, Ledger, "mark_triaged")
        with pytest.raises(SimulatedSigkill):
            run_flush(config, spool)
        flushes = [scheduled_pass(config, spool) for _ in range(2)]

        assert [len(f.missing) for f in flushes] == [0, 0]
        assert (nas / STAGED).read_bytes() == b"a different photo already filed by hand"
        with Ledger(spool.ledger_path) as ledger:
            listed = [p for p in pending(ledger, Triage(spool.triage)) if p.present]
        assert [p.item.src_path for p in listed] == [PHOTO]
        assert listed[0].path.read_bytes() == original
        assert "different content already at" in render_pending(listed, spool.triage)


class TestKilledWhileTriagingAConflict:
    OWN = b"A" * 4096
    LATER = "2026/06/b/PXL_20260615_101500.jpg"

    def kill_while_triaging(self, wired, monkeypatch, *, on_the_nas: bytes) -> None:
        config, spool, nas, remote = wired
        write_file(remote / CAMERA_ROLL / PHOTO, self.OWN)
        pump(config, spool)
        write_file(nas / STAGED, on_the_nas)
        original = Ledger.mark_triaged
        kill_on_call(monkeypatch, Ledger, "mark_triaged")
        with pytest.raises(SimulatedSigkill):
            run_flush(config, spool)
        monkeypatch.setattr(Ledger, "mark_triaged", original)

    def listed(self, spool: Spool) -> dict[str, bytes]:
        with Ledger(spool.ledger_path) as ledger:
            return {
                p.item.src_path: p.path.read_bytes()
                for p in pending(ledger, Triage(spool.triage))
                if p.present
            }

    def states(self, spool: Spool) -> dict[str, str]:
        with Ledger(spool.ledger_path) as ledger:
            rows = ledger.conn.execute("SELECT src_path, state FROM items")
            return {row["src_path"]: row["state"] for row in rows}

    def test_the_row_is_listed_without_another_pass(self, wired, monkeypatch) -> None:
        _config, spool, _nas, _remote = wired
        self.kill_while_triaging(wired, monkeypatch, on_the_nas=b"Y" * 4096)

        assert self.listed(spool) == {PHOTO: self.OWN}

    def test_a_later_photo_for_the_same_destination_keeps_the_row_on_its_own_bytes(
        self, wired, monkeypatch
    ) -> None:
        config, spool, nas, remote = wired
        self.kill_while_triaging(wired, monkeypatch, on_the_nas=b"Y" * 4096)
        write_file(remote / CAMERA_ROLL / self.LATER, b"Z" * 4096)
        pump(config, spool)
        run_flush(config, spool)

        assert self.listed(spool) == {PHOTO: self.OWN, self.LATER: b"Z" * 4096}
        assert (nas / STAGED).read_bytes() == b"Y" * 4096

    def test_a_later_copy_of_the_nas_file_is_not_delivered_in_its_place(
        self, wired, monkeypatch
    ) -> None:
        config, spool, nas, remote = wired
        self.kill_while_triaging(wired, monkeypatch, on_the_nas=b"Z" * 4096)
        write_file(remote / CAMERA_ROLL / self.LATER, b"Z" * 4096)
        pump(config, spool)
        run_flush(config, spool)
        run_flush(config, spool)

        assert self.states(spool) == {PHOTO: State.TRIAGED, self.LATER: State.DELIVERED}
        assert self.listed(spool) == {PHOTO: self.OWN}
        assert (nas / STAGED).read_bytes() == b"Z" * 4096


class TestIdenticalCopies:
    A = "2026/06/a/PXL_20260615_101500.jpg"
    B = "2026/06/b/PXL_20260615_101500.jpg"

    def upload_twice(self, remote: Path, content: bytes) -> None:
        write_file(remote / CAMERA_ROLL / self.A, content)
        write_file(remote / CAMERA_ROLL / self.B, content)

    def test_identical_copies_at_two_paths_converge(self, wired) -> None:
        config, spool, nas, remote = wired
        self.upload_twice(remote, b"C" * 2500)

        flushes = [scheduled_pass(config, spool) for _ in range(3)]

        assert nas_files(nas) == {STAGED}
        assert (nas / STAGED).read_bytes() == b"C" * 2500
        assert ledger_states(spool) == [State.DELIVERED] * 2
        assert [len(f.missing) for f in flushes] == [0, 0, 0]

    def test_kill_between_the_two_ledger_writes_converges(self, wired, monkeypatch) -> None:
        config, spool, nas, remote = wired
        self.upload_twice(remote, b"C" * 2500)
        pump(config, spool)

        kill_on_call(monkeypatch, Ledger, "mark_delivered", nth=2)
        with pytest.raises(SimulatedSigkill):
            run_flush(config, spool)
        last = scheduled_pass(config, spool)

        assert ledger_states(spool) == [State.DELIVERED] * 2
        assert not last.missing
        assert outbox_files(spool) == set()

    def test_kill_between_the_two_triage_writes_lists_both(self, wired, monkeypatch) -> None:
        config, spool, nas, remote = wired
        self.upload_twice(remote, b"C" * 2500)
        pump(config, spool)
        write_file(nas / STAGED, b"a different photo filed by hand")

        kill_on_call(monkeypatch, Ledger, "mark_triaged", nth=2)
        with pytest.raises(SimulatedSigkill):
            run_flush(config, spool)
        last = scheduled_pass(config, spool)

        assert not last.missing
        assert ledger_states(spool) == [State.TRIAGED] * 2
        with Ledger(spool.ledger_path) as ledger:
            listed = pending(ledger, Triage(spool.triage))
        assert sorted(p.item.src_path for p in listed) == [self.A, self.B]
        assert all(p.present and p.path.read_bytes() == b"C" * 2500 for p in listed)

    def test_both_go_to_triage_on_a_destination_conflict(self, wired) -> None:
        config, spool, nas, remote = wired
        self.upload_twice(remote, b"C" * 2500)
        pump(config, spool)
        write_file(nas / STAGED, b"a different photo filed by hand")

        result = run_flush(config, spool)

        assert (nas / STAGED).read_bytes() == b"a different photo filed by hand"
        assert len(result.conflicts) == 2 and not result.missing
        assert ledger_states(spool) == [State.TRIAGED] * 2
        with Ledger(spool.ledger_path) as ledger:
            listed = sorted(p.item.src_path for p in pending(ledger, Triage(spool.triage)))
        assert listed == [self.A, self.B]


    def test_each_triaged_copy_is_checked_on_resolve(self, wired) -> None:
        config, spool, nas, remote = wired
        self.upload_twice(remote, b"C" * 2500)
        pump(config, spool)
        write_file(nas / STAGED, b"a different photo filed by hand")
        run_flush(config, spool)
        same = write_file(nas / "photos/2026/06/by_hand_c.jpg", b"C" * 2500)
        other = write_file(nas / "photos/2026/06/by_hand_d.jpg", b"D" * 2500)

        with Ledger(spool.ledger_path) as ledger:
            ids = {i.src_path: i.id for i in ledger.items_in_state(State.TRIAGED)}
            resolve_filed(config, ledger, spool, ids[self.A], str(same))
            with pytest.raises(GuardError):
                resolve_filed(config, ledger, spool, ids[self.B], str(other))
            resolve_filed(config, ledger, spool, ids[self.B], str(same))

        assert ledger_states(spool) == [State.DELIVERED] * 2


def test_fixture_photo_survives_the_full_pipeline(wired) -> None:
    """A real JPEG, filed by EXIF, delivered and byte-identical at the far end."""
    config, spool, nas, remote = wired
    target = remote / CAMERA_ROLL / "2026/11/PXL_20261101_021009424.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / "PXL_20261101_021009424.jpg", target)

    pump(config, spool)
    run_flush(config, spool)

    landed = nas / "photos/2026/10/PXL_20261101_021009424_phone-1.jpg"
    assert landed.is_file(), "EXIF says 31 October; filename and folder both say November"
    assert landed.read_bytes() == (FIXTURES / "PXL_20261101_021009424.jpg").read_bytes()

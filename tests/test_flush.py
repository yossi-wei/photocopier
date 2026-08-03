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
from photocopier.spool import Spool
from photocopier.triage import DEST_CONFLICT, Triage

from .conftest import FIXTURES, make_config, populate

CAMERA_ROLL = "Camera Roll"


@pytest.fixture
def nas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the mounted share, with the marker file present.

    os.path.ismount is patched because a temp directory is not a real mount point. The
    guard's behaviour when that check *fails* is tested separately and unpatched — that
    is the case that matters.
    """
    root = tmp_path / "nas"
    (root / "photos").mkdir(parents=True)
    (root / "video").mkdir(parents=True)
    (root / ".photocopier-marker").touch()

    import os

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

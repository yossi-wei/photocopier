"""Stage 2 end to end: incoming/ -> outbox/ + triage/."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from photocopier.ledger import Ledger, State
from photocopier.process import process
from photocopier.rclone import Rclone
from photocopier.spool import Spool
from photocopier.triage import DEST_CONFLICT, NO_DATE, UNKNOWN_TYPE, Triage

from .conftest import FIXTURES, make_config, populate

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

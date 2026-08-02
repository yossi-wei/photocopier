"""Stage 1 end to end, against the stub rclone. No network anywhere in here."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from photocopier.ingest import ingest, is_junk, select_within_budget
from photocopier.ledger import Ledger, State
from photocopier.rclone import Rclone, RemoteItem
from photocopier.spool import Spool

from .conftest import make_config, populate

CAMERA_ROLL = "Camera Roll"


def run_ingest(config, spool: Spool, *, dry_run: bool = False, only: str | None = None):
    rclone = Rclone(config.rclone)
    with Ledger(spool.ledger_path) as ledger:
        return ingest(config, rclone, ledger, spool, dry_run=dry_run, only=only)


def spool_files(spool: Spool) -> set[str]:
    incoming = spool.incoming
    return {
        str(path.relative_to(incoming))
        for path in incoming.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def loaded_remote(remote_root: Path) -> Path:
    populate(
        remote_root / CAMERA_ROLL,
        {
            "2026/06/PXL_20260601_112702768.jpg": 1024,
            "2026/06/PXL_20260601_222815463.jpg": 2048,
            "2026/07/PXL_20260702_090000.jpg": 512,
            "2026/07/PXL_20260702_091500.mp4": 4096,
        },
    )
    return remote_root


class TestBasicIngest:
    def test_downloads_everything_on_first_run(
        self, config, spool: Spool, loaded_remote: Path
    ) -> None:
        result = run_ingest(config, spool)

        assert result.total_ingested == 4
        assert result.total_bytes == 1024 + 2048 + 512 + 4096
        assert spool_files(spool) == {
            "phone-1/2026/06/PXL_20260601_112702768.jpg",
            "phone-1/2026/06/PXL_20260601_222815463.jpg",
            "phone-1/2026/07/PXL_20260702_090000.jpg",
            "phone-1/2026/07/PXL_20260702_091500.mp4",
        }

    def test_source_paths_are_preserved_in_the_spool(
        self, config, spool: Spool, loaded_remote: Path
    ) -> None:
        """Stage 2 uses the YYYY/MM source path as a date fallback, so it must survive."""
        run_ingest(config, spool)
        assert (spool.incoming / "phone-1" / "2026" / "06").is_dir()

    def test_second_run_downloads_nothing(self, config, spool: Spool, loaded_remote: Path) -> None:
        run_ingest(config, spool)
        second = run_ingest(config, spool)

        assert second.total_ingested == 0
        assert second.sources[0].already_known == 4

    def test_only_new_files_are_fetched(self, config, spool: Spool, loaded_remote: Path) -> None:
        run_ingest(config, spool)
        populate(loaded_remote / CAMERA_ROLL, {"2026/07/PXL_20260703_101010.jpg": 256})

        second = run_ingest(config, spool)
        assert second.total_ingested == 1
        assert second.sources[0].ingested[0].name == "PXL_20260703_101010.jpg"

    def test_ledger_records_ingested_state(self, config, spool: Spool, loaded_remote: Path) -> None:
        run_ingest(config, spool)
        with Ledger(spool.ledger_path) as ledger:
            assert ledger.count_in_state(State.INGESTED) == 4


class TestEmptiedSpoolRegression:
    """The failure the ledger exists to prevent — see D1.

    Delivery empties the spool. If "what is new" were decided by comparing source
    against destination, as `rclone copy` does by default, the next run would see an
    empty destination and re-download the entire library. Every day, forever.

    This is the phase-1 form of the test; once `flush` lands in phase 3 it is repeated
    against a real delivery.
    """

    def test_emptied_spool_does_not_trigger_redownload(
        self, config, spool: Spool, loaded_remote: Path
    ) -> None:
        first = run_ingest(config, spool)
        assert first.total_ingested == 4

        # Simulate delivery: the files leave the spool, the ledger stays.
        shutil.rmtree(spool.incoming)
        spool.ensure()
        assert spool_files(spool) == set()

        second = run_ingest(config, spool)

        assert second.total_ingested == 0, "an emptied spool must not look like a fresh start"
        assert second.sources[0].already_known == 4
        assert spool_files(spool) == set()

    def test_deleting_the_ledger_does_trigger_redownload(
        self, config, spool: Spool, loaded_remote: Path
    ) -> None:
        """The converse, stated explicitly: the ledger is the memory, not the spool."""
        run_ingest(config, spool)
        shutil.rmtree(spool.incoming)
        spool.ledger_path.unlink()
        spool.ensure()

        assert run_ingest(config, spool).total_ingested == 4


class TestCutoff:
    def test_material_before_cutoff_is_skipped(self, spool_root: Path, remote_root: Path) -> None:
        config = make_config(
            spool_root,
            sources=[
                {"id": "phone-1", "path": CAMERA_ROLL, "suffix": "phone-1", "cutoff": "2026-06-01"}
            ],
        )
        populate(
            remote_root / CAMERA_ROLL,
            {
                "2026/05/PXL_20260531_120000.jpg": 100,
                "2026/06/PXL_20260601_120000.jpg": 100,
                "2026/07/PXL_20260702_120000.jpg": 100,
            },
        )
        spool = Spool.from_config(config.spool)
        spool.ensure()

        result = run_ingest(config, spool)

        assert result.total_ingested == 2
        assert result.sources[0].skipped_cutoff == 1
        assert spool_files(spool) == {
            "phone-1/2026/06/PXL_20260601_120000.jpg",
            "phone-1/2026/07/PXL_20260702_120000.jpg",
        }

    def test_undatable_files_survive_the_cutoff(self, spool_root: Path, remote_root: Path) -> None:
        """They come down and reach triage, rather than being silently skipped forever."""
        config = make_config(
            spool_root,
            sources=[
                {"id": "phone-1", "path": CAMERA_ROLL, "suffix": "phone-1", "cutoff": "2026-06-01"}
            ],
        )
        populate(remote_root / CAMERA_ROLL, {"1969/1J1A3188.jpg": 100})
        spool = Spool.from_config(config.spool)
        spool.ensure()

        result = run_ingest(config, spool)

        assert result.total_ingested == 1
        assert result.sources[0].skipped_cutoff == 0


class TestJunk:
    @pytest.mark.parametrize("name", [".DS_Store", "Thumbs.db", "desktop.ini", ".hidden"])
    def test_detritus_is_recognised(self, name: str) -> None:
        assert is_junk(RemoteItem(path=name, name=name, size=1, mod_time="", hash="h"))

    def test_photos_are_not_junk(self) -> None:
        item = RemoteItem(path="a/PXL.jpg", name="PXL.jpg", size=1, mod_time="", hash="h")
        assert not is_junk(item)

    def test_junk_is_never_transferred(self, config, spool: Spool, remote_root: Path) -> None:
        populate(
            remote_root / CAMERA_ROLL,
            {
                "2026/06/.DS_Store": 10,
                "2026/06/Thumbs.db": 10,
                "2026/06/PXL_20260601_120000.jpg": 100,
            },
        )
        result = run_ingest(config, spool)

        assert result.total_ingested == 1
        assert result.sources[0].skipped_junk == 2
        assert spool_files(spool) == {"phone-1/2026/06/PXL_20260601_120000.jpg"}


class TestSpoolCap:
    def test_budget_selection_stops_at_the_limit(self) -> None:
        items = [
            RemoteItem(path=f"{i}.jpg", name=f"{i}.jpg", size=100, mod_time="", hash=str(i))
            for i in range(5)
        ]
        selected, capped = select_within_budget(items, budget=250)
        assert len(selected) == 2
        assert capped

    def test_budget_selection_takes_everything_when_it_fits(self) -> None:
        items = [RemoteItem(path="a.jpg", name="a.jpg", size=100, mod_time="", hash="a")]
        selected, capped = select_within_budget(items, budget=1000)
        assert len(selected) == 1
        assert not capped

    def test_ingest_stops_at_the_cap_without_deleting(
        self, spool_root: Path, remote_root: Path
    ) -> None:
        config = make_config(spool_root, cap_gb=1)
        spool = Spool.from_config(config.spool)
        spool.ensure()

        # Two files, each 60% of a 1 GB cap.
        big = int(1024**3 * 0.6)
        populate(
            remote_root / CAMERA_ROLL,
            {"2026/06/PXL_20260601_120000.jpg": big, "2026/06/PXL_20260601_130000.jpg": big},
        )

        result = run_ingest(config, spool)

        assert result.total_ingested == 1
        assert result.any_capped
        assert len(spool_files(spool)) == 1

    def test_capped_items_are_retried_later(self, spool_root: Path, remote_root: Path) -> None:
        """Nothing skipped by the cap is recorded, so a later run picks it up."""
        config = make_config(spool_root, cap_gb=1)
        spool = Spool.from_config(config.spool)
        spool.ensure()

        big = int(1024**3 * 0.6)
        populate(
            remote_root / CAMERA_ROLL,
            {"2026/06/a_PXL_20260601_120000.jpg": big, "2026/06/b_PXL_20260601_130000.jpg": big},
        )
        run_ingest(config, spool)

        # Free the spool, as delivery would.
        shutil.rmtree(spool.incoming)
        spool.ensure()

        second = run_ingest(config, spool)
        assert second.total_ingested == 1


class TestDryRun:
    def test_reports_without_transferring(self, config, spool: Spool, loaded_remote: Path) -> None:
        result = run_ingest(config, spool, dry_run=True)

        assert result.total_selected == 4
        assert result.total_ingested == 0
        assert spool_files(spool) == set()

    def test_leaves_the_ledger_untouched(self, config, spool: Spool, loaded_remote: Path) -> None:
        """A dry run must not make the real run think the work is done."""
        run_ingest(config, spool, dry_run=True)
        assert run_ingest(config, spool).total_ingested == 4


class TestMultipleSources:
    @pytest.fixture
    def two_sources(self, spool_root: Path, remote_root: Path):
        config = make_config(
            spool_root,
            sources=[
                {"id": "phone-1", "path": CAMERA_ROLL, "suffix": "phone-1"},
                {"id": "phone-2", "path": "Pictures/Camera Roll", "suffix": "phone-2"},
            ],
        )
        populate(remote_root / CAMERA_ROLL, {"2026/06/PXL_20260601_120000.jpg": 100})
        populate(remote_root / "Pictures/Camera Roll", {"2026/06/PXL_20260601_120000.jpg": 200})
        spool = Spool.from_config(config.spool)
        spool.ensure()
        return config, spool

    def test_both_sources_are_ingested(self, two_sources) -> None:
        config, spool = two_sources
        result = run_ingest(config, spool)

        assert result.total_ingested == 2
        assert spool_files(spool) == {
            "phone-1/2026/06/PXL_20260601_120000.jpg",
            "phone-2/2026/06/PXL_20260601_120000.jpg",
        }

    def test_identical_filenames_do_not_collide(self, two_sources) -> None:
        """Two phones producing the same filename is routine, not an error."""
        config, spool = two_sources
        run_ingest(config, spool)

        first = spool.incoming / "phone-1/2026/06/PXL_20260601_120000.jpg"
        second = spool.incoming / "phone-2/2026/06/PXL_20260601_120000.jpg"
        assert first.stat().st_size == 100
        assert second.stat().st_size == 200

    def test_single_source_can_be_selected(self, two_sources) -> None:
        config, spool = two_sources
        result = run_ingest(config, spool, only="phone-2")

        assert result.total_ingested == 1
        assert spool_files(spool) == {"phone-2/2026/06/PXL_20260601_120000.jpg"}


class TestPartialTransfer:
    def test_landed_files_are_recorded_and_the_rest_retried(
        self, config, spool: Spool, loaded_remote: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transfer that dies midway must not lose the files that made it.

        Bytes land, are size-verified, and only then recorded. Recording before copying
        would mark files done that were never fetched.
        """
        monkeypatch.setenv("FAKE_RCLONE_FAIL_ON", "2026/07/PXL_20260702_090000.jpg")

        first = run_ingest(config, spool)
        assert first.total_ingested == 2  # the two June files landed before the failure
        assert first.total_failed == 2

        monkeypatch.delenv("FAKE_RCLONE_FAIL_ON")
        second = run_ingest(config, spool)

        assert second.total_ingested == 2
        assert len(spool_files(spool)) == 4

    def test_failure_is_reported_as_nonzero_exit(
        self, config, spool: Spool, loaded_remote: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_RCLONE_FAIL_ON", "2026/06/PXL_20260601_112702768.jpg")
        result = run_ingest(config, spool)
        assert result.total_failed > 0


class TestIdempotency:
    def test_running_repeatedly_converges(self, config, spool: Spool, loaded_remote: Path) -> None:
        first = run_ingest(config, spool)
        snapshot = spool_files(spool)

        for _ in range(3):
            later = run_ingest(config, spool)
            assert later.total_ingested == 0

        assert spool_files(spool) == snapshot
        assert first.total_ingested == 4


class TestEmptySource:
    def test_empty_folder_is_not_an_error(self, config, spool: Spool, remote_root: Path) -> None:
        (remote_root / CAMERA_ROLL).mkdir(parents=True)
        result = run_ingest(config, spool)
        assert result.total_ingested == 0

    def test_absent_folder_is_not_an_error(self, config, spool: Spool, remote_root: Path) -> None:
        result = run_ingest(config, spool)
        assert result.total_ingested == 0

"""Stage 1 end to end, against the stub rclone. No network anywhere in here."""

from __future__ import annotations

import shlex
import shutil
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from photocopier.errors import RcloneError
from photocopier.flush import flush
from photocopier.ingest import ingest, is_junk, select_within_budget
from photocopier.ledger import Ledger, State
from photocopier.process import process
from photocopier.rclone import Rclone, RemoteItem
from photocopier.spool import Spool

from .conftest import FAKE_RCLONE, make_config, populate, write_file

CAMERA_ROLL = "Camera Roll"
PHOTO = "2026/06/PXL_20260615_101500.jpg"


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


def ingest_as_main_did(config, spool: Spool, source_id: str) -> None:
    rclone = Rclone(config.rclone)
    [item] = rclone.lsjson(CAMERA_ROLL)
    flat = spool.incoming / source_id
    rclone.copy_files(CAMERA_ROLL, flat, [item.path])
    with Ledger(spool.ledger_path) as ledger:
        ledger.record_ingested(source_id, item, flat / item.path)


def scheduled_pass(config, spool: Spool) -> None:
    run_ingest(config, spool)
    with Ledger(spool.ledger_path) as ledger:
        process(config, ledger, spool)
        flush(config, ledger, spool)


def files_under(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name not in {".photocopier-marker", "manifest.jsonl"}
    }


def in_library_or_triage(spool: Spool, nas: Path) -> set[bytes]:
    return set(files_under(nas).values()) | set(files_under(spool.triage).values())


def where(spool: Spool, nas: Path) -> str:
    with Ledger(spool.ledger_path) as ledger:
        rows = [
            tuple(row)
            for row in ledger.conn.execute(
                "SELECT id, state, src_path, dest_path, spool_path FROM items ORDER BY id"
            )
        ]
    return (
        f"\n  ledger: {rows}"
        f"\n  nas:    {sorted(files_under(nas))}"
        f"\n  triage: {sorted(files_under(spool.triage))}"
        f"\n  incoming: {sorted(files_under(spool.incoming))}"
    )


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



@pytest.mark.usefixtures("eastern")
class TestWindow:
    """`--since` / `--until`: one run narrowed to a date range, to fill a missed month."""

    @pytest.fixture
    def windowed(self, spool_root: Path, remote_root: Path):
        config = make_config(
            spool_root,
            sources=[
                {"id": "phone-1", "path": CAMERA_ROLL, "suffix": "phone-1", "cutoff": "2026-01-01"}
            ],
        )
        populate(
            remote_root / CAMERA_ROLL,
            {
                "2025/06/PXL_20250630_120000.jpg": 100,
                "2025/07/PXL_20250701_120000.jpg": 100,
                "2025/07/PXL_20250731_235959.mp4": 100,
                "2025/08/PXL_20250801_120000.jpg": 100,
                "2026/02/PXL_20260201_120000.jpg": 100,
                "1969/1J1A3188.jpg": 100,
            },
        )
        spool = Spool.from_config(config.spool)
        spool.ensure()
        return config, spool

    def run_window(self, config, spool: Spool, **window):
        rclone = Rclone(config.rclone)
        with Ledger(spool.ledger_path) as ledger:
            return ingest(config, rclone, ledger, spool, **window)

    def test_one_month_before_the_cutoff(self, windowed) -> None:
        config, spool = windowed
        result = self.run_window(config, spool, since=date(2025, 7, 1), until=date(2025, 8, 1))

        assert spool_files(spool) == {
            "phone-1/2025/07/PXL_20250701_120000.jpg",
            "phone-1/2025/07/PXL_20250731_235959.mp4",
            "phone-1/1969/1J1A3188.jpg",
        }
        assert result.sources[0].skipped_cutoff == 1
        assert result.sources[0].skipped_until == 2

    def test_since_replaces_the_cutoff_only_for_that_run(self, windowed) -> None:
        config, spool = windowed
        self.run_window(config, spool, since=date(2025, 7, 1), until=date(2025, 8, 1))

        later = self.run_window(config, spool)
        assert [item.name for item in later.sources[0].ingested] == ["PXL_20260201_120000.jpg"]

    def test_skipped_items_are_not_recorded(self, windowed) -> None:
        """Outside the window is "not this run", never "done": a wider run still fetches it."""
        config, spool = windowed
        self.run_window(config, spool, since=date(2025, 7, 1), until=date(2025, 8, 1))

        wider = self.run_window(config, spool, since=date(2025, 6, 1))
        assert {item.name for item in wider.sources[0].ingested} == {
            "PXL_20250630_120000.jpg",
            "PXL_20250801_120000.jpg",
            "PXL_20260201_120000.jpg",
        }

    def test_until_alone_keeps_the_cutoff(self, windowed) -> None:
        config, spool = windowed
        result = self.run_window(config, spool, until=date(2026, 3, 1))

        assert {item.name for item in result.sources[0].ingested} == {
            "PXL_20260201_120000.jpg",
            "1J1A3188.jpg",
        }
        assert result.sources[0].skipped_cutoff == 4


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


class TestReuploadAtSamePath:
    def test_while_first_version_still_in_incoming_keeps_both(self, wired) -> None:
        config, spool, nas, remote = wired
        v1, v2 = b"A" * 3000, b"B" * 3500
        write_file(remote / CAMERA_ROLL / PHOTO, v1)
        run_ingest(config, spool)
        write_file(remote / CAMERA_ROLL / PHOTO, v2)
        run_ingest(config, spool)

        for _ in range(2):
            scheduled_pass(config, spool)

        assert {v1, v2} <= in_library_or_triage(spool, nas), (
            f"v1 kept={v1 in in_library_or_triage(spool, nas)}, "
            f"v2 kept={v2 in in_library_or_triage(spool, nas)}" + where(spool, nas)
        )

    def test_failed_refetch_over_a_stale_same_size_copy_is_not_recorded(
        self, wired, monkeypatch
    ) -> None:
        config, spool, nas, remote = wired
        v1, v2 = b"A" * 3000, b"B" * 3000
        write_file(remote / CAMERA_ROLL / PHOTO, v1)
        run_ingest(config, spool)
        write_file(remote / CAMERA_ROLL / PHOTO, v2)

        monkeypatch.setenv("FAKE_RCLONE_FAIL_ON", PHOTO)
        failed = run_ingest(config, spool)
        monkeypatch.delenv("FAKE_RCLONE_FAIL_ON")
        assert (failed.total_ingested, failed.total_failed) == (0, 1)
        for _ in range(2):
            scheduled_pass(config, spool)

        assert v2 in in_library_or_triage(spool, nas), (
            f"rclone copy failed, yet ingest recorded {failed.total_ingested} file(s) and "
            f"{failed.total_failed} failure(s); v2 never fetched" + where(spool, nas)
        )


class TestUpgradeFromMain:
    def test_version_ingested_by_main_still_delivers_beside_a_new_one(self, wired) -> None:
        config, spool, nas, remote = wired
        v1, v2 = b"A" * 3000, b"B" * 3000
        write_file(remote / CAMERA_ROLL / PHOTO, v1)
        ingest_as_main_did(config, spool, "phone-1")
        write_file(remote / CAMERA_ROLL / PHOTO, v2)

        scheduled_pass(config, spool)

        assert files_under(nas) == {"photos/2026/06/PXL_20260615_101500_phone-1.jpg": v1}
        assert v2 in in_library_or_triage(spool, nas)
        assert files_under(spool.incoming) == {}


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


FAIL_LSJSON = "FAKE_RCLONE_FAIL_LSJSON"
PHONE_2 = "Pictures/Camera Roll"


@pytest.fixture
def two_phones(spool_root: Path, remote_root: Path):
    config = make_config(
        spool_root,
        sources=[
            {"id": "phone-1", "path": CAMERA_ROLL, "suffix": "phone-1"},
            {"id": "phone-2", "path": PHONE_2, "suffix": "phone-2"},
        ],
    )
    populate(remote_root / CAMERA_ROLL, {"VID_20261116_093000.mp4": 100})
    populate(remote_root / PHONE_2, {"PXL_20261101_021009424.jpg": 100})
    spool = Spool.from_config(config.spool)
    spool.ensure()
    return config, spool


class TestIngestListingFailure:
    def test_one_failed_listing_does_not_stop_the_other_source(
        self, two_phones, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, spool = two_phones
        monkeypatch.setenv(FAIL_LSJSON, CAMERA_ROLL)
        with Ledger(spool.ledger_path) as ledger:
            result = ingest(config, Rclone(config.rclone), ledger, spool)
        assert result.total_ingested == 1, "phone-2 has a new file and should still be ingested"


class TestListingFailureMessage:
    def test_very_long_stderr_is_truncated_in_report(self) -> None:
        err = RcloneError(
            "rclone lsjson failed with exit code 1", returncode=1, stderr="E" * 200_000
        )
        assert len(err.report()) <= 1000, f"report is {len(err.report())} chars"


class TestKillBetweenCopyAndRecord:
    def test_version_landed_but_unrecorded_is_not_orphaned_by_a_reupload(
        self, wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v1 lands in incoming/, the run is killed before the ledger row is written, then the
        phone overwrites the file with v2 before the next pass. v1's bytes must still reach
        the library or triage, never sit untracked in incoming/."""
        config, spool, nas, remote = wired
        v1, v2 = b"A" * 3000, b"B" * 3200
        write_file(remote / CAMERA_ROLL / PHOTO, v1)

        def killed(*_args, **_kwargs):
            raise KeyboardInterrupt("killed between copy and record")

        with monkeypatch.context() as patch:
            patch.setattr(Ledger, "record_ingested", killed)
            with pytest.raises(KeyboardInterrupt):
                run_ingest(config, spool)

        write_file(remote / CAMERA_ROLL / PHOTO, v2)
        for _ in range(2):
            scheduled_pass(config, spool)

        assert {v1, v2} <= in_library_or_triage(spool, nas), where(spool, nas)


def listing_wrapper(tmp_path: Path, source_path: str, action: str) -> Path:
    """An rclone that runs `action` for an lsjson of `source_path` and is the fake otherwise."""
    script = tmp_path / "rclone-wrapper"
    script.write_text(
        "#!/bin/sh\n"
        f'case "$*" in *lsjson*":{source_path}") {action};; esac\n'
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_RCLONE))} "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


class TestListingProblemsStayWithTheirSource:
    @pytest.fixture
    def two_phones(self, spool_root: Path, remote_root: Path):
        def build(binary: Path, list_timeout_s: int = 900):
            config = make_config(
                spool_root,
                sources=[
                    {"id": "phone-1", "path": CAMERA_ROLL, "suffix": "phone-1"},
                    {"id": "phone-2", "path": PHONE_2, "suffix": "phone-2"},
                ],
            )
            rclone = replace(config.rclone, binary=str(binary), list_timeout_s=list_timeout_s)
            config = replace(config, rclone=rclone)
            write_file(remote_root / CAMERA_ROLL / PHOTO, b"1" * 100)
            write_file(remote_root / PHONE_2 / PHOTO, b"2" * 100)
            spool = Spool.from_config(config.spool)
            spool.ensure()
            return config, spool

        return build

    @pytest.mark.parametrize(
        "listing",
        [
            '[{"Name": "x.jpg", "Size": 1, "IsDir": false}]',
            '[{"Path": "x.jpg", "Size": "big"}]',
            '["x.jpg"]',
            '{"Path": "x.jpg"}',
        ],
        ids=["no-path", "bad-size", "not-an-object", "not-a-list"],
    )
    def test_malformed_listing_fails_only_its_own_phone(
        self, two_phones, tmp_path: Path, listing: str
    ) -> None:
        """A malformed entry raised KeyError or ValueError, which aborted the run before the
        other phones were listed."""
        config, spool = two_phones(
            listing_wrapper(tmp_path, CAMERA_ROLL, f"echo {shlex.quote(listing)}; exit 0")
        )
        with Ledger(spool.ledger_path) as ledger:
            result = ingest(config, Rclone(config.rclone), ledger, spool)
        by_id = {s.source_id: s for s in result.sources}
        assert len(by_id["phone-2"].ingested) == 1
        assert "rclone lsjson" in (by_id["phone-1"].listing_error or "")

    def test_hung_listing_times_out_and_the_other_phone_still_ingests(
        self, two_phones, tmp_path: Path
    ) -> None:
        """lsjson ran with no time limit, so a hung OneDrive listing held the run, and the NAS
        lock, until the launcher's six-hour ingest alarm."""
        config, spool = two_phones(
            listing_wrapper(tmp_path, CAMERA_ROLL, "exec sleep 60"), list_timeout_s=1
        )
        start = time.monotonic()
        with Ledger(spool.ledger_path) as ledger:
            result = ingest(config, Rclone(config.rclone), ledger, spool)
        assert time.monotonic() - start < 20
        by_id = {s.source_id: s for s in result.sources}
        assert "timed out after 1s" in (by_id["phone-1"].listing_error or "")
        assert len(by_id["phone-2"].ingested) == 1

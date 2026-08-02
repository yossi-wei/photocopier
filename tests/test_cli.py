"""The CLI surface itself.

Added after a smoke run found an AttributeError in `render()` that every unit test
missed: the pipeline was correct, but nothing exercised the code that reports on it.
Output formatting is part of the product — a run that works but crashes while printing
is still a failed run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photocopier.cli import main
from photocopier.ingest import IngestResult, SourceResult, render
from photocopier.spool import Spool

from .conftest import FAKE_RCLONE, populate

CAMERA_ROLL = "Camera Roll"

CONFIG_TEMPLATE = """
[spool]
path = "{spool}"
cap_gb = {cap_gb}

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
def config_file(tmp_path: Path, spool_root: Path) -> Path:
    nas = tmp_path / "nas"
    nas.mkdir()
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG_TEMPLATE.format(spool=spool_root, nas=nas, rclone=FAKE_RCLONE, cap_gb=20),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def loaded(remote_root: Path) -> Path:
    populate(
        remote_root / CAMERA_ROLL,
        {
            "2026/06/PXL_20260601_112702768.jpg": 1024,
            "2026/06/PXL_20260602_091500.mp4": 4096,
        },
    )
    return remote_root


class TestRender:
    def test_renders_a_real_run(self, spool_root: Path) -> None:
        spool = Spool(root=spool_root, cap_bytes=20 * 1024**3)
        result = IngestResult(sources=[SourceResult(source_id="phone-1", seen=3, already_known=1)])
        output = render(result, spool)

        assert "phone-1" in output
        assert "ingested 0 file(s)" in output
        assert "1 already in ledger" in output

    def test_renders_a_dry_run(self, spool_root: Path) -> None:
        spool = Spool(root=spool_root, cap_bytes=20 * 1024**3)
        result = IngestResult(sources=[SourceResult(source_id="phone-1")], dry_run=True)
        assert "would ingest" in render(result, spool)

    def test_cap_warning_is_loud(self, spool_root: Path) -> None:
        spool = Spool(root=spool_root, cap_bytes=1024)
        result = IngestResult(sources=[SourceResult(source_id="phone-1", capped=True)])
        output = render(result, spool)

        assert "WARNING" in output
        assert "nothing was deleted" in output


class TestDoctor:
    def test_reports_a_healthy_environment(
        self, config_file: Path, remote_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["-c", str(config_file), "doctor"]) == 0
        out = capsys.readouterr().out
        assert "[ok  ] config" in out
        assert "[ok  ] rclone" in out

    def test_unmounted_destination_warns_without_failing(
        self, config_file: Path, remote_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ingest does not need the NAS, so an absent share must not block it.

        Delivery treats the same condition as fatal — see phase 3.
        """
        assert main(["-c", str(config_file), "doctor"]) == 0
        assert "[warn] destination" in capsys.readouterr().out

    def test_missing_config_fails_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["-c", str(tmp_path / "absent.toml"), "doctor"]) == 1
        assert "no config file found" in capsys.readouterr().out


class TestIngestCommand:
    def test_dry_run_reports_without_transferring(
        self, config_file: Path, loaded: Path, spool_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["-c", str(config_file), "ingest", "--dry-run"]) == 0
        out = capsys.readouterr().out

        assert "would ingest 2 file(s)" in out
        assert not (spool_root / "incoming").exists() or not list(
            (spool_root / "incoming").rglob("*.jpg")
        )

    def test_ingest_transfers_and_reports(
        self, config_file: Path, loaded: Path, spool_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["-c", str(config_file), "ingest"]) == 0
        out = capsys.readouterr().out

        assert "ingested 2 file(s)" in out
        assert (spool_root / "incoming/phone-1/2026/06/PXL_20260601_112702768.jpg").is_file()

    def test_second_run_is_quiet(
        self, config_file: Path, loaded: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["-c", str(config_file), "ingest"])
        capsys.readouterr()

        assert main(["-c", str(config_file), "ingest"]) == 0
        assert "ingested 0 file(s)" in capsys.readouterr().out

    def test_unknown_source_is_a_clean_error(
        self, config_file: Path, loaded: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["-c", str(config_file), "ingest", "--source", "nobody"]) == 1
        assert "unknown source" in capsys.readouterr().err

    def test_spool_inside_sync_root_is_refused(
        self, tmp_path: Path, loaded: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        nas = tmp_path / "nas"
        nas.mkdir()
        config = tmp_path / "config.toml"
        config.write_text(
            CONFIG_TEMPLATE.format(
                spool=tmp_path / "OneDrive" / "spool", nas=nas, rclone=FAKE_RCLONE, cap_gb=20
            ),
            encoding="utf-8",
        )
        assert main(["-c", str(config), "ingest"]) == 1
        assert "cloud-sync" in capsys.readouterr().err


class TestStatusCommand:
    def test_before_any_ingest(
        self, config_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["-c", str(config_file), "status"]) == 0
        assert "not created yet" in capsys.readouterr().out

    def test_after_ingest(
        self, config_file: Path, loaded: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["-c", str(config_file), "ingest"])
        capsys.readouterr()

        assert main(["-c", str(config_file), "status"]) == 0
        out = capsys.readouterr().out
        assert "ingested     2" in out
        assert "awaiting processing" in out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "photocopier" in capsys.readouterr().out

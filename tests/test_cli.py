"""The CLI surface itself.

Added after a smoke run found an AttributeError in `render()` that every unit test
missed: the pipeline was correct, but nothing exercised the code that reports on it.
Output formatting is part of the product — a run that works but crashes while printing
is still a failed run.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from photocopier.cli import main
from photocopier.ingest import IngestResult, SourceResult, render
from photocopier.ledger import Ledger
from photocopier.metrics import ENV_SCHEDULED
from photocopier.spool import Spool

from .conftest import FAKE_RCLONE, FIXTURES, populate

CAMERA_ROLL = "Camera Roll"
PHONE_2 = "Pictures/Camera Roll"
PHONE_3 = "Pictures/Phone 3"
FAIL_LSJSON = "FAKE_RCLONE_FAIL_LSJSON"


def write_ingest_config(
    tmp_path: Path, spool_root: Path, sources: list[tuple[str, str]], binary: Path = FAKE_RCLONE
) -> Path:
    """A config with one [[source]] per (id, path) pair, using `binary` as rclone."""
    nas = tmp_path / "nas"
    blocks = "".join(
        f'\n[[source]]\nid = "{source_id}"\npath = "{path}"\nsuffix = "{source_id}"\n'
        for source_id, path in sources
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[spool]
path = "{spool_root}"
cap_gb = 1

[destination]
photos_root = "{nas}/photos"
video_root = "{nas}/video"
mount_point = "{nas}"

[rclone]
remote = "onedrive"
binary = "{binary}"
transfers = 2
{blocks}""",
        encoding="utf-8",
    )
    return config_file
REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "scripts" / "photocopier-run.sh"

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

    @pytest.mark.usefixtures("eastern")
    def test_window_is_reported(
        self, config_file: Path, loaded: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = ["-c", str(config_file), "ingest", "--dry-run"]
        assert main([*args, "--since", "2026-06-01", "--until", "2026-06-02"]) == 0
        out = capsys.readouterr().out

        assert "window: 2026-06-01 to 2026-06-02" in out
        assert "would ingest 1 file(s)" in out
        assert "1 on or after 2026-06-02" in out

    def test_bad_window_is_rejected(
        self, config_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            main(
                ["-c", str(config_file), "ingest", "--since", "2025-08-01", "--until", "2025-07-01"]
            )
        assert "--since must be earlier" in capsys.readouterr().err

    def test_bad_date_is_rejected(
        self, config_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            main(["-c", str(config_file), "ingest", "--until", "July"])
        assert "not an ISO date" in capsys.readouterr().err

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


    def test_reports_the_metrics(
        self, config_file: Path, loaded: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["-c", str(config_file), "ingest"])
        capsys.readouterr()

        assert main(["-c", str(config_file), "status"]) == 0
        out = capsys.readouterr().out
        assert "metrics (PRD §5)" in out
        line = next(line for line in out.splitlines() if "manual interventions" in line)
        assert "1 (1 ingest)" in line

    def test_launcher_can_still_read_the_count(
        self, config_file: Path, loaded: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """scripts/photocopier-run.sh reads the count with awk '/triaged/ {print $2; exit}'."""
        main(["-c", str(config_file), "ingest"])
        capsys.readouterr()
        main(["-c", str(config_file), "status"])

        first = next(line for line in capsys.readouterr().out.splitlines() if "triaged" in line)
        assert first.split()[:2] == ["triaged", "0"]

    def test_names_a_source_quiet_for_longer_than_the_limit(
        self, config_file: Path, loaded: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["-c", str(config_file), "ingest"])
        main(["-c", str(config_file), "status", "--silent-days", "7"])
        assert "WARNING" not in capsys.readouterr().out, "phone-1 uploaded just now"

        uploaded = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds")
        with closing(sqlite3.connect(config_file.parent / "spool" / "ledger.db")) as conn:
            conn.execute("UPDATE items SET src_modtime = ?", (uploaded.replace("+00:00", "Z"),))
            conn.commit()
        assert main(["-c", str(config_file), "status", "--silent-days", "7"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert "WARNING: phone-1 has sent nothing for 10 days." in lines

    def test_names_a_source_that_never_sent_a_file(
        self, config_file: Path, remote_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (remote_root / CAMERA_ROLL).mkdir()
        main(["-c", str(config_file), "ingest"])
        main(["-c", str(config_file), "status"])
        assert "WARNING: phone-1 has never sent a file." in capsys.readouterr().out.splitlines()

    @pytest.mark.parametrize("days", ["0", "-3", "seven", "1.5"])
    def test_silent_days_must_be_a_positive_whole_number(
        self, config_file: Path, days: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            main(["-c", str(config_file), "status", "--silent-days", days])
        assert "a whole number of days, 1 or more" in capsys.readouterr().err


class TestRunRecording:
    def runs(self, spool_root: Path):
        with Ledger(spool_root / "ledger.db") as ledger:
            return ledger.runs()

    def test_each_pipeline_command_is_recorded(
        self, config_file: Path, loaded: Path, spool_root: Path
    ) -> None:
        main(["-c", str(config_file), "ingest"])
        main(["-c", str(config_file), "process", "--dry-run"])
        main(["-c", str(config_file), "status"])

        runs = self.runs(spool_root)
        assert [(r.command, r.dry_run, r.ok) for r in runs] == [
            ("ingest", False, True),
            ("process", True, True),
        ]
        assert not any(r.scheduled for r in runs)

    def test_the_launcher_marks_its_runs(
        self, config_file: Path, loaded: Path, spool_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_SCHEDULED, "1")
        main(["-c", str(config_file), "ingest"])
        assert self.runs(spool_root)[0].scheduled

    def test_a_failing_command_is_recorded_as_failed(
        self, config_file: Path, loaded: Path, spool_root: Path
    ) -> None:
        main(["-c", str(config_file), "ingest"])
        assert main(["-c", str(config_file), "flush"]) == 1  # the NAS is not mounted

        flush_run = self.runs(spool_root)[-1]
        assert (flush_run.command, flush_run.exit_code, flush_run.ok) == ("flush", 1, False)

    def test_no_ledger_is_created_when_the_guard_refuses(
        self, tmp_path: Path, loaded: Path
    ) -> None:
        nas = tmp_path / "nas"
        nas.mkdir()
        spool = tmp_path / "OneDrive" / "spool"
        config = tmp_path / "config.toml"
        config.write_text(
            CONFIG_TEMPLATE.format(spool=spool, nas=nas, rclone=FAKE_RCLONE, cap_gb=20),
            encoding="utf-8",
        )
        assert main(["-c", str(config), "ingest"]) == 1
        assert not (spool / "ledger.db").exists()


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "photocopier" in capsys.readouterr().out


class TestProcessCommand:
    def test_files_and_reports(
        self, config_file: Path, loaded: Path, spool_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["-c", str(config_file), "ingest"])
        capsys.readouterr()

        assert main(["-c", str(config_file), "process"]) == 0
        out = capsys.readouterr().out

        assert "filed 2 file(s)" in out
        assert (spool_root / "outbox/photos/2026/06/PXL_20260601_112702768_phone-1.jpg").is_file()
        assert (spool_root / "outbox/video/2026/PXL_20260602_091500_phone-1.mp4").is_file()

    def test_dry_run_stages_nothing(
        self, config_file: Path, loaded: Path, spool_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["-c", str(config_file), "ingest"])
        capsys.readouterr()

        assert main(["-c", str(config_file), "process", "--dry-run"]) == 0
        assert "would file 2 file(s)" in capsys.readouterr().out
        assert not list((spool_root / "outbox").rglob("*.jpg"))

    def test_nothing_to_do_is_success(
        self, config_file: Path, remote_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["-c", str(config_file), "process"]) == 0
        assert "filed 0 file(s)" in capsys.readouterr().out


class TestValidateCommand:
    def test_reports_agreement(
        self, config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        library = tmp_path / "library" / "2026" / "10"
        library.mkdir(parents=True)
        shutil.copy2(FIXTURES / "PXL_20261101_021009424.jpg", library / "a.jpg")

        assert main(["-c", str(config_file), "validate", str(tmp_path / "library")]) == 0
        out = capsys.readouterr().out
        assert "agree           1" in out
        assert "exif=1" in out

    def test_nonzero_exit_on_disagreement(
        self, config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        library = tmp_path / "library" / "2026" / "11"
        library.mkdir(parents=True)
        shutil.copy2(FIXTURES / "PXL_20261101_021009424.jpg", library / "a.jpg")

        assert main(["-c", str(config_file), "validate", str(tmp_path / "library")]) == 1
        assert "disagreements:" in capsys.readouterr().out

    def test_missing_directory_is_a_clean_error(
        self, config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["-c", str(config_file), "validate", str(tmp_path / "absent")]) == 1
        assert "not a directory" in capsys.readouterr().err

    def share(self, tmp_path: Path) -> Path:
        """A library as the NAS holds it: photos/YYYY/MM and video/YYYY under one share."""
        root = tmp_path / "share"
        photos = root / "photos" / "2026" / "10"
        photos.mkdir(parents=True)
        shutil.copy2(FIXTURES / "PXL_20261101_021009424.jpg", photos / "a.jpg")
        (root / "video" / "2026").mkdir(parents=True)
        return root

    def test_share_root_checks_nothing_and_says_where_to_look(
        self, config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pointed one level too high, validate used to report 0 files checked and exit 0."""
        root = self.share(tmp_path)
        assert main(["-c", str(config_file), "validate", str(root)]) == 1
        err = capsys.readouterr().err
        assert "no dated photos or videos" in err
        assert f"Try {root / 'photos'}, {root / 'video'}" in err

    def test_year_folder_points_one_level_up(
        self, config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = self.share(tmp_path)
        assert main(["-c", str(config_file), "validate", str(root / "photos" / "2026")]) == 1
        assert f"Try {root / 'photos'}" in capsys.readouterr().err

    def test_empty_folder_fails_without_a_hint(
        self, config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "empty").mkdir()
        assert main(["-c", str(config_file), "validate", str(tmp_path / "empty")]) == 1
        assert "Try" not in capsys.readouterr().err


class TestFlushCommand:
    def test_unmounted_share_fails_the_command_cleanly(
        self, config_file: Path, loaded: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """doctor warns about this; flush must refuse outright.

        The config's mount_point is an ordinary temp directory, exactly like an SMB
        share that has dropped.
        """
        main(["-c", str(config_file), "ingest"])
        main(["-c", str(config_file), "process"])
        capsys.readouterr()

        assert main(["-c", str(config_file), "flush"]) == 1
        assert "not a mount point" in capsys.readouterr().err

    def test_nothing_staged_is_reported_not_an_error(
        self, config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import os

        nas = tmp_path / "nas"  # already created by the config_file fixture
        (nas / ".photocopier-marker").touch()
        real_ismount = os.path.ismount
        monkeypatch.setattr(
            os.path, "ismount", lambda p: True if Path(p) == nas else real_ismount(p)
        )

        assert main(["-c", str(config_file), "flush"]) == 0
        assert "nothing staged" in capsys.readouterr().out


class TestIngestFailureReporting:
    def test_failure_message_names_the_source_and_keeps_rclone_stderr(
        self,
        spool_root: Path,
        remote_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        nas = tmp_path / "nas"
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            f"""
[spool]
path = "{spool_root}"
cap_gb = 1

[destination]
photos_root = "{nas}/photos"
video_root = "{nas}/video"
mount_point = "{nas}"

[rclone]
remote = "onedrive"
binary = "{FAKE_RCLONE}"
transfers = 2

[[source]]
id = "phone-1"
path = "{CAMERA_ROLL}"
suffix = "phone-1"
""",
            encoding="utf-8",
        )
        monkeypatch.setenv(FAIL_LSJSON, CAMERA_ROLL)
        populate(remote_root / CAMERA_ROLL, {"VID_20261116_093000.mp4": 100})

        code = main(["-c", str(config_file), "ingest"])

        err = capsys.readouterr().err
        assert code == 1
        assert "phone-1" in err, f"the message must say which source failed, got: {err!r}"
        assert "simulated lsjson failure" in err, f"rclone stderr was dropped, got: {err!r}"

    def test_very_long_stderr_is_truncated_on_the_cli(
        self,
        spool_root: Path,
        remote_root: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        flood = tmp_path / "rclone-flood"
        flood.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = lsjson ]; then head -c 200000 /dev/zero | tr '\\0' E >&2; exit 1; fi\n"
            f"exec {shlex.quote(str(FAKE_RCLONE))} \"$@\"\n",
            encoding="utf-8",
        )
        flood.chmod(0o755)
        config_file = write_ingest_config(
            tmp_path, spool_root, [("phone-1", CAMERA_ROLL)], binary=flood
        )
        populate(remote_root / CAMERA_ROLL, {"VID_20261116_093000.mp4": 100})

        assert main(["-c", str(config_file), "ingest"]) == 1
        err = capsys.readouterr().err
        assert len(err) <= 2000, f"stderr is {len(err)} chars"

    def test_last_line_names_every_failed_source(
        self,
        spool_root: Path,
        remote_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_file = write_ingest_config(
            tmp_path, spool_root, [("phone-1", CAMERA_ROLL), ("phone-2", PHONE_2)]
        )
        monkeypatch.setenv(FAIL_LSJSON, f"{CAMERA_ROLL},{PHONE_2}")
        populate(remote_root / CAMERA_ROLL, {"VID_20261116_093000.mp4": 100})
        populate(remote_root / PHONE_2, {"PXL_20261101_021009424.jpg": 100})

        assert main(["-c", str(config_file), "ingest"]) == 1
        lines = capsys.readouterr().err.splitlines()
        assert lines[-1] == "error: listing failed for phone-1, phone-2", f"got: {lines!r}"


class TestMalformedListing:
    def test_cli_names_the_phone_and_keeps_going(
        self,
        spool_root: Path,
        remote_root: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A malformed listing used to escape as a traceback with nothing ingested."""
        wrapper = tmp_path / "rclone-malformed"
        wrapper.write_text(
            "#!/bin/sh\n"
            f'case "$*" in *lsjson*":{CAMERA_ROLL}") '
            """echo '[{"Path": "x.jpg", "Size": "big"}]'; exit 0;; esac\n"""
            f'exec {shlex.quote(str(FAKE_RCLONE))} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        config_file = write_ingest_config(
            tmp_path, spool_root, [("phone-1", CAMERA_ROLL), ("phone-2", PHONE_2)],
            binary=wrapper,
        )
        populate(remote_root / PHONE_2, {"PXL_20261101_021009424.jpg": 100})

        assert main(["-c", str(config_file), "ingest", "--no-progress"]) == 1
        captured = capsys.readouterr()
        assert "phone-2: ingested 1 file(s)" in captured.out, captured.out
        assert "unexpected rclone lsjson entry" in captured.err, captured.err
        assert captured.err.splitlines()[-1] == "error: listing failed for phone-1"


class LauncherSandbox:
    def __init__(self, tmp_path: Path, remote_root: Path, spool_root: Path) -> None:
        self.root = tmp_path
        self.state = tmp_path / "state"
        self.lock = tmp_path / "lock" / "lock"
        self.nas = tmp_path / "nas"
        self.notify_log = tmp_path / "notify.log"
        self.remote_root = remote_root
        self.cfg = tmp_path / "config.toml"
        self.cfg.write_text(
            f"""
[spool]
path = "{spool_root}"
cap_gb = 1

[destination]
photos_root = "{self.nas}/photos"
video_root = "{self.nas}/video"
mount_point = "{self.nas}"

[rclone]
remote = "onedrive"
binary = "{FAKE_RCLONE}"
transfers = 2

[[source]]
id = "phone-1"
path = "{CAMERA_ROLL}"
suffix = "phone-1"

[[source]]
id = "phone-2"
path = "{PHONE_2}"
suffix = "phone-2"
""",
            encoding="utf-8",
        )
        self.spool = spool_root
        self.bin = tmp_path / "photocopier"
        self.bin.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -m photocopier"
            f" -c {shlex.quote(str(self.cfg))} \"$@\"\n",
            encoding="utf-8",
        )
        self.bin.chmod(0o755)
        self.notify = tmp_path / "notify.sh"
        self.notify.write_text(
            "#!/bin/sh\n"
            f'{{ echo "=== SUBJECT: $1"; echo "$2"; }} >> {shlex.quote(str(self.notify_log))}\n',
            encoding="utf-8",
        )
        self.notify.chmod(0o755)
        # The launcher wraps each step in macOS's caffeinate, which Linux runners lack.
        self.shims = tmp_path / "shims"
        self.shims.mkdir()
        (self.shims / "caffeinate").write_text(
            "#!/bin/sh\n"
            'while [ "${1#-}" != "$1" ]; do shift; done\n'
            'exec "$@"\n',
            encoding="utf-8",
        )
        (self.shims / "caffeinate").chmod(0o755)

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": f"{self.shims}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "PYTHONPATH": str(REPO),
            "FAKE_RCLONE_ROOT": str(self.remote_root),
            "PHOTOCOPIER_BIN": str(self.bin),
            "PHOTOCOPIER_CONFIG": str(self.cfg),
            "PHOTOCOPIER_SPOOL": str(self.spool),
            "NAS_MOUNT": str(self.nas),
            "NAS_URL": "",
            "STATE_DIR": str(self.state),
            "NAS_JOBS_LOCK": str(self.lock),
            "NOTIFY_CMD": str(self.notify),
        }
        env.update(extra or {})
        return env

    def run(self, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(LAUNCHER)],
            env=self.env(extra),
            capture_output=True,
            text=True,
            timeout=300,
        )

    def subjects(self) -> list[str]:
        if not self.notify_log.exists():
            return []
        return [
            line.removeprefix("=== SUBJECT: ")
            for line in self.notify_log.read_text(encoding="utf-8").splitlines()
            if line.startswith("=== SUBJECT: ")
        ]

    def bodies(self) -> str:
        return self.notify_log.read_text(encoding="utf-8") if self.notify_log.exists() else ""

    def set_summary_date(self, value: date) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "summary-date").write_text(value.isoformat() + "\n", encoding="utf-8")


@pytest.fixture
def scheduled(tmp_path: Path, remote_root: Path, spool_root: Path) -> LauncherSandbox:
    populate(remote_root / CAMERA_ROLL, {"VID_20261116_093000.mp4": 100})
    populate(remote_root / PHONE_2, {"PXL_20261101_021009424.jpg": 100})
    return LauncherSandbox(tmp_path, remote_root, spool_root)


class TestLauncherFailureEmail:
    def test_failure_email_names_the_source_that_failed(self, scheduled: LauncherSandbox) -> None:
        scheduled.set_summary_date(date.today())
        result = scheduled.run({FAIL_LSJSON: CAMERA_ROLL})
        assert result.returncode == 0
        subjects = scheduled.subjects()
        assert any("failed" in s for s in subjects), f"no failure email was sent: {subjects}"
        assert "phone-1" in scheduled.bodies(), "the failure email never says which source failed"

    def test_failure_line_names_every_failed_source(self, scheduled: LauncherSandbox) -> None:
        with scheduled.cfg.open("a", encoding="utf-8") as handle:
            handle.write(
                f'\n[[source]]\nid = "phone-3"\npath = "{PHONE_3}"\nsuffix = "phone-3"\n'
            )
        populate(scheduled.remote_root / PHONE_3, {"IMG_0001.jpg": 100})
        scheduled.set_summary_date(date.today())
        result = scheduled.run({FAIL_LSJSON: ",".join([CAMERA_ROLL, PHONE_2, PHONE_3])})
        assert result.returncode == 0
        bullet = next(
            (line for line in scheduled.bodies().splitlines() if line.startswith(" - ingest")), ""
        )
        assert bullet, f"no FAILURES bullet for ingest in: {scheduled.bodies()!r}"
        for source in ("phone-1", "phone-2", "phone-3"):
            assert source in bullet, f"{source} missing from FAILURES bullet: {bullet!r}"

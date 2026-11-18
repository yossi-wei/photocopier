"""Reporting of the scheduled launcher, scripts/photocopier-run.sh.

The email is the user's only signal from an unattended run, so a failed send must leave a
trace in the run log and must not suppress the next attempt, and a phone that has gone quiet
must be named in the summary. Each test states what the user
needs. Nothing here touches the real installation: every path lives under tmp_path, and HOME
points there too.
"""

from __future__ import annotations

import os
import re
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from .conftest import FAKE_RCLONE, populate

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "scripts" / "photocopier-run.sh"
CAMERA_ROLL = "Camera Roll"
PHONE_2 = "Pictures/Camera Roll"
VIDEO = "VID_20261116_093000.mp4"
# Makes rclone's copy of this file fail, so ingest reports a failure on every pass.
FAIL_VIDEO_COPY = {"FAKE_RCLONE_FAIL_ON": VIDEO}


class Pass:
    """One scheduled pass of scripts/photocopier-run.sh inside a temp sandbox."""

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
        python, cfg = shlex.quote(sys.executable), shlex.quote(str(self.cfg))
        self.bin.write_text(
            f'#!/bin/sh\nexec {python} -m photocopier -c {cfg} "$@"\n',
            encoding="utf-8",
        )
        self.bin.chmod(0o755)
        self.notify = tmp_path / "notify.sh"
        self.set_notify(ok=True)

    def set_notify(self, ok: bool) -> None:
        tail = "" if ok else "exit 1\n"
        self.notify.write_text(
            "#!/bin/sh\n"
            f'{{ echo "=== SUBJECT: $1"; echo "$2"; }} >> {shlex.quote(str(self.notify_log))}\n'
            + tail,
            encoding="utf-8",
        )
        self.notify.chmod(0o755)

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"],
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

    def run_log(self) -> str:
        logs = sorted(self.state.glob("run-*.log"))
        return "\n".join(path.read_text(encoding="utf-8") for path in logs)

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
def scheduled(tmp_path: Path, remote_root: Path, spool_root: Path) -> Pass:
    populate(remote_root / CAMERA_ROLL, {VIDEO: 100})
    populate(remote_root / PHONE_2, {"PXL_20261101_021009424.jpg": 100})
    return Pass(tmp_path, remote_root, spool_root)


def backdate_source(ledger_path: Path, source_id: str, days: int) -> None:
    """Make a source look as it does after its phone stops uploading: every file delivered,
    the newest ingested `days` ago. Delivered rows keep the undelivered backlog empty."""
    when = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(sqlite3.connect(ledger_path)) as conn:
        conn.execute(
            "UPDATE items SET state = 'delivered', ingested_at = ? WHERE source_id = ?",
            (when, source_id),
        )
        conn.commit()


class TestLauncherFailureReporting:
    def test_failed_email_send_is_written_to_the_run_log(self, scheduled: Pass) -> None:
        scheduled.set_notify(ok=False)
        scheduled.run()
        log = scheduled.run_log()
        assert re.search(r"(email|notif|summary).*(fail|not sent|could not)", log, re.IGNORECASE), (
            "a summary that could not be sent leaves no trace in the run log; "
            f"log was: {log!r}"
        )

    def test_failed_failure_email_is_not_recorded_as_sent(self, scheduled: Pass) -> None:
        scheduled.set_summary_date(date.today())
        scheduled.set_notify(ok=False)
        scheduled.run(FAIL_VIDEO_COPY)
        assert any("failed" in s for s in scheduled.subjects()), (
            "setup error: the failing pass never tried to send a failure email"
        )
        last_failure = scheduled.state / "last-failure"
        assert not last_failure.exists(), (
            "the failure email never left, yet last-failure was written, so the next 24 hours "
            "of identical failures are suppressed"
        )


class TestLauncherSilentSource:
    def test_source_with_no_new_files_for_days_is_not_reported_as_ok(
        self, scheduled: Pass, remote_root: Path
    ) -> None:
        scheduled.run()
        assert scheduled.subjects(), "the first pass of the day should send the summary"
        backdate_source(scheduled.spool / "ledger.db", "phone-2", days=16)
        scheduled.set_summary_date(date.today() - timedelta(days=1))
        stamp = date.today().strftime("%Y%m%d")
        populate(remote_root / CAMERA_ROLL, {f"VID_{stamp}_090000.mp4": 100})
        scheduled.run()
        last = scheduled.subjects()[-1]
        assert "OK" not in last, (
            "phone-2 has sent nothing for 16 days while phone-1 uploads, "
            f"yet the summary says: {last}"
        )

    def test_source_quiet_for_less_than_the_limit_is_not_flagged(
        self, scheduled: Pass
    ) -> None:
        scheduled.run()
        backdate_source(scheduled.spool / "ledger.db", "phone-2", days=2)
        scheduled.set_summary_date(date.today() - timedelta(days=1))
        scheduled.run()
        last = scheduled.subjects()[-1]
        assert last.endswith("OK"), (
            "phone-2 sent its last file 2 days ago, inside the 7 day limit, "
            f"yet the summary says: {last}"
        )

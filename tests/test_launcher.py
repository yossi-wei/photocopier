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
PHONE_3 = "Phone 3/Camera Roll"
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

    def next_day_summary(self, extra: dict[str, str] | None = None) -> str:
        """Run the first pass of a new day and return the email it sent."""
        self.set_summary_date(date.today() - timedelta(days=1))
        mark = len(self.bodies())
        self.run(extra)
        return self.bodies()[mark:]

    def edit_config(self, old: str, new: str) -> None:
        text = self.cfg.read_text(encoding="utf-8")
        assert old in text, f"setup: {old!r} is not in the config"
        self.cfg.write_text(text.replace(old, new), encoding="utf-8")


@pytest.fixture
def scheduled(tmp_path: Path, remote_root: Path, spool_root: Path) -> Pass:
    populate(remote_root / CAMERA_ROLL, {VIDEO: 100})
    populate(remote_root / PHONE_2, {"PXL_20261101_021009424.jpg": 100})
    return Pass(tmp_path, remote_root, spool_root)


def days_ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def backdate_source(ledger_path: Path, source_id: str, days: int) -> None:
    """Make a source look as it does after its phone stops uploading: every file delivered,
    the newest uploaded to OneDrive and ingested `days` ago. Delivered rows keep the
    undelivered backlog empty."""
    when = days_ago(days).isoformat(timespec="seconds")
    with closing(sqlite3.connect(ledger_path)) as conn:
        conn.execute(
            "UPDATE items SET state = 'delivered', src_modtime = ?, ingested_at = ? "
            "WHERE source_id = ?",
            (when.replace("+00:00", "Z"), when, source_id),
        )
        conn.commit()


def add_silent_phone_3(p: Pass, remote_root: Path) -> None:
    """Configure a second phone that will go silent, so a summary that must name a silent
    phone is also checked for naming the wrong one. Call before the first pass."""
    populate(remote_root / PHONE_3, {"IMG_20261101_120000.jpg": 100})
    p.cfg.write_text(
        p.cfg.read_text(encoding="utf-8")
        + f'\n[[source]]\nid = "phone-3"\npath = "{PHONE_3}"\nsuffix = "phone-3"\n',
        encoding="utf-8",
    )


def assert_once_above_status(email: str, line: str) -> None:
    """The warning is a whole line exactly once, above the STATUS block. STATUS pastes status
    output, so a second copy there would show the operator the same warning twice."""
    lines = email.splitlines()
    copies = [i for i, text in enumerate(lines) if text == line]
    assert len(copies) == 1, f"expected {line!r} once, found {len(copies)} time(s):\n{email}"
    assert "STATUS" in lines, f"the summary has no STATUS block:\n{email}"
    assert copies[0] < lines.index("STATUS"), f"{line!r} sits below STATUS:\n{email}"


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
        stamp = date.today().strftime("%Y%m%d")
        populate(remote_root / CAMERA_ROLL, {f"VID_{stamp}_090000.mp4": 100})
        email = scheduled.next_day_summary()
        assert scheduled.subjects()[-1].endswith("needs attention"), email
        assert_once_above_status(email, "WARNING: phone-2 has sent nothing for 16 days.")
        assert "WARNING: phone-1" not in email, f"phone-1 uploaded today, yet:\n{email}"

    def test_source_quiet_for_less_than_the_limit_is_not_flagged(
        self, scheduled: Pass, remote_root: Path
    ) -> None:
        add_silent_phone_3(scheduled, remote_root)
        scheduled.run()
        backdate_source(scheduled.spool / "ledger.db", "phone-2", days=2)
        backdate_source(scheduled.spool / "ledger.db", "phone-3", days=16)
        email = scheduled.next_day_summary()
        assert scheduled.subjects()[-1].endswith("needs attention"), email
        assert "WARNING: phone-2" not in email, (
            "phone-2 sent its last file 2 days ago, inside the 7 day limit, "
            f"yet the summary names it:\n{email}"
        )
        assert_once_above_status(email, "WARNING: phone-3 has sent nothing for 16 days.")

    def test_configured_source_that_never_sent_a_file_is_named(
        self, tmp_path: Path, remote_root: Path, spool_root: Path
    ) -> None:
        populate(remote_root / CAMERA_ROLL, {VIDEO: 100})
        (remote_root / PHONE_2).mkdir(parents=True)
        p = Pass(tmp_path, remote_root, spool_root)
        p.run()
        email = p.bodies()
        assert p.subjects()[-1].endswith("needs attention"), email
        assert_once_above_status(email, "WARNING: phone-2 has never sent a file.")

    def test_malformed_limit_falls_back_to_the_default(self, scheduled: Pass) -> None:
        scheduled.run()
        backdate_source(scheduled.spool / "ledger.db", "phone-2", days=16)
        sent = len(scheduled.subjects())
        email = scheduled.next_day_summary({"SOURCE_SILENT_DAYS": "seven"})
        assert len(scheduled.subjects()) == sent + 1, (
            "SOURCE_SILENT_DAYS=seven stopped the launcher before it sent the daily summary"
        )
        assert_once_above_status(email, "WARNING: phone-2 has sent nothing for 16 days.")
        assert "SOURCE_SILENT_DAYS" in scheduled.run_log(), (
            "the launcher ignored a bad SOURCE_SILENT_DAYS without saying so in the run log"
        )

    def test_source_id_with_a_pipe_is_named(self, scheduled: Pass) -> None:
        scheduled.edit_config('id = "phone-2"', 'id = "pixel|2"')
        scheduled.run()
        backdate_source(scheduled.spool / "ledger.db", "pixel|2", days=16)
        email = scheduled.next_day_summary()
        assert_once_above_status(email, "WARNING: pixel|2 has sent nothing for 16 days.")

    def test_source_removed_from_the_config_is_not_named(
        self, scheduled: Pass, remote_root: Path
    ) -> None:
        add_silent_phone_3(scheduled, remote_root)
        scheduled.run()
        backdate_source(scheduled.spool / "ledger.db", "phone-2", days=16)
        backdate_source(scheduled.spool / "ledger.db", "phone-3", days=16)
        scheduled.edit_config(
            f'[[source]]\nid = "phone-2"\npath = "{PHONE_2}"\nsuffix = "phone-2"\n', ""
        )
        email = scheduled.next_day_summary()
        assert "WARNING: phone-2" not in email, (
            f"phone-2 was removed from config.toml, yet the summary still warns:\n{email}"
        )
        assert_once_above_status(email, "WARNING: phone-3 has sent nothing for 16 days.")

    def test_backfill_of_an_old_file_does_not_hide_silence(
        self, tmp_path: Path, remote_root: Path, spool_root: Path
    ) -> None:
        populate(remote_root / CAMERA_ROLL, {VIDEO: 100})
        recent = f"PXL_{days_ago(1):%Y%m%d}_100000000.jpg"
        old = f"PXL_{days_ago(20):%Y%m%d}_090000000.jpg"
        populate(remote_root / PHONE_2, {recent: 100, old: 100})
        uploaded = days_ago(20).timestamp()
        os.utime(remote_root / PHONE_2 / old, (uploaded, uploaded))
        p = Pass(tmp_path, remote_root, spool_root)
        p.edit_config(
            'suffix = "phone-2"', f'suffix = "phone-2"\ncutoff = "{days_ago(5).date()}"'
        )
        p.run()
        backdate_source(p.spool / "ledger.db", "phone-2", days=16)

        backfill = subprocess.run(
            [str(p.bin), "ingest", "--since", days_ago(30).date().isoformat()],
            env=p.env(), capture_output=True, text=True, timeout=300,
        )
        assert backfill.returncode == 0, backfill.stderr
        assert old in backfill.stdout or "ingested 1 file(s)" in backfill.stdout, (
            f"setup: the backfill should ingest the old file:\n{backfill.stdout}"
        )

        email = p.next_day_summary()
        assert_once_above_status(email, "WARNING: phone-2 has sent nothing for 16 days.")

    def test_silence_is_found_without_the_spool_variable(self, scheduled: Pass) -> None:
        scheduled.run()
        backdate_source(scheduled.spool / "ledger.db", "phone-2", days=16)
        scheduled.set_summary_date(date.today() - timedelta(days=1))
        env = {k: v for k, v in scheduled.env().items() if k != "PHOTOCOPIER_SPOOL"}
        mark = len(scheduled.bodies())
        subprocess.run(["bash", str(LAUNCHER)], env=env, capture_output=True, timeout=300)
        email = scheduled.bodies()[mark:]
        assert_once_above_status(email, "WARNING: phone-2 has sent nothing for 16 days.")

"""The PRD §5 success metrics, as `status` computes them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from photocopier.errors import GuardError
from photocopier.flush import flush
from photocopier.ingest import ingest
from photocopier.ledger import Ledger, Run, State
from photocopier.metrics import (
    Metrics,
    Streak,
    _share,
    compute,
    render,
    streak,
    verify_delivered,
)
from photocopier.process import process
from photocopier.rclone import Rclone

from .conftest import FIXTURES, populate

CAMERA_ROLL = "Camera Roll"
TODAY = date(2026, 11, 10)


def run(
    day: date,
    command: str = "ingest",
    *,
    ok: bool = True,
    hour: int = 5,
    scheduled: bool = True,
    dry_run: bool = False,
    finished: bool = True,
) -> Run:
    started = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
    return Run(
        command=command,
        scheduled=scheduled,
        dry_run=dry_run,
        started_at=started,
        finished_at=started + timedelta(minutes=1) if finished else None,
        exit_code=(0 if ok else 1) if finished else None,
    )


def days_back(n: int) -> date:
    return TODAY - timedelta(days=n)


def daily(first: int, last: int = 0) -> list[Run]:
    """A clean scheduled pass on each day from `first` days ago to `last` days ago."""
    runs: list[Run] = []
    for n in range(first, last - 1, -1):
        runs += [run(days_back(n)), run(days_back(n), "process"), run(days_back(n), "flush")]
    return runs


class TestStreak:
    def test_no_history(self) -> None:
        assert streak([], today=TODAY, tz=UTC) == Streak(0, 0, None)

    def test_consecutive_clean_days(self) -> None:
        result = streak(daily(6), today=TODAY, tz=UTC)
        assert (result.current, result.longest) == (7, 7)
        assert result.recorded_since == days_back(6)

    def test_a_failure_repaired_the_same_day_does_not_break_it(self) -> None:
        """The real pattern: an rclone listing error at 05:00, fine again at 11:00."""
        runs = daily(4) + [run(days_back(2), ok=False, hour=4)]
        assert streak(runs, today=TODAY, tz=UTC).current == 5

    def test_a_step_that_never_succeeds_breaks_it(self) -> None:
        runs = daily(4, 3) + [run(days_back(2), ok=False)] + daily(1)
        result = streak(runs, today=TODAY, tz=UTC)
        assert (result.current, result.longest) == (2, 2)

    def test_a_day_with_no_scheduled_run_breaks_it(self) -> None:
        """A job that quietly stopped is exactly what the streak is for."""
        runs = daily(5, 3) + daily(1)
        result = streak(runs, today=TODAY, tz=UTC)
        assert (result.current, result.longest) == (2, 3)

    def test_a_killed_run_counts_as_a_failure(self) -> None:
        runs = daily(2, 2) + [run(days_back(1), finished=False)] + daily(0)
        result = streak(runs, today=TODAY, tz=UTC)
        assert (result.current, result.longest) == (1, 1)

    def test_today_extends_but_never_breaks(self) -> None:
        assert streak(daily(3, 1), today=TODAY, tz=UTC).current == 3
        assert streak(daily(3, 1) + [run(TODAY, ok=False)], today=TODAY, tz=UTC).current == 3
        assert streak(daily(3), today=TODAY, tz=UTC).current == 4

    def test_skipped_flush_is_normal(self) -> None:
        """NAS away while travelling: no flush attempted, and that is fine (D2)."""
        runs = [r for r in daily(3) if r.command != "flush"]
        assert streak(runs, today=TODAY, tz=UTC).current == 4

    def test_manual_and_dry_runs_do_not_count(self) -> None:
        runs = [run(days_back(1), scheduled=False), run(days_back(2), dry_run=True)]
        assert streak(runs, today=TODAY, tz=UTC) == Streak(0, 0, None)


def pipeline(config, spool) -> None:
    with Ledger(spool.ledger_path) as ledger:
        ingest(config, Rclone(config.rclone), ledger, spool)
        process(config, ledger, spool)
        flush(config, ledger, spool)


@pytest.fixture
def delivered(wired):
    config, spool, nas, remote = wired
    populate(
        remote / CAMERA_ROLL,
        {"2026/06/PXL_20260601_112702768.jpg": 1024, "2026/06/PXL_20260602_091500.mp4": 4096},
    )
    undated = remote / CAMERA_ROLL / "1969" / "1J1A3188.jpg"
    undated.parent.mkdir(parents=True)
    undated.write_bytes((FIXTURES / "1J1A3188.jpg").read_bytes())
    pipeline(config, spool)
    return config, spool, nas


class TestOutcomes:
    def test_counts_from_a_real_pipeline(self, delivered) -> None:
        _config, spool, _nas = delivered
        with Ledger(spool.ledger_path) as ledger:
            out = ledger.outcomes()

        assert (out.delivered, out.delivered_unassisted, out.awaiting_review) == (2, 2, 1)
        assert (out.processed, out.ever_triaged, out.finished) == (3, 1, 3)
        assert out.oldest_undelivered is None

    def test_a_resolved_item_still_counts_as_needing_a_person(self, delivered) -> None:
        _config, spool, _nas = delivered
        with Ledger(spool.ledger_path) as ledger:
            (item,) = ledger.items_in_state(State.TRIAGED)
            ledger.mark_discarded(item.id)
            out = ledger.outcomes()

        assert (out.discarded, out.ever_triaged, out.delivered_unassisted) == (1, 1, 2)

    def test_oldest_undelivered(self, wired) -> None:
        config, spool, _nas, remote = wired
        populate(remote / CAMERA_ROLL, {"2026/06/PXL_20260601_112702768.jpg": 10})
        with Ledger(spool.ledger_path) as ledger:
            ingest(config, Rclone(config.rclone), ledger, spool)
            assert ledger.outcomes().oldest_undelivered is not None


class TestVerify:
    def test_everything_present(self, delivered) -> None:
        config, spool, _nas = delivered
        with Ledger(spool.ledger_path) as ledger:
            result = verify_delivered(config, ledger)
        assert (result.checked, result.ok) == (2, True)

    def test_missing_and_changed_files_are_named(self, delivered) -> None:
        config, spool, nas = delivered
        (nas / "photos/2026/06/PXL_20260601_112702768_phone-1.jpg").unlink()
        (nas / "video/2026/PXL_20260602_091500_phone-1.mp4").write_bytes(b"short")
        with Ledger(spool.ledger_path) as ledger:
            result = verify_delivered(config, ledger)

        assert result.missing == ["photos/2026/06/PXL_20260601_112702768_phone-1.jpg"]
        assert result.changed == ["video/2026/PXL_20260602_091500_phone-1.mp4"]

    def test_refuses_an_unmounted_share(self, delivered) -> None:
        """An empty mount point would make every delivered file look lost."""
        config, spool, nas = delivered
        (nas / ".photocopier-marker").unlink()
        with Ledger(spool.ledger_path) as ledger, pytest.raises(GuardError):
            verify_delivered(config, ledger)


class TestRender:
    def test_every_prd_metric_is_reported(self, delivered) -> None:
        _config, spool, _nas = delivered
        with Ledger(spool.ledger_path) as ledger:
            lines = "\n".join(render(compute(ledger)))

        for label in (
            "delivered without intervention",
            "needed triage",
            "manual interventions, 30 days",
            "unattended streak",
            "oldest undelivered file",
            "lost or overwritten",
        ):
            assert label in lines
        assert "2 of 3 (66.7%)" in lines
        assert "status --verify" in lines

    def test_interventions_and_streak(self, ledger: Ledger) -> None:
        now = datetime(2026, 11, 10, 12, tzinfo=UTC)
        for n in range(3, 0, -1):
            when = now - timedelta(days=n)
            run_id = ledger.start_run("ingest", scheduled=True, dry_run=False, started_at=when)
            ledger.finish_run(run_id, 0)
        manual = ledger.start_run("resolve", scheduled=False, dry_run=False, started_at=now)
        ledger.finish_run(manual, 0)
        ledger.start_run("flush", scheduled=False, dry_run=True, started_at=now)

        metrics = compute(ledger, now=now, tz=UTC)
        assert metrics.interventions == {"resolve": 1}
        assert (metrics.streak.current, metrics.failed_scheduled_runs) == (3, 0)
        assert "1 (1 resolve)" in "\n".join(render(metrics))

    def test_empty_ledger(self, ledger: Ledger) -> None:
        lines = "\n".join(render(compute(ledger)))
        assert "n/a" in lines
        assert "no scheduled runs recorded yet" in lines

    def test_verify_result_is_shown(self, delivered) -> None:
        config, spool, nas = delivered
        (nas / "photos/2026/06/PXL_20260601_112702768_phone-1.jpg").unlink()
        with Ledger(spool.ledger_path) as ledger:
            metrics: Metrics = compute(ledger)
            lines = "\n".join(render(metrics, verify_delivered(config, ledger)))

        assert "1 of 2 delivered" in lines
        assert "missing  photos/2026/06/" in lines


def test_upgrading_an_old_ledger_adds_the_runs_table(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        assert ledger.runs() == []
        version = ledger.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert version == "2"


@pytest.mark.parametrize(
    ("part", "whole", "shown"),
    [
        (1, 4158, "1 of 4,158 (<0.1%)"),
        (4157, 4158, "4,157 of 4,158 (>99.9%)"),
        (4158, 4158, "4,158 of 4,158 (100.0%)"),
        (0, 10, "0 of 10 (0.0%)"),
        (0, 0, "n/a"),
    ],
)
def test_shares_never_round_to_a_false_zero_or_hundred(part: int, whole: int, shown: str) -> None:
    assert _share(part, whole) == shown

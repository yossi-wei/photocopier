"""The PRD's success metrics, computed from the ledger for `photocopier status`.

Each metric here is the one in PRD §5, under the same name, so the document and the tool
cannot drift apart. Two are proxies, and say so: minutes are not measurable, so manual
work is counted as interventions; and there is no single "drain" event, so the spool
backlog is reported as the age of the oldest undelivered file.

The unattended streak counts days on which every step the scheduled job attempted
succeeded at least once. A transient failure that the next run repairs on its own does
not break it; a day with no scheduled run at all does. See D13.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo

from .config import Config
from .flush import destination_root
from .guards import check_mount_live
from .ledger import Ledger, Outcomes, Run, State

# Set by the launcher, so a run can tell whether a person started it.
ENV_SCHEDULED = "PHOTOCOPIER_SCHEDULED"

# Commands recorded in the runs table. Only these can change the library or the ledger.
RECORDED_COMMANDS = ("ingest", "process", "flush", "resolve")

WINDOW_DAYS = 30


@dataclass(frozen=True)
class Streak:
    current: int
    longest: int
    recorded_since: date | None


@dataclass(frozen=True)
class Metrics:
    outcomes: Outcomes
    streak: Streak
    interventions: dict[str, int]
    failed_scheduled_runs: int
    now: datetime

    @property
    def total_interventions(self) -> int:
        return sum(self.interventions.values())


@dataclass
class VerifyResult:
    checked: int = 0
    missing: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.changed


def clean_days(runs: list[Run], tz: tzinfo | None = None) -> tuple[set[date], set[date]]:
    """(days with a scheduled run, days on which each attempted step succeeded at least once)."""
    attempted: dict[date, dict[str, bool]] = defaultdict(dict)
    for run in runs:
        if not run.scheduled or run.dry_run:
            continue
        day = run.started_at.astimezone(tz).date()
        attempted[day][run.command] = attempted[day].get(run.command, False) or run.ok
    clean = {day for day, steps in attempted.items() if all(steps.values())}
    return set(attempted), clean


def streak(runs: list[Run], *, today: date, tz: tzinfo | None = None) -> Streak:
    """Today can extend the current streak but never break it: its runs are not all in."""
    seen, clean = clean_days(runs, tz)
    if not seen:
        return Streak(current=0, longest=0, recorded_since=None)

    first = min(seen)
    longest = run_length = 0
    day = first
    while day <= today:
        if day in clean:
            run_length += 1
            longest = max(longest, run_length)
        elif day != today:
            run_length = 0
        day += timedelta(days=1)
    return Streak(current=run_length, longest=longest, recorded_since=first)


def compute(ledger: Ledger, *, now: datetime | None = None, tz: tzinfo | None = None) -> Metrics:
    now = now or datetime.now(UTC)
    window_start = now - timedelta(days=WINDOW_DAYS)
    recent = ledger.runs(since=window_start)

    interventions: dict[str, int] = defaultdict(int)
    for run in recent:
        if not run.scheduled and not run.dry_run:
            interventions[run.command] += 1

    return Metrics(
        outcomes=ledger.outcomes(),
        streak=streak(ledger.runs(), today=now.astimezone(tz).date(), tz=tz),
        interventions=dict(interventions),
        failed_scheduled_runs=sum(
            1 for run in recent if run.scheduled and not run.dry_run and not run.ok
        ),
        now=now,
    )


def verify_delivered(config: Config, ledger: Ledger) -> VerifyResult:
    """Check every delivered file is still at its destination, at the size it arrived.

    Read-only. Mount-guarded like flush, because an unmounted share's mount point is an
    empty local folder and every file would look lost.
    """
    check_mount_live(config.destination.mount_point, config.destination.mount_marker)
    result = VerifyResult()
    for item in ledger.items_in_state(State.DELIVERED):
        if not item.dest_path:
            continue
        result.checked += 1
        target = destination_root(config, item.dest_path)
        if not target.is_file():
            result.missing.append(item.dest_path)
        elif target.stat().st_size != item.src_size:
            result.changed.append(item.dest_path)
    return result


def render(metrics: Metrics, verified: VerifyResult | None = None) -> list[str]:
    out = metrics.outcomes
    lines = ["metrics (PRD §5)"]

    def row(label: str, value: str, target: str = "") -> None:
        lines.append(f"  {label:<34}{value:<28}{target}".rstrip())

    row(
        "delivered without intervention",
        _share(out.delivered_unassisted, out.finished),
        "target >= 99%",
    )
    row("needed triage", _share(out.ever_triaged, out.processed), "target < 1%")

    if metrics.interventions:
        detail = ", ".join(f"{n} {cmd}" for cmd, n in sorted(metrics.interventions.items()))
        interventions = f"{metrics.total_interventions} ({detail})"
    else:
        interventions = "0"
    row(f"manual interventions, {WINDOW_DAYS} days", interventions, "target 0")

    s = metrics.streak
    if s.recorded_since is None:
        row("unattended streak", "no scheduled runs recorded yet")
    else:
        row(
            "unattended streak",
            f"{_days(s.current)} (longest {_days(s.longest)})",
            f"recorded since {s.recorded_since}",
        )
    row(f"failed scheduled steps, {WINDOW_DAYS} days", str(metrics.failed_scheduled_runs))

    if out.oldest_undelivered is None:
        row("oldest undelivered file", "none")
    else:
        age = metrics.now - out.oldest_undelivered
        row("oldest undelivered file", f"{_age(age)} old")

    if verified is None:
        row("lost or overwritten", "not checked", "status --verify checks the NAS")
    else:
        bad = len(verified.missing) + len(verified.changed)
        row("lost or overwritten", f"{bad} of {verified.checked:,} delivered", "invariant 0")
        for path in verified.missing[:5]:
            lines.append(f"    missing  {path}")
        for path in verified.changed[:5]:
            lines.append(f"    changed  {path}")
    return lines


def _share(part: int, whole: int) -> str:
    if not whole:
        return "n/a"
    percent = 100 * part / whole
    # Rounding must not turn one triaged file into 0.0%, or one miss into 100.0%.
    if 0 < percent < 0.1:
        shown = "<0.1%"
    elif 99.9 < percent < 100:
        shown = ">99.9%"
    else:
        shown = f"{percent:.1f}%"
    return f"{part:,} of {whole:,} ({shown})"


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _age(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"

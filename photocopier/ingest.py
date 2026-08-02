"""Stage 1 — ingest.

Enumerate each configured source, ask the ledger which items are genuinely new, respect
the cutoff and the spool cap, then copy an explicit file list into the spool.

Ordering matters: bytes land first, are verified, and only then are recorded. Recording
before copying would mark a file done that was never fetched — silent data loss. The
reverse ordering costs at most a re-download.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config, SourceConfig
from .dates import is_before_cutoff
from .guards import human_bytes
from .ledger import Ledger
from .rclone import Rclone, RemoteItem
from .spool import Spool

# Filesystem detritus that is never worth transferring.
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r", "Icon"}


@dataclass
class SourceResult:
    source_id: str
    seen: int = 0
    skipped_cutoff: int = 0
    skipped_junk: int = 0
    already_known: int = 0
    selected: list[RemoteItem] = field(default_factory=list)
    ingested: list[RemoteItem] = field(default_factory=list)
    failed: list[tuple[RemoteItem, str]] = field(default_factory=list)
    capped: bool = False

    @property
    def selected_bytes(self) -> int:
        return sum(item.size for item in self.selected)

    @property
    def ingested_bytes(self) -> int:
        return sum(item.size for item in self.ingested)


@dataclass
class IngestResult:
    sources: list[SourceResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def total_selected(self) -> int:
        return sum(len(s.selected) for s in self.sources)

    @property
    def total_ingested(self) -> int:
        return sum(len(s.ingested) for s in self.sources)

    @property
    def total_bytes(self) -> int:
        return sum(s.ingested_bytes for s in self.sources)

    @property
    def selected_bytes(self) -> int:
        return sum(s.selected_bytes for s in self.sources)

    @property
    def any_capped(self) -> bool:
        return any(s.capped for s in self.sources)

    @property
    def total_failed(self) -> int:
        return sum(len(s.failed) for s in self.sources)


def is_junk(item: RemoteItem) -> bool:
    """Skip OS detritus and hidden files. Classification by type is stage 2's job."""
    return item.name in IGNORED_NAMES or item.name.startswith(".")


def select_within_budget(items: list[RemoteItem], budget: int) -> tuple[list[RemoteItem], bool]:
    """Take items in order until the next one would exceed the budget.

    Stops rather than skipping ahead to smaller files: a large video should not be
    starved indefinitely by later small photos. Nothing is ever deleted to make room.
    """
    selected: list[RemoteItem] = []
    remaining = budget
    for item in items:
        if item.size > remaining:
            return selected, True
        selected.append(item)
        remaining -= item.size
    return selected, False


def ingest_source(
    source: SourceConfig,
    config: Config,
    rclone: Rclone,
    ledger: Ledger,
    spool: Spool,
    *,
    budget: int,
    dry_run: bool = False,
) -> SourceResult:
    result = SourceResult(source_id=source.id)

    listing = rclone.lsjson(source.path)
    result.seen = len(listing)

    candidates: list[RemoteItem] = []
    for item in listing:
        if is_junk(item):
            result.skipped_junk += 1
            continue
        if is_before_cutoff(item.path, item.name, source.cutoff):
            result.skipped_cutoff += 1
            continue
        candidates.append(item)

    unseen = ledger.filter_new(source.id, candidates)
    result.already_known = len(candidates) - len(unseen)

    unseen.sort(key=lambda item: item.path)
    result.selected, result.capped = select_within_budget(unseen, budget)

    if dry_run or not result.selected:
        return result

    dest = spool.source_incoming(source.id)
    rel_paths = [item.path for item in result.selected]

    copy_error: str | None = None
    try:
        rclone.copy_files(source.path, dest, rel_paths)
    except Exception as exc:  # noqa: BLE001 - recorded per item below
        # A partial transfer is normal here: whatever landed is still verified and
        # recorded, and the rest is retried on the next run.
        copy_error = str(exc)

    for item in result.selected:
        landed = dest / item.path
        if landed.is_file() and landed.stat().st_size == item.size:
            ledger.record_ingested(source.id, item, landed)
            result.ingested.append(item)
        else:
            reason = copy_error or "file missing or truncated after copy"
            ledger.record_failure(source.id, item, reason)
            result.failed.append((item, reason))

    return result


def ingest(
    config: Config,
    rclone: Rclone,
    ledger: Ledger,
    spool: Spool,
    *,
    only: str | None = None,
    dry_run: bool = False,
) -> IngestResult:
    sources = [config.source(only)] if only else list(config.sources)
    result = IngestResult(dry_run=dry_run)

    budget = spool.remaining_budget()
    for source in sources:
        source_result = ingest_source(
            source, config, rclone, ledger, spool, budget=budget, dry_run=dry_run
        )
        result.sources.append(source_result)
        # Sources share one cap, so each consumes from the same running budget.
        budget = max(0, budget - source_result.selected_bytes)

    return result


def render(result: IngestResult, spool: Spool) -> str:
    lines: list[str] = []
    verb = "would ingest" if result.dry_run else "ingested"

    for source in result.sources:
        count = len(source.selected) if result.dry_run else len(source.ingested)
        size = source.selected_bytes if result.dry_run else source.ingested_bytes
        lines.append(f"{source.source_id}: {verb} {count} file(s), {human_bytes(size)}")
        detail = [
            f"{source.seen} seen",
            f"{source.already_known} already in ledger",
            f"{source.skipped_cutoff} before cutoff",
            f"{source.skipped_junk} junk",
        ]
        lines.append(f"    ({', '.join(detail)})")
        if source.failed:
            lines.append(f"    {len(source.failed)} failed, will retry next run")
        if source.capped:
            lines.append("    stopped early: spool cap reached")

    total = result.total_selected if result.dry_run else result.total_ingested
    total_bytes = result.selected_bytes if result.dry_run else result.total_bytes
    lines.append("")
    lines.append(f"total: {verb} {total} file(s), {human_bytes(total_bytes)}")

    if not result.dry_run:
        used = spool.usage_bytes()
        lines.append(f"spool: {human_bytes(used)} of {human_bytes(spool.cap_bytes)} used")
    if result.any_capped:
        lines.append(
            "WARNING: spool cap reached. Ingest stopped early; nothing was deleted. "
            "Deliver to the NAS to free space, or raise [spool].cap_gb."
        )
    return "\n".join(lines)

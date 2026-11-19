"""Stage 1 — ingest.

Enumerate each configured source, ask the ledger which items are genuinely new, respect
the cutoff and the spool cap, then copy an explicit file list into the spool.

`--since` / `--until` narrow one run to a date window, for filling a gap by hand. Items
outside the window are skipped without being recorded, so they stay available to any
later run.

Ordering matters: bytes land first, are verified, and only then are recorded. Recording
before copying would mark a file done that was never fetched — silent data loss. The
reverse ordering costs at most a re-download.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config, SourceConfig
from .dates import is_before_cutoff, is_on_or_after
from .errors import RcloneError
from .guards import human_bytes
from .ledger import Ledger
from .progress import QUIET, Progress
from .rclone import Rclone, RemoteItem
from .spool import Spool

# Filesystem detritus that is never worth transferring.
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r", "Icon"}

# rclone writes each file under this suffix and renames it when the transfer completes.
RCLONE_PARTIAL_SUFFIX = ".partial"


@dataclass
class SourceResult:
    source_id: str
    seen: int = 0
    skipped_cutoff: int = 0
    skipped_until: int = 0
    skipped_junk: int = 0
    already_known: int = 0
    selected: list[RemoteItem] = field(default_factory=list)
    ingested: list[RemoteItem] = field(default_factory=list)
    failed: list[tuple[RemoteItem, str]] = field(default_factory=list)
    capped: bool = False
    listing_error: str | None = None
    # Files the remote listed with no hash. Identity is (source, path, hash), so a re-upload
    # of one at the same path would look already known. See D15.
    hashless: int = 0

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
    since: date | None = None
    until: date | None = None

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

    @property
    def any_failed(self) -> bool:
        return any(s.listing_error or s.failed for s in self.sources)


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


def landed_so_far(dest: Path) -> tuple[int, int]:
    """Completed files and bytes so far under `dest`, partial transfers counted in bytes."""
    files = nbytes = 0
    if not dest.exists():
        return 0, 0
    for path in dest.rglob("*"):
        if path.is_file():
            nbytes += path.stat().st_size
            if not path.name.endswith(RCLONE_PARTIAL_SUFFIX):
                files += 1
    return files, nbytes


def ingest_source(
    source: SourceConfig,
    config: Config,
    rclone: Rclone,
    ledger: Ledger,
    spool: Spool,
    *,
    budget: int,
    dry_run: bool = False,
    since: date | None = None,
    until: date | None = None,
    progress: Progress = QUIET,
) -> SourceResult:
    """`since` replaces the source's cutoff for this run; `until` is exclusive."""
    result = SourceResult(source_id=source.id)
    lower = since if since is not None else source.cutoff

    with progress.task(f"listing {source.id}"):
        listing = rclone.lsjson(source.path)
    result.seen = len(listing)

    candidates: list[RemoteItem] = []
    for item in listing:
        if is_junk(item):
            result.skipped_junk += 1
            continue
        if is_before_cutoff(item.path, item.name, lower):
            result.skipped_cutoff += 1
            continue
        if is_on_or_after(item.path, item.name, until):
            result.skipped_until += 1
            continue
        candidates.append(item)
    result.hashless = sum(1 for item in candidates if not item.hash)

    unseen = ledger.filter_new(source.id, candidates)
    result.already_known = len(candidates) - len(unseen)

    unseen.sort(key=lambda item: item.path)
    result.selected, result.capped = select_within_budget(unseen, budget)

    if dry_run or not result.selected:
        return result

    dest = spool.source_incoming(source.id)
    placed: list[tuple[RemoteItem, Path]] = []
    batches: defaultdict[Path, list[str]] = defaultdict(list)
    for item in result.selected:
        root = spool.incoming_root_for(source.id, item.path, item.hash)
        placed.append((item, root))
        batches[root].append(item.path)

    # The source's incoming folder may still hold files from an earlier, unprocessed
    # ingest, so progress counts only what lands on top of them.
    before_files, before_bytes = landed_so_far(dest)

    def poll() -> tuple[int, int]:
        files, nbytes = landed_so_far(dest)
        return files - before_files, nbytes - before_bytes

    copy_errors: dict[Path, str] = {}
    with progress.task(
        f"ingest {source.id}",
        items=len(result.selected),
        total_bytes=result.selected_bytes,
        poll=poll,
    ):
        for root, rel_paths in batches.items():
            try:
                rclone.copy_files(source.path, root, rel_paths)
            except Exception as exc:
                copy_errors[root] = str(exc)

    for item, root in placed:
        landed = root / item.path
        if landed.is_file() and landed.stat().st_size == item.size:
            ledger.record_ingested(source.id, item, landed)
            result.ingested.append(item)
        else:
            reason = copy_errors.get(root, "file missing or truncated after copy")
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
    since: date | None = None,
    until: date | None = None,
    progress: Progress = QUIET,
) -> IngestResult:
    sources = [config.source(only)] if only else list(config.sources)
    result = IngestResult(dry_run=dry_run, since=since, until=until)

    budget = spool.remaining_budget()
    for source in sources:
        try:
            source_result = ingest_source(
                source,
                config,
                rclone,
                ledger,
                spool,
                budget=budget,
                dry_run=dry_run,
                since=since,
                until=until,
                progress=progress,
            )
        except RcloneError as exc:
            source_result = SourceResult(source_id=source.id, listing_error=exc.report())
        result.sources.append(source_result)
        # Sources share one cap, so each consumes from the same running budget.
        budget = max(0, budget - source_result.selected_bytes)

    return result


def render(result: IngestResult, spool: Spool) -> str:
    lines: list[str] = []
    verb = "would ingest" if result.dry_run else "ingested"
    lower = f"before {result.since}" if result.since else "before cutoff"

    if result.since or result.until:
        start = result.since or "cutoff"
        end = result.until or "open"
        lines.append(f"window: {start} to {end} (end exclusive); undated files still come down")
        lines.append("")

    for source in result.sources:
        count = len(source.selected) if result.dry_run else len(source.ingested)
        size = source.selected_bytes if result.dry_run else source.ingested_bytes
        lines.append(f"{source.source_id}: {verb} {count} file(s), {human_bytes(size)}")
        if source.listing_error:
            lines.append("    listing failed, will retry next run")
            continue
        detail = [
            f"{source.seen} seen",
            f"{source.already_known} already in ledger",
            f"{source.skipped_cutoff} {lower}",
            f"{source.skipped_junk} junk",
        ]
        if result.until:
            detail.insert(3, f"{source.skipped_until} on or after {result.until}")
        lines.append(f"    ({', '.join(detail)})")
        if source.failed:
            lines.append(f"    {len(source.failed)} failed, will retry next run")
        if source.capped:
            lines.append("    stopped early: spool cap reached")
        if source.hashless:
            lines.append(
                f"    WARNING: {source.hashless} file(s) listed with no hash, so a re-upload "
                "at the same path would be missed (D15)"
            )

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

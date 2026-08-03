"""Stage 2 — process.

Takes what ingest put in `incoming/` and builds `outbox/`, an exact replica of what will
land on the NAS. Delivery then needs no business logic at all: it moves a tree.

Everything here is a `rename()` within one filesystem, which is atomic and free. That
is why the spool is required to live on a single volume — a cross-device spool would
turn this stage into a full byte copy of every photo.

Ordering matches ingest: the file moves first, then the ledger is updated. A lost
ledger write costs a reprocess, which is idempotent. The reverse would mark a file
filed that never moved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config
from .dates import resolve
from .layout import Destination, destination_for, same_content
from .ledger import Item, Ledger, State
from .spool import Spool
from .triage import DEST_CONFLICT, NO_DATE, UNKNOWN_TYPE, Triage


@dataclass
class ProcessResult:
    filed: list[tuple[Item, Destination]] = field(default_factory=list)
    triaged: list[tuple[Item, str]] = field(default_factory=list)
    duplicates: list[Item] = field(default_factory=list)
    missing: list[Item] = field(default_factory=list)
    date_sources: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def total_seen(self) -> int:
        return (
            len(self.filed) + len(self.triaged) + len(self.duplicates) + len(self.missing)
        )

    @property
    def triage_rate(self) -> float:
        return len(self.triaged) / self.total_seen if self.total_seen else 0.0


def _move(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)


def _prune_empty(root: Path, leaf: Path) -> None:
    """Remove directories left empty by a move, without escaping the spool."""
    current = leaf
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            return  # not empty, or gone
        current = current.parent


def process_item(
    item: Item,
    config: Config,
    spool: Spool,
    triage: Triage,
    ledger: Ledger,
    result: ProcessResult,
    *,
    dry_run: bool,
) -> None:
    source = Path(item.spool_path) if item.spool_path else None
    if source is None or not source.is_file():
        result.missing.append(item)
        return

    filename = source.name
    kind = config.classify.kind(filename)

    if kind == "unknown":
        _triage(item, source, UNKNOWN_TYPE, f"unrecognised extension: {filename}",
                triage, ledger, result, spool, dry_run=dry_run)
        return

    when, date_source = resolve(source, item.src_path, filename, try_exif=kind == "photo")
    result.date_sources[date_source] = result.date_sources.get(date_source, 0) + 1

    if when is None:
        _triage(item, source, NO_DATE, "no date in EXIF, filename, or source path",
                triage, ledger, result, spool, dry_run=dry_run)
        return

    suffix = config.source(item.source_id).suffix
    destination = destination_for(filename, kind, when, suffix)
    target = spool.outbox / destination.relative

    if target.exists():
        # Already staged. Identical content means this is a re-run or a genuine
        # duplicate and can be dropped; different content is a real collision that a
        # human has to look at, never an overwrite. See D5.
        if same_content(source, target):
            result.duplicates.append(item)
            if not dry_run:
                source.unlink()
                ledger.mark_processed(
                    item.id, spool_path=target, dest_path=str(destination.relative),
                    resolved_date=when.isoformat(), date_source=date_source,
                )
                _prune_empty(spool.incoming, source.parent)
            return
        _triage(item, source, DEST_CONFLICT,
                f"different content already staged at {destination.relative}",
                triage, ledger, result, spool, dry_run=dry_run)
        return

    result.filed.append((item, destination))
    if dry_run:
        return

    _move(source, target)
    ledger.mark_processed(
        item.id,
        spool_path=target,
        dest_path=str(destination.relative),
        resolved_date=when.isoformat(),
        date_source=date_source,
    )
    _prune_empty(spool.incoming, source.parent)


def _triage(
    item: Item,
    source: Path,
    reason: str,
    detail: str,
    triage: Triage,
    ledger: Ledger,
    result: ProcessResult,
    spool: Spool,
    *,
    dry_run: bool,
) -> None:
    result.triaged.append((item, reason))
    if dry_run:
        return

    target = triage.path_for(reason, item.source_id, item.src_path)
    if target.exists():
        target = target.with_name(f"{target.stem}_{item.id}{target.suffix}")
    _move(source, target)
    triage.record(
        reason=reason,
        source_id=item.source_id,
        src_path=item.src_path,
        triage_path=target,
        detail=detail,
    )
    ledger.mark_triaged(item.id, spool_path=target, reason=reason)
    _prune_empty(spool.incoming, source.parent)


def process(
    config: Config,
    ledger: Ledger,
    spool: Spool,
    *,
    dry_run: bool = False,
) -> ProcessResult:
    result = ProcessResult(dry_run=dry_run)
    triage = Triage(spool.triage)

    for item in ledger.items_in_state(State.INGESTED):
        process_item(item, config, spool, triage, ledger, result, dry_run=dry_run)

    return result


def render(result: ProcessResult) -> str:
    verb = "would file" if result.dry_run else "filed"
    lines = [
        f"{verb} {len(result.filed)} file(s)",
        f"triaged      {len(result.triaged)}",
    ]
    if result.duplicates:
        lines.append(f"duplicates   {len(result.duplicates)} (already staged, dropped)")
    if result.missing:
        lines.append(f"missing      {len(result.missing)} (spool file gone; re-ingest)")

    if result.date_sources:
        mix = ", ".join(f"{k}={v}" for k, v in sorted(result.date_sources.items()))
        lines.append(f"date source  {mix}")

    if result.triaged:
        reasons: dict[str, int] = {}
        for _item, reason in result.triaged:
            reasons[reason] = reasons.get(reason, 0) + 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        lines.append(f"\n{len(result.triaged)} file(s) need review ({detail})")
        lines.append("inspect them under the spool's triage/ directory")

    if result.filed and result.dry_run:
        lines.append("\nsample of what would be staged:")
        for _item, destination in result.filed[:5]:
            lines.append(f"  {destination.relative}")

    return "\n".join(lines)


def sample_destinations(result: ProcessResult, limit: int = 5) -> list[str]:
    return [str(d.relative) for _i, d in result.filed[:limit]]


def month_of(when: date) -> str:
    return f"{when:%Y-%m}"

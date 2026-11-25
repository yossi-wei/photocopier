"""Stage 2 — process.

Takes what ingest put in `incoming/` and builds `outbox/`, an exact replica of what will
land on the NAS. Delivery then needs no business logic at all: it moves a tree.

Everything here is a `rename()` within one filesystem, which is atomic and free. That
is why the spool is required to live on a single volume — a cross-device spool would
turn this stage into a full byte copy of every photo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from .config import Config
from .dates import coarse_date, resolve
from .flush import destination_root
from .ingest import RCLONE_PARTIAL_SUFFIX
from .layout import Destination, destination_for, file_digest, same_content
from .ledger import Item, Ledger, Moves, State
from .progress import QUIET, Progress
from .rclone import RemoteItem
from .spool import Spool
from .triage import (
    BEFORE_CUTOFF,
    DEST_CONFLICT,
    NO_DATE,
    UNKNOWN_TYPE,
    UNTRACKED,
    Triage,
    conflict_detail,
)

# The folder ingest puts a later version under, see Spool.incoming_root_for.
VERSION_DIR = re.compile(r"^[0-9a-f]{16}$")
# Untracked files are keyed by their own bytes, which never collides with a remote hash.
LOCAL_HASH_PREFIX = "local-sha256:"


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
    moves: Moves,
    result: ProcessResult,
    *,
    dry_run: bool,
) -> None:
    source = Path(item.spool_path) if item.spool_path else None
    if source is None or not source.is_file():
        result.missing.append(item)
        return

    filename = Path(item.src_path).name
    kind = config.classify.kind(filename)

    if kind == "unknown":
        _triage(item, source, UNKNOWN_TYPE, f"unrecognised extension: {filename}",
                triage, moves, result, spool, dry_run=dry_run)
        return

    when, date_source = resolve(source, item.src_path, filename, try_exif=kind == "photo")
    result.date_sources[date_source] = result.date_sources.get(date_source, 0) + 1

    if when is None:
        _triage(item, source, NO_DATE, "no date in EXIF, filename, or source path",
                triage, moves, result, spool, dry_run=dry_run)
        return

    source_config = config.source(item.source_id)
    cutoff = source_config.cutoff
    if cutoff and when < cutoff and coarse_date(item.src_path, filename)[0] is None:
        # Ingest skips what its name or folder dates before the cutoff. This file carried no
        # such date, so only EXIF shows it is older. A file whose name dates it before the
        # cutoff came down on purpose, through an `ingest --since` window, and is filed.
        _triage(item, source, BEFORE_CUTOFF,
                f"{date_source} dates it {when}, before the {cutoff} cutoff",
                triage, moves, result, spool, dry_run=dry_run)
        return

    suffix = source_config.suffix
    destination = destination_for(filename, kind, when, suffix)
    target = spool.outbox / destination.relative

    another_file_staged = target.exists() and not target.samefile(source)
    if another_file_staged:
        # Already staged. Identical content means this is a re-run or a genuine
        # duplicate and can be dropped; different content is a real collision that a
        # human has to look at, never an overwrite. See D5.
        if same_content(source, target):
            result.duplicates.append(item)
            if not dry_run:
                ledger.mark_processed(
                    item.id, spool_path=target, dest_path=str(destination.relative),
                    resolved_date=when.isoformat(), date_source=date_source,
                )
                source.unlink()
                _prune_empty(spool.incoming, source.parent)
            return
        _triage(item, source, DEST_CONFLICT,
                conflict_detail(str(destination.relative), staged=True),
                triage, moves, result, spool, dry_run=dry_run)
        return

    result.filed.append((item, destination))
    if dry_run:
        return

    moves.to_outbox(
        item.id,
        source,
        target,
        dest_path=str(destination.relative),
        resolved_date=when.isoformat(),
        date_source=date_source,
    )
    _prune_empty(spool.incoming, source.parent)


def _settle_strays(
    config: Config,
    ledger: Ledger,
    spool: Spool,
    pending: list[Item],
    result: ProcessResult,
    *,
    dry_run: bool,
) -> list[Path]:
    """Deal with files in incoming/ that no pending item points at.

    A copy already held downstream is a leftover and is dropped. A copy of a version still
    waiting is dropped too. A file the ledger never recorded is returned, to be triaged as
    untracked. A file a failed copy left behind, and an rclone partial, stay where they are:
    the retry fetches a fresh copy.
    """
    waiting = {item.spool_path for item in pending}
    strays = [
        path
        for path in spool.incoming.rglob("*")
        if path.is_file()
        and str(path) not in waiting
        and not path.name.endswith(RCLONE_PARTIAL_SUFFIX)
    ]
    if not strays:
        return []
    moved_on: dict[str, list[Item]] = {}
    for state in (State.PROCESSED, State.DELIVERED):
        for item in ledger.items_in_state(state):
            if item.dest_path:
                landed = spool.source_incoming(item.source_id) / item.src_path
                moved_on.setdefault(str(landed), []).append(item)
    versions: dict[tuple[str, str], list[Item]] = {}
    for item in pending:
        versions.setdefault((item.source_id, item.src_path), []).append(item)
    failed = {(item.source_id, item.src_path) for item in ledger.items_in_state(State.FAILED)}

    untracked: list[Path] = []
    for path in strays:
        held = _held_downstream(path, moved_on.get(str(path), []), config, spool)
        if held is not None:
            result.duplicates.append(held)
            if not dry_run:
                path.unlink()
                _prune_empty(spool.incoming, path.parent)
            continue
        key = _stray_key(spool, path)
        if key in failed:
            continue
        if any(
            item.spool_path and same_content(path, Path(item.spool_path))
            for item in versions.get(key, [])
            if Path(item.spool_path or "").is_file()
        ):
            if not dry_run:
                path.unlink()
                _prune_empty(spool.incoming, path.parent)
            continue
        untracked.append(path)
    return untracked


def _stray_key(spool: Spool, path: Path) -> tuple[str, str]:
    """(source id, source-relative path) of a file under incoming/, version folder dropped."""
    source_id, *rest = path.relative_to(spool.incoming).parts
    if len(rest) > 1 and VERSION_DIR.match(rest[0]):
        rest = rest[1:]
    return source_id, "/".join(rest)


def _admit_untracked(
    path: Path,
    spool: Spool,
    triage: Triage,
    ledger: Ledger,
    moves: Moves,
    result: ProcessResult,
    *,
    dry_run: bool,
) -> None:
    """Record an untracked file in the ledger, then triage it, so its bytes are never lost.

    A kill between the two leaves an ingested row pointing at the file, which the next pass
    processes like any other: the file is tracked either way.
    """
    source_id, src_path = _stray_key(spool, path)
    stat = path.stat()
    remote = RemoteItem(
        path=src_path,
        name=path.name,
        size=stat.st_size,
        mod_time=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat().replace("+00:00", "Z"),
        hash=LOCAL_HASH_PREFIX + file_digest(path),
    )
    if dry_run:
        stand_in = Item(
            id=0, source_id=source_id, src_path=src_path, src_size=remote.size,
            src_hash=remote.hash, state=State.INGESTED, spool_path=str(path),
        )
        result.triaged.append((stand_in, UNTRACKED))
        return
    ledger.record_ingested(source_id, remote, path)
    item = next(
        row for row in ledger.items_in_state(State.INGESTED, source_id)
        if row.spool_path == str(path)
    )
    _triage(item, path, UNTRACKED, "in incoming/ with no ledger record (killed ingest)",
            triage, moves, result, spool, dry_run=False)


def _held_downstream(path: Path, items: list[Item], config: Config, spool: Spool) -> Item | None:
    for item in items:
        for copy in (spool.outbox / item.dest_path, destination_root(config, item.dest_path)):
            if copy.is_file() and same_content(path, copy):
                return item
    return None


def _triage(
    item: Item,
    source: Path,
    reason: str,
    detail: str,
    triage: Triage,
    moves: Moves,
    result: ProcessResult,
    spool: Spool,
    *,
    dry_run: bool,
) -> None:
    result.triaged.append((item, reason))
    if dry_run:
        return

    triage.admit(moves, item, source, reason=reason, detail=detail)
    _prune_empty(spool.incoming, source.parent)


def process(
    config: Config,
    ledger: Ledger,
    spool: Spool,
    *,
    dry_run: bool = False,
    progress: Progress = QUIET,
) -> ProcessResult:
    result = ProcessResult(dry_run=dry_run)
    triage = Triage(spool.triage)
    triage.settle(ledger)

    pending = ledger.items_in_state(State.INGESTED)
    untracked = _settle_strays(config, ledger, spool, pending, result, dry_run=dry_run)
    with Moves(ledger) as moves, progress.task("process", items=len(pending)) as task:
        for path in untracked:
            _admit_untracked(path, spool, triage, ledger, moves, result, dry_run=dry_run)
        for item in pending:
            task.current(Path(item.src_path).name)
            process_item(item, config, spool, triage, ledger, moves, result, dry_run=dry_run)
            task.advance()

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

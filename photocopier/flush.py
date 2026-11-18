"""Stage 3 — flush.

Delivers the outbox to the NAS. Process already decided every destination path, so this
stage carries no filing logic: it moves a tree, verifies what it moved, and reclaims the
spool.

Two properties matter more than speed here.

**Nothing is delivered unverified.** Every file is copied to a temporary name, read back
from the destination and hashed, and only then moved into place. That read-back costs
roughly double the wire time and is the point: a truncated file over SMB is
indistinguishable from a good one by size alone often enough to matter.

**Nothing is ever overwritten.** A destination path that already holds different content
is a genuine collision and goes to triage. See D5.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .guards import check_free_space, check_mount_live, human_bytes
from .layout import PHOTOS, VIDEO, file_digest, same_content
from .ledger import Item, Ledger, State, first_file
from .progress import QUIET, Progress
from .spool import Spool
from .triage import DEST_CONFLICT, Triage

TEMP_SUFFIX = ".photocopier-partial"


@dataclass
class FlushResult:
    delivered: list[tuple[Item, Path]] = field(default_factory=list)
    already_there: list[Item] = field(default_factory=list)
    conflicts: list[Item] = field(default_factory=list)
    missing: list[Item] = field(default_factory=list)
    failed: list[tuple[Item, str]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def delivered_bytes(self) -> int:
        return sum(p.stat().st_size for _i, p in self.delivered if p.exists())

    @property
    def total_seen(self) -> int:
        return (
            len(self.delivered)
            + len(self.already_there)
            + len(self.conflicts)
            + len(self.missing)
            + len(self.failed)
        )


def destination_root(config: Config, dest_path: str) -> Path:
    """Map an outbox-relative path to its configured root.

    The outbox has two top-level buckets rather than mirroring the NAS mount directly,
    so photos_root and video_root can live anywhere — they are not required to share a
    parent.
    """
    bucket, _, remainder = dest_path.partition("/")
    if bucket == VIDEO:
        return config.destination.video_root / remainder
    if bucket == PHOTOS:
        return config.destination.photos_root / remainder
    raise ValueError(f"unrecognised destination bucket in {dest_path!r}")


def deliver_file(source: Path, target: Path) -> None:
    """Copy, verify against a read-back hash, then move into place.

    The temporary name means an interrupted transfer never leaves a plausible-looking
    file at the real path. Verification reads the destination copy rather than trusting
    the write.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + TEMP_SUFFIX)

    try:
        shutil.copy2(source, partial)
        expected = file_digest(source)
        actual = file_digest(partial)
        if expected != actual:
            raise OSError(
                f"verification failed: {target.name} hashed {actual[:12]} at the "
                f"destination, expected {expected[:12]}"
            )
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _prune_empty(root: Path, leaf: Path) -> None:
    current = leaf
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def flush_staged(
    rows: list[Item],
    config: Config,
    spool: Spool,
    triage: Triage,
    ledger: Ledger,
    result: FlushResult,
    *,
    dry_run: bool,
) -> None:
    item = rows[0]
    if not item.dest_path:
        result.missing.extend(rows)
        return

    staged = spool.outbox / item.dest_path
    source = first_file(staged, *(row.target_path for row in rows))
    if source is None:
        result.missing.extend(rows)
        return

    target = destination_root(config, item.dest_path)

    if target.exists():
        if same_content(target, source):
            # Already delivered — a re-run, or something filed by hand earlier. Routine.
            result.already_there.extend(rows)
            if not dry_run:
                _mark_delivered_then_reclaim(rows, source, spool, ledger)
            return

        result.conflicts.extend(rows)
        if not dry_run:
            detail = f"different content already at {target}"
            for row in rows[:-1]:
                copy = triage.vacant_path(DEST_CONFLICT, row)
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, copy)
                triage.record(
                    reason=DEST_CONFLICT,
                    source_id=row.source_id,
                    src_path=row.src_path,
                    triage_path=copy,
                    detail=detail,
                )
                ledger.mark_triaged(row.id, spool_path=copy, reason=DEST_CONFLICT)
            triage.admit(ledger, rows[-1], source, reason=DEST_CONFLICT, detail=detail)
            _prune_empty(spool.outbox, staged.parent)
        return

    if dry_run:
        result.delivered.append((item, source))
        result.already_there.extend(rows[1:])
        return

    try:
        deliver_file(source, target)
    except OSError as exc:
        for row in rows:
            result.failed.append((row, str(exc)))
            ledger.record_error(row.id, str(exc))
        return

    _mark_delivered_then_reclaim(rows, source, spool, ledger)
    result.delivered.append((item, target))
    result.already_there.extend(rows[1:])


def _mark_delivered_then_reclaim(
    rows: list[Item], source: Path, spool: Spool, ledger: Ledger
) -> None:
    for row in rows:
        ledger.mark_delivered(row.id, dest_path=row.dest_path)
    source.unlink()
    _prune_empty(spool.outbox, source.parent)


def _drop_delivered_leftovers(
    config: Config,
    ledger: Ledger,
    spool: Spool,
    staged: dict[str | None, list[Item]],
    result: FlushResult,
    *,
    dry_run: bool,
) -> None:
    in_outbox = {
        path.relative_to(spool.outbox).as_posix(): path
        for path in spool.outbox.rglob("*")
        if path.is_file()
    }
    moving_here = {item.target_path for item in ledger.items_in_state(State.INGESTED)}
    strays = {
        rel: path
        for rel, path in in_outbox.items()
        if rel not in staged and str(path) not in moving_here
    }
    if not strays:
        return
    delivered: dict[str | None, list[Item]] = {}
    for item in ledger.items_in_state(State.DELIVERED):
        delivered.setdefault(item.dest_path, []).append(item)
    for dest_path, path in strays.items():
        rows = delivered.get(dest_path)
        if not rows:
            continue
        target = destination_root(config, dest_path)
        if not (target.is_file() and same_content(target, path)):
            continue
        result.already_there.extend(rows)
        if not dry_run:
            path.unlink()
            _prune_empty(spool.outbox, path.parent)


def _staged_size(spool: Spool, item: Item) -> int:
    if not item.dest_path:
        return 0
    source = spool.outbox / item.dest_path
    return source.stat().st_size if source.is_file() else 0


def flush(
    config: Config,
    ledger: Ledger,
    spool: Spool,
    *,
    dry_run: bool = False,
    progress: Progress = QUIET,
) -> FlushResult:
    """Deliver everything staged. Guards run first and refuse rather than proceed."""
    check_mount_live(config.destination.mount_point, config.destination.mount_marker)

    staged: dict[str | None, list[Item]] = {}
    for item in ledger.items_in_state(State.PROCESSED):
        staged.setdefault(item.dest_path, []).append(item)
    sizes = [_staged_size(spool, rows[0]) for rows in staged.values()]
    staged_bytes = sum(sizes)
    if staged_bytes:
        check_free_space(config.destination.mount_point, staged_bytes)

    result = FlushResult(dry_run=dry_run)
    _drop_delivered_leftovers(config, ledger, spool, staged, result, dry_run=dry_run)
    triage = Triage(spool.triage)
    with progress.task("flush", items=len(staged), total_bytes=staged_bytes) as task:
        for rows, size in zip(staged.values(), sizes, strict=True):
            task.current(Path(rows[0].dest_path or rows[0].src_path).name)
            flush_staged(rows, config, spool, triage, ledger, result, dry_run=dry_run)
            task.advance(nbytes=size)
    return result


def render(result: FlushResult) -> str:
    verb = "would deliver" if result.dry_run else "delivered"
    lines = [f"{verb} {len(result.delivered)} file(s), {human_bytes(result.delivered_bytes)}"]

    if result.already_there:
        lines.append(f"already present  {len(result.already_there)} (identical, spool reclaimed)")
    if result.conflicts:
        lines.append(f"conflicts        {len(result.conflicts)} -> triage, nothing overwritten")
    if result.missing:
        lines.append(f"missing          {len(result.missing)} (outbox file gone)")
    if result.failed:
        lines.append(f"failed           {len(result.failed)}, will retry next run")
        for _item, reason in result.failed[:3]:
            lines.append(f"    {reason}")

    if not result.total_seen:
        return "nothing staged for delivery"
    return "\n".join(lines)

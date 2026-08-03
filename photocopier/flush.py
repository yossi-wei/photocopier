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
from .layout import PHOTOS, VIDEO, file_digest
from .ledger import Item, Ledger, State
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


def flush_item(
    item: Item,
    config: Config,
    spool: Spool,
    triage: Triage,
    ledger: Ledger,
    result: FlushResult,
    *,
    dry_run: bool,
) -> None:
    if not item.dest_path:
        result.missing.append(item)
        return

    source = spool.outbox / item.dest_path
    if not source.is_file():
        result.missing.append(item)
        return

    target = destination_root(config, item.dest_path)

    if target.exists():
        if target.stat().st_size == source.stat().st_size and file_digest(
            target
        ) == file_digest(source):
            # Already delivered — a re-run, or something filed by hand earlier. Routine.
            result.already_there.append(item)
            if not dry_run:
                source.unlink()
                ledger.mark_delivered(item.id, dest_path=item.dest_path)
                _prune_empty(spool.outbox, source.parent)
            return

        result.conflicts.append(item)
        if not dry_run:
            triage_target = triage.path_for(DEST_CONFLICT, item.source_id, item.src_path)
            triage_target.parent.mkdir(parents=True, exist_ok=True)
            if triage_target.exists():
                triage_target = triage_target.with_name(
                    f"{triage_target.stem}_{item.id}{triage_target.suffix}"
                )
            source.rename(triage_target)
            triage.record(
                reason=DEST_CONFLICT,
                source_id=item.source_id,
                src_path=item.src_path,
                triage_path=triage_target,
                detail=f"different content already at {target}",
            )
            ledger.mark_triaged(item.id, spool_path=triage_target, reason=DEST_CONFLICT)
            _prune_empty(spool.outbox, source.parent)
        return

    if dry_run:
        result.delivered.append((item, source))
        return

    try:
        deliver_file(source, target)
    except OSError as exc:
        result.failed.append((item, str(exc)))
        ledger.record_error(item.id, str(exc))
        return

    result.delivered.append((item, target))
    source.unlink()
    ledger.mark_delivered(item.id, dest_path=item.dest_path)
    _prune_empty(spool.outbox, source.parent)


def flush(
    config: Config,
    ledger: Ledger,
    spool: Spool,
    *,
    dry_run: bool = False,
) -> FlushResult:
    """Deliver everything staged. Guards run first and refuse rather than proceed."""
    check_mount_live(config.destination.mount_point, config.destination.mount_marker)

    pending = ledger.items_in_state(State.PROCESSED)
    staged_bytes = sum(
        (spool.outbox / i.dest_path).stat().st_size
        for i in pending
        if i.dest_path and (spool.outbox / i.dest_path).is_file()
    )
    if staged_bytes:
        check_free_space(config.destination.mount_point, staged_bytes)

    result = FlushResult(dry_run=dry_run)
    triage = Triage(spool.triage)
    for item in pending:
        flush_item(item, config, spool, triage, ledger, result, dry_run=dry_run)
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

"""Closing out triage — the human's half of the workflow.

process and flush park what they cannot place. This is how a person tells the ledger
what they decided, so nobody has to edit ledger.db by hand. Two outcomes:

- **filed** — the person put the file in the library themselves. The copy there is
  checked, the row becomes delivered, and the triaged copy is removed.
- **discarded** — the file does not belong in the library. The row is kept, so ingest
  never fetches it again, and the triaged copy is removed.

Ordering matches the rest of the pipeline: the ledger is written first and the spool
copy deleted after. A lost delete leaves a stray file in triage, which is wasteful but
safe; the reverse would leave a triaged row pointing at nothing. See D11.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config, expand
from .errors import GuardError, LedgerError
from .flush import destination_root
from .guards import check_mount_live
from .layout import PHOTOS, VIDEO, same_content
from .ledger import Item, Ledger, State
from .spool import Spool
from .triage import Triage, TriageEntry

FILED = "filed"
DISCARDED = "discarded"


@dataclass(frozen=True)
class Pending:
    """A triaged row, joined to its file and the manifest entry that explains it."""

    item: Item
    path: Path | None
    entry: TriageEntry | None

    @property
    def present(self) -> bool:
        return self.path is not None and self.path.is_file()


@dataclass(frozen=True)
class Resolution:
    item: Item
    outcome: str
    removed: Path | None
    dest_path: str | None = None
    dry_run: bool = False


def pending(ledger: Ledger, triage: Triage) -> list[Pending]:
    entries = triage.lookup()
    out: list[Pending] = []
    for item in ledger.items_in_state(State.TRIAGED):
        path = Path(item.spool_path) if item.spool_path else None
        entry = None
        if path is not None and triage.root in path.parents:
            entry = entries.get(str(path.relative_to(triage.root)))
        out.append(Pending(item=item, path=path, entry=entry))
    return out


def filed_location(config: Config, raw: str) -> tuple[Path, str]:
    """Turn what the person typed into an absolute path and the ledger's dest_path.

    Accepts an absolute path under photos_root or video_root, or one in the outbox form
    the ledger already uses everywhere else (`photos/2025/03/x.jpg`, `video/2025/x.mp4`).
    """
    path = expand(raw)
    if not path.is_absolute():
        if not path.parts or path.parts[0] not in (PHOTOS, VIDEO):
            raise GuardError(
                f"{raw!r} is neither an absolute path nor one starting with "
                f"{PHOTOS}/ or {VIDEO}/"
            )
        path = destination_root(config, path.as_posix())

    roots = [
        (PHOTOS, config.destination.photos_root.resolve()),
        (VIDEO, config.destination.video_root.resolve()),
    ]
    # The deeper root wins, in case one library lives inside the other.
    roots.sort(key=lambda pair: len(pair[1].parts), reverse=True)
    resolved = path.resolve()
    for bucket, root in roots:
        if root in resolved.parents:
            return path, f"{bucket}/{resolved.relative_to(root).as_posix()}"

    raise GuardError(
        f"{path} is not under photos_root ({config.destination.photos_root}) or "
        f"video_root ({config.destination.video_root})"
    )


def resolve_filed(
    config: Config,
    ledger: Ledger,
    spool: Spool,
    item_id: int,
    filed_at: str,
    *,
    dry_run: bool = False,
) -> Resolution:
    """Record that a person filed a triaged item in the library by hand.

    The mount guard runs first for the same reason it does in flush: a copy made into
    an unmounted share's empty mount point is on the local disk, and would otherwise
    pass the existence check and be recorded as delivered.
    """
    item = _triaged(ledger, item_id)
    copy = _triaged_copy(item, spool)
    check_mount_live(config.destination.mount_point, config.destination.mount_marker)

    target, dest_path = filed_location(config, filed_at)
    if not target.is_file():
        raise GuardError(f"nothing at {target}. File it there first, then resolve.")
    if copy is not None and not same_content(copy, target):
        raise GuardError(
            f"{target} is not the same file as the triaged copy at {copy}; nothing "
            "changed. If the file already there is the one to keep, use --discard."
        )

    if not dry_run:
        ledger.mark_delivered(item.id, dest_path=dest_path)
        _remove(copy, spool.triage)
    return Resolution(item, FILED, copy, dest_path=dest_path, dry_run=dry_run)


def resolve_discard(
    ledger: Ledger,
    spool: Spool,
    item_id: int,
    *,
    dry_run: bool = False,
) -> Resolution:
    """Drop a triaged item for good, keeping the row so it is never fetched again."""
    item = _triaged(ledger, item_id)
    copy = _triaged_copy(item, spool)
    if not dry_run:
        ledger.mark_discarded(item.id)
        _remove(copy, spool.triage)
    return Resolution(item, DISCARDED, copy, dry_run=dry_run)


def _triaged(ledger: Ledger, item_id: int) -> Item:
    item = ledger.get(item_id)
    if item is None:
        raise LedgerError(f"no ledger item #{item_id}")
    if item.state != State.TRIAGED:
        raise LedgerError(f"item #{item_id} is {item.state.value}, not triaged")
    return item


def _triaged_copy(item: Item, spool: Spool) -> Path | None:
    """The file in triage, or None if it is already gone.

    Anything the row points at outside the triage area is refused rather than deleted:
    a hand-edited ledger is exactly where such a path would come from.
    """
    if not item.spool_path:
        return None
    path = Path(item.spool_path)
    if spool.triage.resolve() not in path.resolve().parents:
        raise GuardError(
            f"item #{item.id} points at {path}, which is outside {spool.triage}; "
            "refusing to touch it"
        )
    return path if path.is_file() else None


def _remove(path: Path | None, root: Path) -> None:
    if path is None:
        return
    path.unlink(missing_ok=True)
    _prune_empty(root, path.parent)


def _prune_empty(root: Path, leaf: Path) -> None:
    current = leaf
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def render_pending(items: list[Pending], triage_root: Path) -> str:
    if not items:
        return "nothing in triage"

    lines = [f"{len(items)} item(s) awaiting review in {triage_root}", ""]
    for p in items:
        reason = p.item.triage_reason or "?"
        lines.append(f"#{p.item.id}  {reason}  {p.item.source_id}  {p.item.src_path}")
        where = str(p.path) if p.path else "(no path recorded)"
        lines.append(f"    file    {where}{'' if p.present else '  (missing)'}")
        lines.append(f"    why     {p.entry.detail if p.entry else '(no manifest entry)'}")

    lines += [
        "",
        "close one out with:",
        "  photocopier resolve <id> --filed-at <path>   after filing it in the library",
        "  photocopier resolve <id> --discard           to drop it; it is not fetched again",
    ]
    return "\n".join(lines)


def render(result: Resolution) -> str:
    mark, remove = ("would mark", "would remove") if result.dry_run else ("marked", "removed")
    item = result.item
    if result.outcome == FILED:
        lines = [f"#{item.id} {mark} delivered at {result.dest_path}"]
    else:
        lines = [f"#{item.id} {mark} discarded; it will not be fetched again"]
    if result.removed:
        lines.append(f"{remove} triaged copy {result.removed}")
    else:
        lines.append("triaged copy was already gone")
    return "\n".join(lines)

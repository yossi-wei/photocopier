"""Triage — the pile of things a human has to decide about.

Triage is a workflow, not a dead-letter folder. Every file that lands here carries a
recorded reason, so the question "why is this here?" is answered by the manifest rather
than by guessing.

Two ways out, both in the caller's hands: fix the individual file, or add a rule and
reprocess the whole area. Nothing in here is ever auto-filed, and nothing in here is
ever delivered. Closing out a single item once a human has decided is resolve.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .ledger import Item, Ledger, Moves, file_identity

# Reasons are a closed set. A new reason is a deliberate change, not an ad-hoc string.
NO_DATE = "no_date"
UNKNOWN_TYPE = "unknown_type"
DEST_CONFLICT = "dest_conflict"
# Dated by EXIF to before its source's cutoff, with no date in its name or folder, so ingest
# could not skip it. A human decides; see D14.
BEFORE_CUTOFF = "before_cutoff"
# A file in incoming/ the ledger never recorded, left by an ingest killed between the
# copy and the ledger write. Its bytes are kept and shown to a human rather than guessed at.
UNTRACKED = "untracked"

REASONS = (NO_DATE, UNKNOWN_TYPE, DEST_CONFLICT, UNTRACKED, BEFORE_CUTOFF)

MANIFEST_NAME = "manifest.jsonl"


@dataclass(frozen=True)
class TriageEntry:
    reason: str
    source_id: str
    src_path: str
    triage_path: str
    detail: str
    at: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "reason": self.reason,
                "source_id": self.source_id,
                "src_path": self.src_path,
                "triage_path": self.triage_path,
                "detail": self.detail,
                "at": self.at,
            },
            ensure_ascii=False,
        )


def conflict_detail(target: str, *, staged: bool) -> str:
    """The manifest detail for a dest_conflict. `staged` means the target is in the outbox."""
    where = "already staged at" if staged else "already at"
    return f"different content {where} {target}"


_CONFLICT_TARGET = re.compile(r"different content already (?:staged )?at (.+)")


def conflict_target(detail: str) -> str | None:
    """The path a dest_conflict detail names, or None for any other detail."""
    match = _CONFLICT_TARGET.fullmatch(detail)
    return match.group(1) if match else None


class Triage:
    def __init__(self, root: Path):
        self.root = root

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    def path_for(self, reason: str, source_id: str, src_path: str) -> Path:
        """Where a triaged file lives.

        The source's own directory structure is preserved so the surrounding context —
        which month OneDrive thought it belonged to — survives for review.
        """
        if reason not in REASONS:
            raise ValueError(f"unknown triage reason {reason!r}")
        return self.root / reason / source_id / src_path

    def vacant_path(self, reason: str, item: Item) -> Path:
        target = self.path_for(reason, item.source_id, item.src_path)
        if target.exists():
            target = target.with_name(f"{target.stem}_{item.id}{target.suffix}")
        return target

    def admit(self, moves: Moves, item: Item, file: Path, *, reason: str, detail: str) -> None:
        target = self.vacant_path(reason, item)
        self.record(
            reason=reason,
            source_id=item.source_id,
            src_path=item.src_path,
            triage_path=target,
            detail=detail,
        )
        moves.to_triage(item.id, file, target, reason=reason)

    def settle(self, ledger: Ledger) -> None:
        with ledger.transaction():
            for item in ledger.items_moving():
                target = Path(str(item.target_path))
                if file_identity(target) != item.target_identity:
                    continue
                if self.root in target.parents:
                    reason = target.relative_to(self.root).parts[0]
                    ledger.mark_triaged(item.id, spool_path=target, reason=reason)
                else:
                    ledger.moved(item.id, target)

    def record(
        self,
        *,
        reason: str,
        source_id: str,
        src_path: str,
        triage_path: Path,
        detail: str = "",
    ) -> TriageEntry:
        """Append to the manifest.

        JSON Lines rather than a single JSON document: appending cannot corrupt earlier
        entries, so an interrupted run leaves a readable manifest rather than a
        truncated one.
        """
        entry = TriageEntry(
            reason=reason,
            source_id=source_id,
            src_path=src_path,
            triage_path=str(triage_path.relative_to(self.root)),
            detail=detail,
            at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(entry.to_json() + "\n")
        return entry

    def entries(self) -> list[TriageEntry]:
        if not self.manifest_path.exists():
            return []
        out: list[TriageEntry] = []
        for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue  # a partial final line from an interrupted write
            out.append(TriageEntry(**data))
        return out

    def lookup(self) -> dict[str, TriageEntry]:
        """The newest manifest entry for each triage path, relative to the triage root.

        A path can appear more than once if an item was triaged, cleared by hand, and
        triaged again; the latest entry is the one that describes the file there now.
        """
        return {entry.triage_path: entry for entry in self.entries()}

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry.reason] = counts.get(entry.reason, 0) + 1
        return counts

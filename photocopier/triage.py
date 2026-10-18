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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Reasons are a closed set. A new reason is a deliberate change, not an ad-hoc string.
NO_DATE = "no_date"
UNKNOWN_TYPE = "unknown_type"
DEST_CONFLICT = "dest_conflict"

REASONS = (NO_DATE, UNKNOWN_TYPE, DEST_CONFLICT)

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

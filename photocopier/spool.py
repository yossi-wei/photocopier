"""Spool layout and capacity.

The spool is a durable staging area, not a cache: it has to survive weeks of travel, so
it never lives anywhere the OS considers disposable. See D7.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import SpoolConfig


@dataclass(frozen=True)
class Spool:
    root: Path
    cap_bytes: int

    @classmethod
    def from_config(cls, config: SpoolConfig) -> Spool:
        return cls(root=config.path, cap_bytes=config.cap_bytes)

    @property
    def incoming(self) -> Path:
        return self.root / "incoming"

    @property
    def outbox(self) -> Path:
        return self.root / "outbox"

    @property
    def triage(self) -> Path:
        return self.root / "triage"

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.db"

    def source_incoming(self, source_id: str) -> Path:
        return self.incoming / source_id

    def ensure(self) -> None:
        for path in (self.root, self.incoming, self.outbox, self.triage):
            path.mkdir(parents=True, exist_ok=True)

    def usage_bytes(self) -> int:
        """Bytes currently held in the spool's file areas (the ledger is excluded)."""
        total = 0
        for area in (self.incoming, self.outbox, self.triage):
            if not area.exists():
                continue
            for path in area.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
        return total

    def free_bytes(self) -> int:
        probe = self.root if self.root.exists() else self.root.parent
        return shutil.disk_usage(probe).free

    def remaining_budget(self) -> int:
        """How many more bytes may be ingested before the cap is reached."""
        return max(0, self.cap_bytes - self.usage_bytes())

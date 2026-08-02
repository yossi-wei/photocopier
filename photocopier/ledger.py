"""The ledger: the system of record for what has been seen, moved, and delivered.

This exists because the spool is emptied after delivery. If "what is new" were decided
by comparing source against destination, an emptied spool would look like a fresh start
and the entire library would download again, daily. The ledger survives the flush, and
that is its whole reason for existing. See D1.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .errors import LedgerError
from .rclone import RemoteItem

SCHEMA_VERSION = 1


class State(StrEnum):
    DISCOVERED = "discovered"
    INGESTED = "ingested"
    PROCESSED = "processed"
    DELIVERED = "delivered"
    TRIAGED = "triaged"
    FAILED = "failed"


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY,
  source_id     TEXT NOT NULL,
  src_path      TEXT NOT NULL,
  src_size      INTEGER NOT NULL,
  src_modtime   TEXT NOT NULL DEFAULT '',
  src_hash      TEXT NOT NULL DEFAULT '',
  state         TEXT NOT NULL,
  spool_path    TEXT,
  dest_path     TEXT,
  resolved_date TEXT,
  date_source   TEXT,
  triage_reason TEXT,
  discovered_at TEXT NOT NULL,
  ingested_at   TEXT,
  processed_at  TEXT,
  delivered_at  TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  UNIQUE (source_id, src_path, src_hash)
);

CREATE INDEX IF NOT EXISTS items_state_idx ON items (state);
CREATE INDEX IF NOT EXISTS items_source_idx ON items (source_id);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Item:
    id: int
    source_id: str
    src_path: str
    src_size: int
    src_hash: str
    state: State
    spool_path: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _key(item: RemoteItem) -> tuple[str, str]:
    """Identity is (source, path, hash). The hash is normalised to '' rather than NULL.

    SQLite treats NULLs as distinct in UNIQUE constraints, so a NULL hash would let the
    same file be inserted repeatedly and re-downloaded forever — the exact failure the
    ledger exists to prevent.
    """
    return item.path, item.hash or ""


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle -------------------------------------------------------

    def open(self) -> Ledger:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self.path)
        except sqlite3.Error as exc:
            raise LedgerError(f"cannot open ledger at {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Ledger:
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise LedgerError("ledger is not open")
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    # -- queries ---------------------------------------------------------

    def known_keys(self, source_id: str) -> set[tuple[str, str]]:
        """Keys that should not be fetched again.

        FAILED is deliberately excluded. A failed item has not been obtained, so
        treating it as known would skip it permanently on every subsequent run — the
        same silent, invisible data loss that D1 rejected --max-age for.
        """
        rows = self.conn.execute(
            "SELECT src_path, src_hash FROM items WHERE source_id = ? AND state != ?",
            (source_id, State.FAILED.value),
        )
        return {(row["src_path"], row["src_hash"]) for row in rows}

    def filter_new(self, source_id: str, items: Iterable[RemoteItem]) -> list[RemoteItem]:
        """Return the items that still need fetching.

        This is the decision rclone must never make. It is answered from the ledger,
        which outlives the spool.
        """
        known = self.known_keys(source_id)
        return [item for item in items if _key(item) not in known]

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) AS n FROM items GROUP BY state")
        return {row["state"]: row["n"] for row in rows}

    def count_in_state(self, state: State) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE state = ?", (state.value,)
        ).fetchone()
        return int(row["n"])

    def items_in_state(self, state: State, source_id: str | None = None) -> list[Item]:
        sql = "SELECT * FROM items WHERE state = ?"
        params: list[object] = [state.value]
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        sql += " ORDER BY src_path"
        return [_row_to_item(row) for row in self.conn.execute(sql, params)]

    def total_bytes_in_state(self, state: State) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(src_size), 0) AS total FROM items WHERE state = ?",
            (state.value,),
        ).fetchone()
        return int(row["total"])

    # -- mutations -------------------------------------------------------

    def record_ingested(self, source_id: str, item: RemoteItem, spool_path: Path) -> int:
        """Record a file that is confirmed present in the spool.

        Called *after* the bytes have landed and been size-verified, never before. If
        recording fails, the next run re-downloads — wasteful but safe. The reverse
        ordering would mark a file done that was never fetched, which is data loss.
        """
        path, digest = _key(item)
        now = _now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO items (
                  source_id, src_path, src_size, src_modtime, src_hash,
                  state, spool_path, discovered_at, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_id, src_path, src_hash) DO UPDATE SET
                  state = excluded.state,
                  spool_path = excluded.spool_path,
                  ingested_at = excluded.ingested_at,
                  last_error = NULL
                """,
                (
                    source_id, path, item.size, item.mod_time, digest,
                    State.INGESTED.value, str(spool_path), now, now,
                ),
            )
            return int(cursor.lastrowid or 0)

    def record_failure(self, source_id: str, item: RemoteItem, error: str) -> None:
        path, digest = _key(item)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO items (
                  source_id, src_path, src_size, src_modtime, src_hash,
                  state, discovered_at, attempts, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT (source_id, src_path, src_hash) DO UPDATE SET
                  attempts = items.attempts + 1,
                  last_error = excluded.last_error
                """,
                (
                    source_id, path, item.size, item.mod_time, digest,
                    State.FAILED.value, _now(), error,
                ),
            )

    def forget(self, source_id: str, src_path: str, src_hash: str = "") -> None:
        """Remove an item so it will be re-ingested. Used by recovery tooling only."""
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM items WHERE source_id = ? AND src_path = ? AND src_hash = ?",
                (source_id, src_path, src_hash),
            )


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        source_id=row["source_id"],
        src_path=row["src_path"],
        src_size=row["src_size"],
        src_hash=row["src_hash"],
        state=State(row["state"]),
        spool_path=row["spool_path"],
    )

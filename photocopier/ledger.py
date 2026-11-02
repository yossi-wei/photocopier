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

SCHEMA_VERSION = 2


class State(StrEnum):
    DISCOVERED = "discovered"
    INGESTED = "ingested"
    PROCESSED = "processed"
    DELIVERED = "delivered"
    TRIAGED = "triaged"
    DISCARDED = "discarded"
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

-- One row per ingest, process, flush or resolve invocation, for the status metrics.
-- finished_at stays NULL for a run that was killed before it could report.
CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY,
  command     TEXT NOT NULL,
  scheduled   INTEGER NOT NULL,
  dry_run     INTEGER NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  exit_code   INTEGER
);

CREATE INDEX IF NOT EXISTS runs_started_idx ON runs (started_at);
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
    dest_path: str | None = None
    triage_reason: str | None = None


@dataclass(frozen=True)
class Run:
    command: str
    scheduled: bool
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None
    exit_code: int | None

    @property
    def ok(self) -> bool:
        return self.finished_at is not None and self.exit_code == 0


@dataclass(frozen=True)
class Outcomes:
    """Where every item has got to, for the success metrics."""

    delivered: int
    delivered_unassisted: int
    awaiting_review: int
    discarded: int
    processed: int
    ever_triaged: int
    oldest_undelivered: datetime | None

    @property
    def finished(self) -> int:
        """Items that left the pipeline, or are waiting on a person to decide."""
        return self.delivered + self.awaiting_review + self.discarded


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
            # Every schema change so far only adds tables, which the CREATE IF NOT
            # EXISTS above has already done for an older ledger; record that it has.
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
            "WHERE CAST(value AS INTEGER) < CAST(excluded.value AS INTEGER)",
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

    def get(self, item_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

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

    def outcomes(self) -> Outcomes:
        """triage_reason survives resolve (D11), so it marks every item a person touched."""
        row = self.conn.execute(
            """
            SELECT
              SUM(state = :delivered) AS delivered,
              SUM(state = :delivered AND triage_reason IS NULL) AS unassisted,
              SUM(state = :triaged) AS awaiting,
              SUM(state = :discarded) AS discarded,
              SUM(processed_at IS NOT NULL OR triage_reason IS NOT NULL) AS processed,
              SUM(triage_reason IS NOT NULL) AS ever_triaged,
              MIN(CASE WHEN state IN (:ingested, :processed_state) THEN ingested_at END)
                AS oldest
            FROM items
            """,
            {
                "delivered": State.DELIVERED.value,
                "triaged": State.TRIAGED.value,
                "discarded": State.DISCARDED.value,
                "ingested": State.INGESTED.value,
                "processed_state": State.PROCESSED.value,
            },
        ).fetchone()
        return Outcomes(
            delivered=row["delivered"] or 0,
            delivered_unassisted=row["unassisted"] or 0,
            awaiting_review=row["awaiting"] or 0,
            discarded=row["discarded"] or 0,
            processed=row["processed"] or 0,
            ever_triaged=row["ever_triaged"] or 0,
            oldest_undelivered=_parse_time(row["oldest"]),
        )

    def runs(self, since: datetime | None = None) -> list[Run]:
        sql = "SELECT * FROM runs"
        params: list[object] = []
        if since is not None:
            sql += " WHERE started_at >= ?"
            params.append(since.astimezone(UTC).isoformat(timespec="seconds"))
        sql += " ORDER BY started_at, id"
        return [
            Run(
                command=row["command"],
                scheduled=bool(row["scheduled"]),
                dry_run=bool(row["dry_run"]),
                started_at=datetime.fromisoformat(row["started_at"]),
                finished_at=_parse_time(row["finished_at"]),
                exit_code=row["exit_code"],
            )
            for row in self.conn.execute(sql, params)
        ]

    # -- mutations -------------------------------------------------------

    def start_run(
        self, command: str, *, scheduled: bool, dry_run: bool, started_at: datetime | None = None
    ) -> int:
        when = (started_at or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (command, scheduled, dry_run, started_at) VALUES (?, ?, ?, ?)",
                (command, int(scheduled), int(dry_run), when),
            )
            return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, exit_code: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, exit_code = ? WHERE id = ?",
                (_now(), exit_code, run_id),
            )

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

    def mark_processed(
        self,
        item_id: int,
        *,
        spool_path: Path,
        dest_path: str,
        resolved_date: str,
        date_source: str,
    ) -> None:
        """Record a file that is confirmed present at its outbox location.

        Called after the rename, never before — same ordering rule as ingest. If this
        write is lost, the file is simply reprocessed, which is idempotent.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE items SET
                  state = ?, spool_path = ?, dest_path = ?,
                  resolved_date = ?, date_source = ?, processed_at = ?,
                  triage_reason = NULL, last_error = NULL
                WHERE id = ?
                """,
                (
                    State.PROCESSED.value, str(spool_path), dest_path,
                    resolved_date, date_source, _now(), item_id,
                ),
            )

    def mark_triaged(
        self,
        item_id: int,
        *,
        spool_path: Path,
        reason: str,
        date_source: str = "none",
    ) -> None:
        """Park an item for human review. Triaged items are never delivered."""
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE items SET
                  state = ?, spool_path = ?, triage_reason = ?,
                  date_source = ?, processed_at = ?, dest_path = NULL
                WHERE id = ?
                """,
                (
                    State.TRIAGED.value, str(spool_path), reason,
                    date_source, _now(), item_id,
                ),
            )

    def mark_delivered(self, item_id: int, *, dest_path: str) -> None:
        """Record a file confirmed present and verified at its destination.

        spool_path is cleared: the item no longer occupies the spool, and this is what
        makes the emptied spool safe. The row remains as the memory that stops ingest
        fetching the file again. See D1.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE items SET
                  state = ?, spool_path = NULL, dest_path = ?,
                  delivered_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (State.DELIVERED.value, dest_path, _now(), item_id),
            )

    def mark_discarded(self, item_id: int) -> None:
        """Close out a triaged item that a human decided not to keep.

        The row stays, which is the point: known_keys() counts every state except
        FAILED, so ingest never fetches the file again. triage_reason is kept as the
        record of why it never reached the library.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE items SET
                  state = ?, spool_path = NULL, dest_path = NULL, last_error = NULL
                WHERE id = ?
                """,
                (State.DISCARDED.value, item_id),
            )

    def record_error(self, item_id: int, error: str) -> None:
        """Note a failure without changing state, so the item is retried as-is."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE items SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error, item_id),
            )

    def triage_reasons(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT triage_reason AS reason, COUNT(*) AS n FROM items "
            "WHERE state = ? AND triage_reason IS NOT NULL GROUP BY triage_reason",
            (State.TRIAGED.value,),
        )
        return {row["reason"]: row["n"] for row in rows}

    def date_sources(self) -> dict[str, int]:
        """How each processed item got its date. The tier mix is a health signal."""
        rows = self.conn.execute(
            "SELECT date_source AS src, COUNT(*) AS n FROM items "
            "WHERE date_source IS NOT NULL GROUP BY date_source",
        )
        return {row["src"]: row["n"] for row in rows}

    def dest_path_taken(self, dest_path: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM items WHERE dest_path = ? LIMIT 1", (dest_path,)
        ).fetchone()
        return row is not None

    def forget(self, source_id: str, src_path: str, src_hash: str = "") -> None:
        """Remove an item so it will be re-ingested. Used by recovery tooling only."""
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM items WHERE source_id = ? AND src_path = ? AND src_hash = ?",
                (source_id, src_path, src_hash),
            )


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        source_id=row["source_id"],
        src_path=row["src_path"],
        src_size=row["src_size"],
        src_hash=row["src_hash"],
        state=State(row["state"]),
        spool_path=row["spool_path"],
        dest_path=row["dest_path"],
        triage_reason=row["triage_reason"],
    )

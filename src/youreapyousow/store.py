"""SQLite persistence: versioned current-state records beside the append-only ledger.

The ledger is the audit trail; the records table holds the current state of each
objective, grant, quote, intent and deployment. Both live in one database so a state
change and the event that records it commit in the same transaction and can never
diverge.
"""

import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    objective_id TEXT,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS records_by_objective ON records (kind, objective_id);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    at TEXT NOT NULL,
    objective_id TEXT,
    subject_id TEXT NOT NULL,
    refs TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_objective ON events (objective_id, seq);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""


class StaleRecordError(RuntimeError):
    """Raised when a compare-and-set update finds a newer version than expected."""


class RecordNotFoundError(KeyError):
    """Raised when a required record does not exist."""


class Database:
    """A single SQLite database shared by the record store and the ledger.

    Writes are serialised through one re-entrant lock and ``BEGIN IMMEDIATE``
    transactions, which is ample for one control-plane process.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        """Open (and if needed create) the database.

        Args:
            path: A filesystem path, or ``":memory:"`` for an ephemeral database.
        """
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Run a block atomically; nested calls join the outer transaction.

        Yields:
            The underlying connection.

        Raises:
            BaseException: Whatever the block raised, after rolling back.
        """
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outermost:
                self._conn.execute("COMMIT")

    def read(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        """Run a read-only query.

        Args:
            sql: The SELECT statement.
            params: Positional parameters.

        Returns:
            All matching rows.
        """
        with self._lock:
            cursor = self._conn.execute(sql, params)
            cursor.row_factory = sqlite3.Row
            return list(cursor.fetchall())

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()


class Records[T: BaseModel]:
    """Typed, versioned current-state storage for one kind of record."""

    def __init__(self, db: Database, kind: str, model: type[T]) -> None:
        """Bind a record kind to its pydantic model.

        Args:
            db: The shared database.
            kind: The kind name stored in the table, such as ``intent``.
            model: The pydantic model the bodies are parsed into.
        """
        self._db = db
        self._kind = kind
        self._model = model

    def insert(self, record: T, *, record_id: str, objective_id: str | None) -> None:
        """Store a new record at version 1.

        Args:
            record: The record to store.
            record_id: Its identifier.
            objective_id: The objective it belongs to, for listing, if any.
        """
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO records (kind, id, objective_id, version, body)"
                " VALUES (?, ?, ?, 1, ?)",
                (self._kind, record_id, objective_id, record.model_dump_json()),
            )

    def get(self, record_id: str) -> tuple[T, int] | None:
        """Fetch a record with its version.

        Args:
            record_id: The identifier.

        Returns:
            The record and its version, or None if absent.
        """
        rows = self._db.read(
            "SELECT body, version FROM records WHERE kind = ? AND id = ?", (self._kind, record_id)
        )
        if not rows:
            return None
        return self._model.model_validate_json(rows[0]["body"]), int(rows[0]["version"])

    def require(self, record_id: str) -> tuple[T, int]:
        """Fetch a record that must exist.

        Args:
            record_id: The identifier.

        Returns:
            The record and its version.

        Raises:
            RecordNotFoundError: If there is no such record.
        """
        found = self.get(record_id)
        if found is None:
            raise RecordNotFoundError(f"{self._kind} {record_id} not found")
        return found

    def update(self, record: T, *, record_id: str, expected_version: int) -> int:
        """Replace a record if nobody else has changed it since it was read.

        Args:
            record: The new body.
            record_id: The identifier.
            expected_version: The version the caller read.

        Returns:
            The new version.

        Raises:
            StaleRecordError: If the stored version differs from ``expected_version``.
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE records SET body = ?, version = version + 1 "
                "WHERE kind = ? AND id = ? AND version = ?",
                (record.model_dump_json(), self._kind, record_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StaleRecordError(
                    f"{self._kind} {record_id} changed since version {expected_version}"
                )
        return expected_version + 1

    def list(self, *, objective_id: str | None = None) -> list[T]:
        """List records of this kind, oldest first.

        Args:
            objective_id: Restrict to one objective when given.

        Returns:
            The matching records.
        """
        if objective_id is None:
            rows = self._db.read(
                "SELECT body FROM records WHERE kind = ? ORDER BY rowid", (self._kind,)
            )
        else:
            rows = self._db.read(
                "SELECT body FROM records WHERE kind = ? AND objective_id = ? ORDER BY rowid",
                (self._kind, objective_id),
            )
        return [self._model.model_validate_json(row["body"]) for row in rows]

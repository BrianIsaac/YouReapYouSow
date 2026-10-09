"""The append-only, hash-chained ledger."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from youreapyousow.clock import Clock, utc_now
from youreapyousow.ids import new_id
from youreapyousow.ledger.events import GENESIS_HASH, EventType, LedgerEvent
from youreapyousow.store import Database


def _event_hash(
    *,
    event_id: str,
    type_: EventType,
    at: datetime,
    objective_id: str | None,
    subject_id: str,
    refs: dict[str, str],
    payload: dict[str, JsonValue],
    prev_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "event_id": event_id,
            "type": type_.value,
            "at": at.isoformat(),
            "objective_id": objective_id,
            "subject_id": subject_id,
            "refs": refs,
            "payload": payload,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ChainVerification:
    """Result of re-deriving every hash on the ledger.

    Attributes:
        ok: True when every event's hash and link are intact.
        events_checked: How many events were verified.
        first_bad_seq: The first event whose hash or link is wrong, if any.
    """

    ok: bool
    events_checked: int
    first_bad_seq: int | None = None


class Ledger:
    """Append-only event log; SQLite triggers refuse any update or delete."""

    def __init__(self, db: Database, clock: Clock = utc_now) -> None:
        """Bind the ledger to its database.

        Args:
            db: The shared database.
            clock: Time source for event timestamps.
        """
        self._db = db
        self._clock = clock

    def append(
        self,
        type_: EventType,
        *,
        subject_id: str,
        objective_id: str | None,
        refs: dict[str, str] | None = None,
        payload: dict[str, JsonValue] | None = None,
    ) -> LedgerEvent:
        """Append one event, chained to the previous one.

        Joins the caller's transaction when there is one, so a record change and its
        event commit together.

        Args:
            type_: What happened.
            subject_id: The record the event is about.
            objective_id: The objective it serves, if any.
            refs: Links to other records by kind.
            payload: Event-specific detail; must be JSON-serialisable.

        Returns:
            The stored event with its sequence number and hash.
        """
        refs = dict(refs or {})
        payload = dict(payload or {})
        event_id = new_id("evt")
        at = self._clock()
        with self._db.transaction() as conn:
            row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = str(row[0]) if row else GENESIS_HASH
            digest = _event_hash(
                event_id=event_id,
                type_=type_,
                at=at,
                objective_id=objective_id,
                subject_id=subject_id,
                refs=refs,
                payload=payload,
                prev_hash=prev_hash,
            )
            cursor = conn.execute(
                "INSERT INTO events (event_id, type, at, objective_id, subject_id, refs, payload,"
                " prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    type_.value,
                    at.isoformat(),
                    objective_id,
                    subject_id,
                    json.dumps(refs, sort_keys=True),
                    json.dumps(payload, sort_keys=True),
                    prev_hash,
                    digest,
                ),
            )
            seq = int(cursor.lastrowid or 0)
        return LedgerEvent(
            seq=seq,
            event_id=event_id,
            type=type_,
            at=at,
            objective_id=objective_id,
            subject_id=subject_id,
            refs=refs,
            payload=payload,
            prev_hash=prev_hash,
            hash=digest,
        )

    def events(
        self,
        *,
        objective_id: str | None = None,
        types: Iterable[EventType] | None = None,
        after_seq: int = 0,
    ) -> list[LedgerEvent]:
        """Read events in ledger order.

        Args:
            objective_id: Restrict to one objective when given.
            types: Restrict to these event types when given.
            after_seq: Only events with a larger sequence number.

        Returns:
            The matching events, oldest first.
        """
        sql = "SELECT * FROM events WHERE seq > ?"
        params: list[object] = [after_seq]
        if objective_id is not None:
            sql += " AND objective_id = ?"
            params.append(objective_id)
        wanted = [t.value for t in types] if types is not None else None
        if wanted is not None:
            sql += f" AND type IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        rows = self._db.read(sql + " ORDER BY seq", tuple(params))
        return [
            LedgerEvent(
                seq=int(row["seq"]),
                event_id=row["event_id"],
                type=EventType(row["type"]),
                at=datetime.fromisoformat(row["at"]),
                objective_id=row["objective_id"],
                subject_id=row["subject_id"],
                refs=json.loads(row["refs"]),
                payload=json.loads(row["payload"]),
                prev_hash=row["prev_hash"],
                hash=row["hash"],
            )
            for row in rows
        ]

    def verify_chain(self) -> ChainVerification:
        """Recompute every hash and link from the genesis hash onwards.

        Returns:
            Whether the chain is intact and, if not, where it first breaks.
        """
        prev_hash = GENESIS_HASH
        checked = 0
        for event in self.events():
            expected = _event_hash(
                event_id=event.event_id,
                type_=event.type,
                at=event.at,
                objective_id=event.objective_id,
                subject_id=event.subject_id,
                refs=event.refs,
                payload=event.payload,
                prev_hash=prev_hash,
            )
            if event.prev_hash != prev_hash or event.hash != expected:
                return ChainVerification(ok=False, events_checked=checked, first_bad_seq=event.seq)
            prev_hash = event.hash
            checked += 1
        return ChainVerification(ok=True, events_checked=checked)

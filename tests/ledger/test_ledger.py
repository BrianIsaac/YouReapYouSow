"""Tests for the append-only ledger."""

import sqlite3

import pytest

from youreapyousow.clock import ManualClock
from youreapyousow.ledger.events import GENESIS_HASH, EventType
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.store import Database


def test_events_are_ordered_and_chained(ledger: Ledger, clock: ManualClock) -> None:
    """Sequence numbers increase and every event links to its predecessor."""
    first = ledger.append(EventType.OBJECTIVE_CREATED, subject_id="obj_1", objective_id="obj_1")
    clock.advance(seconds=1)
    second = ledger.append(
        EventType.QUOTE_CREATED,
        subject_id="quo_1",
        objective_id="obj_1",
        refs={"offer": "vast:1"},
        payload={"amount_usd": "1.46"},
    )

    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.hash
    assert second.seq > first.seq
    assert [e.event_id for e in ledger.events()] == [first.event_id, second.event_id]
    assert ledger.events()[1] == second


def test_filters_by_objective_type_and_sequence(ledger: Ledger) -> None:
    """Reads can be narrowed by objective, type and position."""
    a = ledger.append(EventType.OBJECTIVE_CREATED, subject_id="obj_a", objective_id="obj_a")
    ledger.append(EventType.OBJECTIVE_CREATED, subject_id="obj_b", objective_id="obj_b")
    ledger.append(EventType.QUOTE_CREATED, subject_id="quo", objective_id="obj_a")

    assert [e.subject_id for e in ledger.events(objective_id="obj_a")] == ["obj_a", "quo"]
    assert [e.subject_id for e in ledger.events(types=[EventType.QUOTE_CREATED])] == ["quo"]
    assert len(ledger.events(after_seq=a.seq)) == 2


def test_update_and_delete_are_refused(db: Database, ledger: Ledger) -> None:
    """The database itself refuses to rewrite history."""
    ledger.append(EventType.OBJECTIVE_CREATED, subject_id="obj_1", objective_id="obj_1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), db.transaction() as conn:
        conn.execute("UPDATE events SET type = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), db.transaction() as conn:
        conn.execute("DELETE FROM events")
    assert len(ledger.events()) == 1


def test_verify_chain_detects_tampering(db: Database, ledger: Ledger) -> None:
    """Rewriting a payload behind the triggers' back breaks the chain at that event."""
    ledger.append(EventType.OBJECTIVE_CREATED, subject_id="obj_1", objective_id="obj_1")
    tampered = ledger.append(
        EventType.QUOTE_CREATED, subject_id="q", objective_id="obj_1", payload={"amount": "1"}
    )
    assert ledger.verify_chain().ok
    assert ledger.verify_chain().events_checked == 2

    with db.transaction() as conn:
        conn.execute("DROP TRIGGER events_no_update")
        conn.execute(
            'UPDATE events SET payload = \'{"amount": "9"}\' WHERE seq = ?', (tampered.seq,)
        )
    result = ledger.verify_chain()
    assert not result.ok
    assert result.first_bad_seq == tampered.seq


def test_rolled_back_transaction_leaves_no_event(db: Database, ledger: Ledger) -> None:
    """An event appended inside a failed transaction is not kept."""

    def append_then_fail() -> None:
        with db.transaction():
            ledger.append(EventType.OBJECTIVE_CREATED, subject_id="obj_1", objective_id="obj_1")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        append_then_fail()
    assert ledger.events() == []

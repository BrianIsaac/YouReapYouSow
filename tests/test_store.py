"""Tests for the versioned record store."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from youreapyousow.store import Database, RecordNotFoundError, Records, StaleRecordError


class Thing(BaseModel):
    """A trivial record."""

    name: str


def test_insert_get_and_list_by_objective(db: Database) -> None:
    """Records round-trip and list per objective in insertion order."""
    things = Records(db, "thing", Thing)
    things.insert(Thing(name="a"), record_id="t1", objective_id="o1")
    things.insert(Thing(name="b"), record_id="t2", objective_id="o2")
    things.insert(Thing(name="c"), record_id="t3", objective_id="o1")

    assert things.get("t1") == (Thing(name="a"), 1)
    assert things.get("missing") is None
    assert [t.name for t in things.list(objective_id="o1")] == ["a", "c"]
    assert [t.name for t in things.list()] == ["a", "b", "c"]


def test_update_is_compare_and_set(db: Database) -> None:
    """An update with a stale version is refused; a fresh one bumps the version."""
    things = Records(db, "thing", Thing)
    things.insert(Thing(name="a"), record_id="t1", objective_id=None)

    assert things.update(Thing(name="b"), record_id="t1", expected_version=1) == 2
    with pytest.raises(StaleRecordError):
        things.update(Thing(name="c"), record_id="t1", expected_version=1)
    assert things.require("t1") == (Thing(name="b"), 2)


def test_require_raises_for_missing(db: Database) -> None:
    """Requiring an absent record raises."""
    with pytest.raises(RecordNotFoundError):
        Records(db, "thing", Thing).require("nope")


def test_transaction_rolls_back_every_write(db: Database) -> None:
    """A failure inside a transaction undoes nested writes too."""
    things = Records(db, "thing", Thing)

    def write_then_fail() -> None:
        with db.transaction():
            things.insert(Thing(name="a"), record_id="t1", objective_id=None)
            with db.transaction():
                things.insert(Thing(name="b"), record_id="t2", objective_id=None)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        write_then_fail()
    assert things.list() == []


def test_file_database_persists(tmp_path: Path) -> None:
    """A file-backed database keeps records across connections."""
    path = tmp_path / "nested" / "cp.db"
    first = Database(path)
    Records(first, "thing", Thing).insert(Thing(name="a"), record_id="t1", objective_id=None)
    first.close()
    assert Records(Database(path), "thing", Thing).require("t1")[0].name == "a"

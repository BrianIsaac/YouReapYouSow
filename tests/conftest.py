"""Shared fixtures."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from youreapyousow.clock import ManualClock
from youreapyousow.config import Settings
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.store import Database

START = datetime(2037, 10, 9, 8, 42, 3, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_from_operator_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep every test away from the operator's ``.env`` and exported settings.

    An operator's ``.env`` may hold real sandbox keys; no test may pick them up, and no
    test reaches a model server unless it names one (``MODEL_PROVIDER=none``).
    ``REAP_PURCHASE_PATH`` is set to ``card`` for every test (see below).
    """
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv("PURCHASE_CONFIG", raising=False)
    # No model unless a test names one: unset, the provider would be found by asking
    # the local server, so a running llama-server would answer the suite's live paths.
    monkeypatch.setenv("MODEL_PROVIDER", "none")
    # The card path is the suite's default; a test of the agentic path, or of the true
    # default, chooses it explicitly.
    monkeypatch.setenv("REAP_PURCHASE_PATH", "card")


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only.

    Returns:
        The anyio backend name.
    """
    return "asyncio"


@pytest.fixture
def clock() -> ManualClock:
    """A manual clock starting at the demo's spike time.

    Returns:
        The clock.
    """
    return ManualClock(START)


@pytest.fixture
def db() -> Database:
    """An in-memory database.

    Returns:
        The database.
    """
    return Database()


@pytest.fixture
def ledger(db: Database, clock: ManualClock) -> Ledger:
    """A ledger on the in-memory database, driven by the manual clock.

    Returns:
        The ledger.
    """
    return Ledger(db, clock)

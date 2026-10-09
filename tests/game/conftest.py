"""A game service on an in-memory database and a manual clock."""

from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import START
from youreapyousow.clock import ManualClock
from youreapyousow.game.models import GroupTerms
from youreapyousow.game.service import GameService
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.store import Database

TERMS = GroupTerms(title="Earn your Keychron B40", entry_amount=Decimal("25.00"))


class Game:
    """A service with its ledger and clock, for one test."""

    def __init__(
        self,
        *,
        vault_balance: Decimal = Decimal("200.00"),
        terms: GroupTerms = TERMS,
        evidence_dir: Path | None = None,
    ) -> None:
        """Build the service and open the group.

        Args:
            vault_balance: The vault's test USDC.
            terms: The group's terms.
            evidence_dir: Where evidence is stored.
        """
        self.clock = ManualClock(START)
        self.db = Database()
        self.ledger = Ledger(self.db, self.clock)
        self.service = GameService(
            db=self.db,
            ledger=self.ledger,
            clock=self.clock,
            terms=terms,
            vault_balance=vault_balance,
            vault_source="test vault",
            evidence_dir=evidence_dir or Path("evidence"),
        )
        self.service.open_group()


@pytest.fixture
def game() -> Game:
    """A fresh game.

    Returns:
        The game.
    """
    return Game()


@pytest.fixture
def game_factory() -> type[Game]:
    """Build games with other settings.

    Returns:
        The class.
    """
    return Game

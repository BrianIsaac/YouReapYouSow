"""The group: seats, entries against the vault, the pool, and the enrolment deadline."""

from decimal import Decimal

import pytest

from tests.game.conftest import Game
from youreapyousow.game.models import EntryState, GroupStatus
from youreapyousow.game.service import GameError, compute_pool
from youreapyousow.ledger.events import EventType


def test_a_group_opens_for_joining_with_its_terms_on_the_ledger(game: Game) -> None:
    """Opening appends ``group.opened`` with the terms and the rubric version."""
    group = game.service.group()
    assert group.status == GroupStatus.OPEN_FOR_JOINING
    opened = game.ledger.events(types=[EventType.GROUP_OPENED])
    assert len(opened) == 1
    assert opened[0].payload["entry_amount"] == "25.00"
    assert opened[0].payload["rubric_version"] == "rubric-v1"


def test_joining_reserves_a_seat_and_an_entry_against_the_vault(game: Game) -> None:
    """Each join is a numbered seat and an ``entry.reserved`` with the vault's balance."""
    alice = game.service.join("Alice")
    bob = game.service.join("Bob")
    assert (alice.seat, bob.seat) == (1, 2)
    assert alice.entry == EntryState.RESERVED
    reserved = game.ledger.events(types=[EventType.ENTRY_RESERVED])
    assert [e.subject_id for e in reserved] == [alice.id, bob.id]
    assert reserved[1].payload["amount"] == "25.00"
    assert reserved[1].payload["reserved_total"] == "50.00"
    assert reserved[1].payload["vault_balance"] == "200.00"
    assert game.service.group().status == GroupStatus.OPEN_FOR_JOINING


def test_the_last_seat_moves_the_group_to_intake_and_closes_joining(game: Game) -> None:
    """The third join fills the group; a fourth is refused."""
    for name in ("Alice", "Bob", "Chloe"):
        game.service.join(name)
    assert game.service.group().status == GroupStatus.INTAKE
    with pytest.raises(GameError) as refused:
        game.service.join("Dan")
    assert refused.value.code == "GROUP_FULL"


def test_an_entry_the_vault_cannot_cover_is_refused(game_factory: type[Game]) -> None:
    """Reservations never exceed the vault's balance."""
    game = game_factory(vault_balance=Decimal("40.00"))
    game.service.join("Alice")
    with pytest.raises(GameError) as refused:
        game.service.join("Bob")
    assert refused.value.code == "VAULT_INSUFFICIENT"
    assert len(game.service.players()) == 1


def test_a_blank_name_is_refused(game: Game) -> None:
    """A seat needs a display name."""
    with pytest.raises(GameError) as refused:
        game.service.join("   ")
    assert refused.value.code == "INVALID_NAME"


def test_a_missed_enrolment_deadline_cancels_and_refunds_every_entry(game: Game) -> None:
    """Short of the quorum at the deadline: refunding, each entry refunded, cancelled."""
    alice = game.service.join("Alice")
    bob = game.service.join("Bob")
    game.clock.advance(seconds=3601)
    game.service.tick()
    group = game.service.group()
    assert group.status == GroupStatus.CANCELLED
    assert group.cancelled_reason is not None
    assert all(p.entry == EntryState.REFUNDED for p in game.service.players())
    types = [e.type for e in game.ledger.events()]
    assert types[-4:] == [
        EventType.GROUP_REFUNDING,
        EventType.ENTRY_REFUNDED,
        EventType.ENTRY_REFUNDED,
        EventType.GROUP_CANCELLED,
    ]
    refunded = game.ledger.events(types=[EventType.ENTRY_REFUNDED])
    assert {e.subject_id for e in refunded} == {alice.id, bob.id}
    assert game.ledger.verify_chain().ok


def test_the_pool_discloses_gross_buffer_ceiling_and_surplus() -> None:
    """Three entries of 25.00 over a 57.49 quote: a ceiling of 67.50, 10.01 to spare."""
    pool = compute_pool(Decimal("25.00"), 3, Decimal("0.10"), Decimal("57.49"))
    assert pool.gross == Decimal("75.00")
    assert pool.buffer == Decimal("7.50")
    assert pool.ceiling == Decimal("67.50")
    assert pool.surplus == Decimal("10.01")
    assert compute_pool(Decimal("25.00"), 0, Decimal("0.10"), None).surplus is None


def test_reset_opens_a_fresh_group(game: Game) -> None:
    """A reset leaves the old group on the ledger and opens a new one."""
    first = game.service.group().id
    game.service.join("Alice")
    game.service.reset()
    assert game.service.group().id != first
    assert game.service.players() == []
    assert len(game.ledger.events(types=[EventType.GROUP_OPENED])) == 2

"""Acceptance: unanimous, on locked contracts and funded seats, then the timer starts."""

from datetime import timedelta

import pytest

from tests.game.conftest import Game
from tests.game.helpers import ready_group
from youreapyousow.game.models import ContractStatus, EntryState, GroupStatus
from youreapyousow.game.service import GameError
from youreapyousow.ledger.events import EventType


def test_the_challenge_starts_at_its_fixed_start_once_everyone_accepts(game: Game) -> None:
    """Three accepts, then the published start: rubric locked, ACTIVE, the end fixed."""
    published = game.service.group()
    assert published.starts_at == game.clock() + timedelta(seconds=3600)
    assert published.starts_at is not None
    assert published.ends_at == published.starts_at + timedelta(seconds=28 * 60 / 7)
    ids = ready_group(game)
    for pid in ids:
        game.service.accept(pid)
    assert game.service.group().status == GroupStatus.READY_FOR_ACCEPTANCE
    game.clock.advance(seconds=3600)
    game.service.tick()
    group = game.service.group()
    assert group.status == GroupStatus.ACTIVE
    assert group.started_at == published.starts_at
    assert group.rubric_locked_at == published.starts_at
    assert group.ends_at == published.ends_at
    assert all(
        p.contract is not None and p.contract.status == ContractStatus.ACCEPTED
        for p in game.service.players()
    )
    types = [e.type for e in game.ledger.events()][-5:]
    assert types == [
        EventType.CONTRACT_ACCEPTED,
        EventType.CONTRACT_ACCEPTED,
        EventType.CONTRACT_ACCEPTED,
        EventType.RUBRIC_LOCKED,
        EventType.GROUP_STARTED,
    ]
    rubric = game.ledger.events(types=[EventType.RUBRIC_LOCKED])[0]
    assert rubric.payload["version"] == "rubric-v1"
    assert rubric.payload["milestone_points"] == [15, 20, 25, 40]


def test_accepting_twice_is_refused(game: Game) -> None:
    """One accept per player."""
    ids = ready_group(game)
    game.service.accept(ids[0])
    with pytest.raises(GameError) as refused:
        game.service.accept(ids[0])
    assert refused.value.code == "ALREADY_ACCEPTED"


def test_accept_before_every_contract_is_locked_is_refused(game: Game) -> None:
    """The agreement opens only when the group is ready."""
    ids = [game.service.join(n).id for n in ("Alice", "Ben", "Chloe")]
    with pytest.raises(GameError) as refused:
        game.service.accept(ids[0])
    assert refused.value.code == "WRONG_STATE"


def test_a_decline_cancels_and_refunds(game: Game) -> None:
    """One player declining the agreement cancels the group and refunds every entry."""
    ids = ready_group(game)
    game.service.accept(ids[0])
    game.service.decline(ids[1])
    assert game.service.group().status == GroupStatus.CANCELLED
    assert all(p.entry == EntryState.REFUNDED for p in game.service.players())
    assert len(game.ledger.events(types=[EventType.ENTRY_REFUNDED])) == 3


def test_a_missed_acceptance_deadline_cancels_and_refunds(game: Game) -> None:
    """Two accepts and the deadline passes: cancelled, refunded."""
    ids = ready_group(game)
    game.service.accept(ids[0])
    game.service.accept(ids[1])
    game.clock.advance(seconds=3601)
    with pytest.raises(GameError):
        game.service.accept(ids[2])
    assert game.service.group().status == GroupStatus.CANCELLED

"""Check-ins: the rubric decides, the vision model advises, the leaderboard is folded."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tests.game.conftest import Game
from tests.game.helpers import active_group
from tests.game.test_coach import FakeChat
from youreapyousow.featherless import ModelError
from youreapyousow.game.llm import Link
from youreapyousow.game.models import EvidenceKind, ScoreState
from youreapyousow.game.rubric import standings
from youreapyousow.game.service import EvidenceIn, GameError
from youreapyousow.game.verifier import Verifier
from youreapyousow.ledger.events import EventType

pytestmark = pytest.mark.anyio

PHOTO = EvidenceIn(content=b"\xff\xd8 a photo of push-ups", content_type="image/jpeg")
WEEK = 60.0


@pytest.fixture
def game(tmp_path: Path) -> Game:
    """A game whose evidence lands in a temporary folder.

    Args:
        tmp_path: The folder.

    Returns:
        The game.
    """
    return Game(evidence_dir=tmp_path / "evidence")


def test_a_photo_meeting_the_target_is_verified_for_the_milestones_points(game: Game) -> None:
    """Alice's 9 reps against milestone 1's 8: verified, 15 points, stored and on the ledger."""
    alice, *_ = active_group(game)
    event = game.service.checkin(alice, milestone=0, value=Decimal(9), evidence=PHOTO)
    assert event.state == ScoreState.VERIFIED
    assert event.delta == 15
    assert event.rubric_version == "rubric-v1"
    assert event.evidence_kind == EvidenceKind.PHOTO
    assert event.evidence_id is not None
    stored = game.service.evidence_path(event.evidence_id)
    assert stored is not None
    assert stored.read_bytes() == PHOTO.content
    recorded = game.ledger.events(types=[EventType.SCORE_RECORDED])[-1]
    assert recorded.subject_id == event.event_id
    assert recorded.payload["delta"] == 15


def test_a_value_below_the_target_is_rejected_with_no_points(game: Game) -> None:
    """The rubric gives no partial credit."""
    alice, *_ = active_group(game)
    event = game.service.checkin(alice, milestone=0, value=Decimal(7), evidence=PHOTO)
    assert event.state == ScoreState.REJECTED
    assert event.delta == 0
    assert "below" in event.reason


def test_a_photo_goal_without_media_is_refused_and_a_log_goal_takes_a_log(game: Game) -> None:
    """Alice's contract needs a photo or clip; Ben's runs take his own log."""
    alice, ben, _ = active_group(game)
    with pytest.raises(GameError) as refused:
        game.service.checkin(alice, milestone=0, value=Decimal(9), evidence=None)
    assert refused.value.code == "EVIDENCE_REQUIRED"
    logged = game.service.checkin(ben, milestone=0, value=Decimal(1), evidence=None, note="5 km")
    assert logged.state == ScoreState.VERIFIED
    assert logged.evidence_kind == EvidenceKind.LOG


def test_a_later_milestone_waits_for_its_window(game: Game) -> None:
    """Milestone 2 opens on milestone 1's day: one demo week in."""
    alice, *_ = active_group(game)
    with pytest.raises(GameError) as early:
        game.service.checkin(alice, milestone=1, value=Decimal(11), evidence=PHOTO)
    assert early.value.code == "MILESTONE_NOT_OPEN"
    game.clock.advance(seconds=WEEK + 1)
    event = game.service.checkin(alice, milestone=1, value=Decimal(11), evidence=PHOTO)
    assert event.delta == 20


def test_a_milestone_scores_once_and_the_same_file_twice_is_refused(game: Game) -> None:
    """Points are capped per period; replayed evidence is rejected by its hash."""
    alice, _, chloe = active_group(game)
    game.service.checkin(alice, milestone=0, value=Decimal(9), evidence=PHOTO)
    game.clock.advance(seconds=11)
    with pytest.raises(GameError) as again:
        game.service.checkin(
            alice, milestone=0, value=Decimal(9), evidence=EvidenceIn(b"other", "image/png")
        )
    assert again.value.code == "MILESTONE_ALREADY_SCORED"
    with pytest.raises(GameError) as replayed:
        game.service.checkin(chloe, milestone=0, value=Decimal(1), evidence=PHOTO)
    assert replayed.value.code == "DUPLICATE_EVIDENCE"


def test_check_ins_are_rate_limited(game: Game) -> None:
    """One check-in per player every ten seconds."""
    alice, *_ = active_group(game)
    game.service.checkin(alice, milestone=0, value=Decimal(7), evidence=PHOTO)
    with pytest.raises(GameError) as limited:
        game.service.checkin(
            alice, milestone=0, value=Decimal(9), evidence=EvidenceIn(b"b", "image/jpeg")
        )
    assert limited.value.code == "RATE_LIMITED"
    game.clock.advance(seconds=10)
    retry = game.service.checkin(
        alice, milestone=0, value=Decimal(9), evidence=EvidenceIn(b"b", "image/jpeg")
    )
    assert retry.state == ScoreState.VERIFIED


def test_check_ins_close_when_the_challenge_ends(game: Game) -> None:
    """After four demo weeks no check-in is taken."""
    alice, *_ = active_group(game)
    game.clock.advance(seconds=4 * WEEK + 1)
    with pytest.raises(GameError) as closed:
        game.service.checkin(alice, milestone=0, value=Decimal(9), evidence=PHOTO)
    assert closed.value.code == "WRONG_STATE"


def test_the_leaderboard_is_recomputed_from_the_ledger_alone(game: Game) -> None:
    """Folding the score events read back from the ledger gives the same standings."""
    alice, ben, chloe = active_group(game)
    game.service.checkin(alice, milestone=0, value=Decimal(9), evidence=PHOTO)
    game.service.checkin(ben, milestone=0, value=Decimal(1), evidence=None)
    game.clock.advance(seconds=WEEK + 1)
    game.service.checkin(ben, milestone=1, value=Decimal(2), evidence=None)
    game.service.checkin(
        chloe, milestone=0, value=Decimal(0), evidence=EvidenceIn(b"c", "video/mp4")
    )
    board = standings(game.service.players(), game.service.score_events())
    assert [(s.name, s.score) for s in board] == [("Ben", 35), ("Alice", 15), ("Chloe", 0)]
    replayed = game.service.replay_scores()
    assert replayed == game.service.score_events()


def test_the_tie_break_prefers_the_earlier_last_verified_check_in(game: Game) -> None:
    """Equal 15 points each: Ben, who got there first, ranks above Alice."""
    alice, ben, _ = active_group(game)
    game.service.checkin(ben, milestone=0, value=Decimal(1), evidence=None)
    game.clock.advance(seconds=5)
    game.service.checkin(alice, milestone=0, value=Decimal(9), evidence=PHOTO)
    board = standings(game.service.players(), game.service.score_events())
    assert [(s.name, s.score) for s in board[:2]] == [("Ben", 15), ("Alice", 15)]


async def test_the_vision_reading_is_advisory_and_never_the_score(game: Game) -> None:
    """The model says 6 reps; the rubric still verifies the claimed 9, marked as disagreeing."""
    alice, *_ = active_group(game)
    contract = game.service.player(alice).record.contract
    assert contract is not None
    reply = json.dumps({"shows": True, "count": 6, "note": "six push-ups visible"})
    chat = FakeChat("m", [reply])
    verifier = Verifier([Link("openai:gpt-5.6-terra", chat)])
    advisory = await verifier.read(PHOTO, contract, 0, Decimal(9))
    assert advisory.model == "openai:gpt-5.6-terra"
    assert advisory.agrees is False
    sent = chat.seen[0][-1]["content"]
    assert isinstance(sent, list)
    event = game.service.checkin(
        alice, milestone=0, value=Decimal(9), evidence=PHOTO, advisory=advisory
    )
    assert event.state == ScoreState.VERIFIED
    assert event.advisory is not None
    assert event.advisory.count == 6


async def test_an_unreachable_vision_model_leaves_a_note() -> None:
    """No model: the advisory says so and the rubric decides alone."""
    verifier = Verifier([Link("x", FakeChat("m", [ModelError("down")]))])
    game = Game()
    alice, *_ = active_group(game)
    contract = game.service.player(alice).record.contract
    assert contract is not None
    advisory = await verifier.read(PHOTO, contract, 0, Decimal(9))
    assert advisory.model is None
    assert advisory.note == "vision model unavailable"

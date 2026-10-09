"""Drive a game to a state, as the routes would."""

from decimal import Decimal

from tests.game.conftest import Game
from youreapyousow.game.coach import CoachDraft, CoachTurn
from youreapyousow.game.models import EvidencePolicy, GoalType

DRAFTS = (
    CoachDraft(
        goal_type=GoalType.PUSH_UP_IMPROVEMENT,
        goal_statement="Increase max consecutive strict push-ups",
        baseline_value=Decimal(5),
        unit="reps",
        target_value=Decimal(20),
        milestone_targets=[Decimal(8), Decimal(11), Decimal(15), Decimal(20)],
        evidence_policy=EvidencePolicy.PHOTO_OR_CLIP,
    ),
    CoachDraft(
        goal_type=GoalType.RUN_CONSISTENCY,
        goal_statement="Run three times a week",
        baseline_value=Decimal(0),
        unit="runs per week",
        target_value=Decimal(3),
        milestone_targets=[Decimal(1), Decimal(2), Decimal(2), Decimal(3)],
        evidence_policy=EvidencePolicy.LOG,
    ),
    CoachDraft(
        goal_type=GoalType.STRENGTH_ROUTINE,
        goal_statement="Build a strength-training routine",
        baseline_value=Decimal(0),
        unit="sessions per week",
        target_value=Decimal(3),
        milestone_targets=[Decimal(1), Decimal(2), Decimal(3), Decimal(3)],
        evidence_policy=EvidencePolicy.PHOTO_OR_CLIP,
    ),
)


def ready_group(game: Game) -> list[str]:
    """Fill the group, propose and lock the handover's three contracts.

    Args:
        game: The game.

    Returns:
        The player ids, Alice (push-ups), Ben (runs), Chloe (strength).
    """
    ids = [game.service.join(n).id for n in ("Alice", "Ben", "Chloe")]
    for pid, draft in zip(ids, DRAFTS, strict=True):
        game.service.record_intake(pid, "my goal", CoachTurn("Here it is.", draft, "fake"))
        game.service.lock_contract(pid)
    return ids


def active_group(game: Game) -> list[str]:
    """Ready the group and have everyone accept.

    Args:
        game: The game.

    Returns:
        The player ids.
    """
    ids = ready_group(game)
    for pid in ids:
        game.service.accept(pid)
    return ids

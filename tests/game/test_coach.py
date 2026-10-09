"""The coach drafts, the rubric shapes, the player edits and locks."""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from tests.game.conftest import Game
from youreapyousow.config import Settings
from youreapyousow.featherless import Message, ModelError
from youreapyousow.game.coach import Coach, CoachDraft, ContractInvalidError, build_contract
from youreapyousow.game.llm import Link, build_links
from youreapyousow.game.models import (
    ChatTurn,
    ContractStatus,
    EvidencePolicy,
    GoalType,
    GroupStatus,
    IntakeState,
)
from youreapyousow.game.rubric import RUBRIC_V1
from youreapyousow.game.service import GameError
from youreapyousow.ledger.events import EventType

pytestmark = pytest.mark.anyio

DRAFT = {
    "goal_type": "push_up_improvement",
    "goal_statement": "Increase max consecutive strict push-ups",
    "baseline_value": 5,
    "unit": "reps",
    "target_value": 20,
    "milestone_targets": [8, 11, 15, 20],
    "evidence_policy": "photo_or_clip",
    "comparability": "Quadrupling from 5 in four weeks is as demanding as three runs a week.",
}


class FakeChat:
    """A chat client that answers from a script, or fails."""

    def __init__(self, model: str, replies: list[str | Exception]) -> None:
        """Script the answers.

        Args:
            model: Its model name.
            replies: Each call's answer, in order; an exception is raised.
        """
        self.model = model
        self.replies = replies
        self.seen: list[list[Message]] = []

    async def complete(self, messages: list[Message]) -> str:
        """Answer the next scripted reply.

        Args:
            messages: The conversation.

        Returns:
            The reply.

        Raises:
            Exception: The scripted failure.
        """
        self.seen.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def proposal(**draft: object) -> str:
    """A ready coach reply with a draft.

    Args:
        **draft: Fields overriding the default draft.

    Returns:
        The JSON text.
    """
    return json.dumps({"reply": "Here is your contract.", "ready": True, "draft": DRAFT | draft})


def fill(game: Game) -> list[str]:
    """Fill the group's three seats.

    Args:
        game: The game.

    Returns:
        The player ids.
    """
    return [game.service.join(n).id for n in ("Alice", "Ben", "Chloe")]


async def talk(game: Game, player_id: str, coach: Coach, message: str) -> None:
    """Run one intake turn as the route does.

    Args:
        game: The game.
        player_id: The player.
        coach: The coach.
        message: What the player says.
    """
    player = game.service.intake_player(player_id)
    turn = await coach.respond(player.transcript, message)
    game.service.record_intake(player_id, message, turn)


async def test_a_proposal_becomes_a_contract_on_the_rubric(game: Game) -> None:
    """Equal 100 points on 15/20/25/40 at days 7/14/21/28, with model and versions stored."""
    alice, *_ = fill(game)
    coach = Coach([Link("openai:gpt-5.6-terra", FakeChat("m", [proposal()]))], duration_days=28)
    await talk(game, alice, coach, "I can do 5 push-ups and want 20")
    player = game.service.player(alice).record
    contract = player.contract
    assert contract is not None
    assert player.intake == IntakeState.PROPOSED
    assert [m.max_points for m in contract.milestones] == [15, 20, 25, 40]
    assert [m.day for m in contract.milestones] == [7, 14, 21, 28]
    assert [m.target for m in contract.milestones] == [8, 11, 15, 20]
    assert contract.total_max_points == 100
    assert contract.model == "openai:gpt-5.6-terra"
    assert contract.prompt_version == "coach-v1"
    assert contract.rubric_version == "rubric-v1"
    proposed = game.ledger.events(types=[EventType.CONTRACT_PROPOSED])[-1]
    assert proposed.payload["model"] == "openai:gpt-5.6-terra"
    assert proposed.payload["input_summary"] == "I can do 5 push-ups and want 20"


async def test_a_question_keeps_the_chat_going(game: Game) -> None:
    """A reply that is not ready stores the exchange and no contract."""
    alice, *_ = fill(game)
    question = json.dumps({"reply": "How many can you do now?", "ready": False, "draft": None})
    coach = Coach([Link("a", FakeChat("m", [question]))], duration_days=28)
    await talk(game, alice, coach, "I want to do more push-ups")
    player = game.service.player(alice).record
    assert player.contract is None
    assert player.intake == IntakeState.CHATTING
    assert [t.role for t in player.transcript] == ["user", "assistant"]


async def test_a_failing_link_falls_to_the_next() -> None:
    """A timeout and a malformed reply pass the turn to the next model, which is named."""
    coach = Coach(
        [
            Link("openai:first", FakeChat("a", [ModelError("timed out")])),
            Link("openai:second", FakeChat("b", ["not json at all"])),
            Link("featherless:glm", FakeChat("c", [proposal()])),
        ],
        duration_days=28,
    )
    turn = await coach.respond((), "push-ups 5 to 20")
    assert turn.model == "featherless:glm"
    assert turn.draft is not None


async def test_no_model_drafts_a_template_from_the_players_words() -> None:
    """With every link down, the template reads the goal and the numbers said."""
    coach = Coach([Link("x", FakeChat("m", [ModelError("down")]))], duration_days=28)
    turn = await coach.respond((), "I run 1 time a week and want to get to 3")
    assert turn.model == "template"
    assert turn.draft is not None
    assert turn.draft.goal_type == GoalType.RUN_CONSISTENCY
    assert (turn.draft.baseline_value, turn.draft.target_value) == (Decimal(1), Decimal(3))
    assert turn.draft.evidence_policy == EvidencePolicy.LOG


async def test_the_last_turn_always_ends_in_a_draft() -> None:
    """At the third message a model still asking questions is overridden by the template."""
    question = json.dumps({"reply": "Tell me more?", "ready": False})
    coach = Coach([Link("a", FakeChat("m", [question]))], duration_days=28)

    history = (
        ChatTurn(role="user", content="strength"),
        ChatTurn(role="assistant", content="ok"),
        ChatTurn(role="user", content="twice a week"),
        ChatTurn(role="assistant", content="ok"),
    )
    turn = await coach.respond(history, "fine")
    assert turn.draft is not None
    assert turn.draft.goal_type == GoalType.STRENGTH_ROUTINE


def test_milestones_that_do_not_climb_are_replaced_by_an_even_climb() -> None:
    """The model's milestones are kept only if they climb from baseline to target."""
    draft = CoachDraft.model_validate(DRAFT | {"milestone_targets": [30, 2, 1, 7]})
    contract = build_contract(
        participant_id="p", draft=draft, rubric=RUBRIC_V1, duration_days=28, model="m"
    )
    assert [m.target for m in contract.milestones] == [9, 12, 16, 20]


def test_a_target_below_the_baseline_is_refused() -> None:
    """No contract rewards going backwards."""
    draft = CoachDraft.model_validate(DRAFT | {"target_value": 4})
    with pytest.raises(ContractInvalidError):
        build_contract(
            participant_id="p", draft=draft, rubric=RUBRIC_V1, duration_days=28, model="m"
        )


async def test_the_player_edits_then_locks_and_the_group_becomes_ready(game: Game) -> None:
    """Edits are re-validated with points unchanged; three locks make the group ready."""
    ids = fill(game)
    coach = Coach(
        [Link("a", FakeChat("m", [proposal(), proposal(), proposal()]))], duration_days=28
    )
    for pid in ids:
        await talk(game, pid, coach, "push-ups")
    edited = game.service.edit_contract(ids[0], {"target_value": 24, "milestone_targets": None})
    assert edited.target.value == 24
    assert [m.max_points for m in edited.milestones] == [15, 20, 25, 40]
    with pytest.raises(GameError) as refused:
        game.service.edit_contract(ids[0], {"target_value": 2})
    assert refused.value.code == "CONTRACT_INVALID"
    for pid in ids[:2]:
        game.service.lock_contract(pid)
    assert game.service.group().status == GroupStatus.INTAKE
    with pytest.raises(GameError) as locked:
        game.service.edit_contract(ids[0], {"target_value": 30})
    assert locked.value.code == "CONTRACT_LOCKED"
    contract = game.service.lock_contract(ids[2])
    assert contract.status == ContractStatus.LOCKED
    assert game.service.group().status == GroupStatus.READY_FOR_ACCEPTANCE
    assert game.ledger.events(types=[EventType.GROUP_READY])


def test_intake_waits_for_a_full_group(game: Game) -> None:
    """The coach opens once every seat is taken."""
    alice = game.service.join("Alice")
    with pytest.raises(GameError) as refused:
        game.service.intake_player(alice.id)
    assert refused.value.code == "WRONG_STATE"


def _settings(**values: object) -> Settings:
    return Settings.model_validate(values)


def test_the_chain_puts_openai_first_and_featherless_after() -> None:
    """With both keys: the two OpenAI models from the settings, then Featherless."""
    http = httpx.AsyncClient()
    settings = _settings(
        openai_api_key=SecretStr("o"), featherless_api_key=SecretStr("f"), model_provider=None
    )
    coach = [link.label for link in build_links(settings, http, "coach")]
    vision = [link.label for link in build_links(settings, http, "vision")]
    assert coach == [
        "openai:gpt-5.6-terra",
        "openai:gpt-5.6-sol",
        "featherless:zai-org/GLM-5.3-Flash",
    ]
    assert vision == [
        "openai:gpt-5.6-terra",
        "openai:gpt-6-luna",
        "featherless:Qwen/Qwen3-VL-8B-Instruct",
    ]


def test_featherless_alone_and_none_are_chosen_by_setting() -> None:
    """``MODEL_PROVIDER=featherless`` skips OpenAI; no key at all is an empty chain."""
    http = httpx.AsyncClient()
    featherless = _settings(
        openai_api_key=SecretStr("o"),
        featherless_api_key=SecretStr("f"),
        model_provider="featherless",
    )
    assert [link.label for link in build_links(featherless, http, "coach")] == [
        "featherless:zai-org/GLM-5.3-Flash"
    ]
    assert build_links(_settings(model_provider=None), http, "coach") == []

"""The AI coach: a short intake chat that ends in a draft goal contract.

The model only proposes. Its draft is validated against the schema and turned into a
``GoalContract`` by deterministic code: the rubric sets every milestone's day and points,
milestone targets that do not climb from the baseline to the target are replaced by an
even climb, and an implausible goal is refused. When no model answers, a template contract
is drafted from the player's own words, for the player to edit.
"""

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field

from youreapyousow.featherless import Message
from youreapyousow.game.llm import ChainError, Link, ask, json_object
from youreapyousow.game.models import (
    ChatTurn,
    EvidencePolicy,
    GoalContract,
    GoalType,
    Measure,
    Milestone,
)
from youreapyousow.game.rubric import Rubric, milestone_days

PROMPT_VERSION = "coach-v1"
MAX_USER_TURNS = 3
MAX_VALUE = Decimal(1000)

SYSTEM_PROMPT = """You are the coach of a group accountability challenge. Players compete on
their own goals for one prize; everyone gets the same 100 points across four milestones,
so goals must be comparably ambitious, measurable and safe. You only propose; code checks
everything you say and the player edits and locks the contract.

Allowed goal types, with their usual units:
- push_up_improvement: max consecutive strict push-ups, unit "reps"
- run_consistency: runs per week, unit "runs per week"
- strength_routine: strength sessions per week, unit "sessions per week"

In at most {max_turns} short exchanges, learn the objective, the baseline (where they are
now), time available and any limitation they volunteer, and the target for a
{duration} day challenge. Ask one short question at a time. This is the player's message
{turn} of at most {max_turns}; at message {max_turns} you must propose.

Answer with exactly one JSON object and nothing else:
{{"reply": "<one or two friendly sentences to the player>",
 "ready": <true when you propose a contract, else false>,
 "draft": null or {{"goal_type": "<one of the three>",
   "goal_statement": "<the goal in one line>",
   "baseline_value": <number>, "unit": "<unit>", "target_value": <number>,
   "milestone_targets": [<four numbers climbing from the baseline to the target>],
   "evidence_policy": "photo_or_clip" for push-ups or strength, "log" for runs,
   "comparability": "<one sentence on why this target is as demanding as a typical
     player's, given the baseline and the time>"}}}}
Targets must be realistic for {duration} days: ambitious, never unsafe."""


class CoachDraft(BaseModel):
    """The model's draft contract, before the rubric shapes it.

    Attributes:
        goal_type: One of the rubric's goal types.
        goal_statement: The goal in one line.
        baseline_value: Where the player starts.
        unit: The unit of the baseline and the target.
        target_value: Where the player commits to finish.
        milestone_targets: Four targets climbing to the target.
        evidence_policy: Photo or clip, or a log.
        comparability: One sentence on why the target is comparable; advisory.
    """

    model_config = ConfigDict(extra="ignore")

    goal_type: GoalType
    goal_statement: str = Field(min_length=1, max_length=200)
    baseline_value: Decimal
    unit: str = Field(min_length=1, max_length=40)
    target_value: Decimal
    milestone_targets: list[Decimal] = []
    evidence_policy: EvidencePolicy
    comparability: str = Field(default="", max_length=400)


class CoachReply(BaseModel):
    """One coach answer.

    Attributes:
        reply: What the coach says.
        ready: Whether a draft is proposed.
        draft: The draft, when ready.
    """

    model_config = ConfigDict(extra="ignore")

    reply: str = Field(min_length=1, max_length=1000)
    ready: bool = False
    draft: CoachDraft | None = None


@dataclass(frozen=True)
class CoachTurn:
    """What the coach said this turn, and who said it.

    Attributes:
        reply: The coach's words.
        draft: The draft contract, when proposed.
        model: The model that answered, or ``template`` when none did.
        note: Why the template was used, if it was.
    """

    reply: str
    draft: CoachDraft | None
    model: str
    note: str | None = None


class ContractInvalidError(ValueError):
    """Raised when a draft or an edit cannot be a fair contract."""


def _parse(text: str) -> CoachReply:
    reply = CoachReply.model_validate(json.loads(json_object(text)))
    if reply.ready and reply.draft is None:
        raise ValueError("ready without a draft")
    return reply


_KEYWORDS: tuple[tuple[GoalType, tuple[str, ...], str, EvidencePolicy, str], ...] = (
    (
        GoalType.RUN_CONSISTENCY,
        ("run", "jog"),
        "runs per week",
        EvidencePolicy.LOG,
        "Run consistently each week",
    ),
    (
        GoalType.STRENGTH_ROUTINE,
        ("strength", "gym", "weights", "lift", "routine"),
        "sessions per week",
        EvidencePolicy.PHOTO_OR_CLIP,
        "Build a strength-training routine",
    ),
    (
        GoalType.PUSH_UP_IMPROVEMENT,
        ("push",),
        "reps",
        EvidencePolicy.PHOTO_OR_CLIP,
        "Increase max consecutive strict push-ups",
    ),
)


def template_draft(words: str) -> CoachDraft:
    """Draft a contract from the player's own words when no model answers.

    Args:
        words: Everything the player said.

    Returns:
        A draft on the goal type the words name (push-ups when none), with the first two
        numbers said as baseline and target when they climb.
    """
    lowered = words.lower()
    goal_type, unit, policy, statement = (
        GoalType.PUSH_UP_IMPROVEMENT,
        "reps",
        EvidencePolicy.PHOTO_OR_CLIP,
        "Increase max consecutive strict push-ups",
    )
    for kind, keywords, kind_unit, kind_policy, kind_statement in _KEYWORDS:
        if any(k in lowered for k in keywords):
            goal_type, unit, policy, statement = kind, kind_unit, kind_policy, kind_statement
            break
    numbers = [Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", words)][:2]
    defaults = {
        GoalType.PUSH_UP_IMPROVEMENT: (Decimal(5), Decimal(20)),
        GoalType.RUN_CONSISTENCY: (Decimal(0), Decimal(3)),
        GoalType.STRENGTH_ROUTINE: (Decimal(0), Decimal(3)),
    }
    baseline, target = defaults[goal_type]
    if len(numbers) == 2 and numbers[1] > numbers[0]:
        baseline, target = numbers
    return CoachDraft(
        goal_type=goal_type,
        goal_statement=statement,
        baseline_value=baseline,
        unit=unit,
        target_value=target,
        evidence_policy=policy,
        comparability="Drafted from your words without the coach; check and edit it.",
    )


def _climb(baseline: Decimal, target: Decimal, steps: int) -> list[Decimal]:
    span = target - baseline
    whole = baseline == baseline.to_integral_value() and target == target.to_integral_value()
    out: list[Decimal] = []
    for i in range(1, steps + 1):
        value = baseline + span * Decimal(i) / Decimal(steps)
        out.append(value.to_integral_value() if whole else value.quantize(Decimal("0.1")))
    return out


def milestone_targets(
    baseline: Decimal, target: Decimal, proposed: list[Decimal], steps: int
) -> list[Decimal]:
    """Keep the proposed milestone targets if they climb to the target, else climb evenly.

    Args:
        baseline: Where the player starts.
        target: Where the player finishes.
        proposed: The model's or the player's targets.
        steps: How many milestones.

    Returns:
        ``steps`` targets, non-decreasing, above the baseline, the last equal to the target.
    """
    ok = (
        len(proposed) == steps
        and proposed[-1] == target
        and proposed[0] > baseline
        and all(a <= b for a, b in pairwise(proposed))
    )
    return list(proposed) if ok else _climb(baseline, target, steps)


def build_contract(
    *,
    participant_id: str,
    draft: CoachDraft,
    rubric: Rubric,
    duration_days: int,
    model: str,
) -> GoalContract:
    """Shape a draft into a contract under the rubric.

    Args:
        participant_id: The player.
        draft: The draft.
        rubric: The rubric in force.
        duration_days: The challenge's length.
        model: The model that drafted it.

    Returns:
        The proposed contract, with the rubric's days and points.

    Raises:
        ContractInvalidError: If the target does not improve on the baseline, or a value is
            negative or out of range.
    """
    baseline, target = draft.baseline_value, draft.target_value
    if baseline < 0 or target > MAX_VALUE:
        raise ContractInvalidError(f"Values must be between 0 and {MAX_VALUE}.")
    if target <= baseline:
        raise ContractInvalidError("The target must be above the baseline.")
    days = milestone_days(duration_days)
    targets = milestone_targets(
        baseline, target, list(draft.milestone_targets), len(rubric.milestone_points)
    )
    milestones = tuple(
        Milestone(index=i, day=day, target=t, max_points=points)
        for i, (day, t, points) in enumerate(
            zip(days, targets, rubric.milestone_points, strict=True)
        )
    )
    return GoalContract(
        participant_id=participant_id,
        goal_type=draft.goal_type,
        goal_statement=draft.goal_statement,
        baseline=Measure(value=baseline, unit=draft.unit),
        target=Measure(value=target, unit=draft.unit),
        duration_days=duration_days,
        milestones=milestones,
        evidence_policy=draft.evidence_policy,
        total_max_points=rubric.total_points,
        rubric_version=rubric.version,
        comparability=draft.comparability,
        model=model,
        prompt_version=PROMPT_VERSION,
    )


def parse_number(value: object) -> Decimal:
    """Read a number from an edit.

    Args:
        value: A number or numeric string.

    Returns:
        The decimal.

    Raises:
        ContractInvalidError: If it is not a number.
    """
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise ContractInvalidError(f"{value!r} is not a number.") from error


class Coach:
    """Runs one intake turn through the model chain."""

    def __init__(self, links: list[Link], *, duration_days: int) -> None:
        """Wire the coach.

        Args:
            links: The model chain, in order; empty for the template only.
            duration_days: The challenge's length, for the prompt.
        """
        self.links = links
        self.duration_days = duration_days

    async def respond(self, transcript: tuple[ChatTurn, ...], message: str) -> CoachTurn:
        """Answer the player's message.

        Args:
            transcript: The chat so far.
            message: The player's new message.

        Returns:
            The coach's turn: a reply, and a draft when it proposes one.
        """
        turn = sum(1 for t in transcript if t.role == "user") + 1
        system = SYSTEM_PROMPT.format(
            max_turns=MAX_USER_TURNS, duration=self.duration_days, turn=min(turn, MAX_USER_TURNS)
        )
        messages: list[Message] = [{"role": "system", "content": system}]
        for t in transcript:
            messages.append({"role": t.role, "content": t.content})
        messages.append({"role": "user", "content": message})
        words = " ".join([t.content for t in transcript if t.role == "user"] + [message])
        try:
            reply, model = await ask(self.links, messages, _parse)
        except ChainError as error:
            draft = template_draft(words)
            return CoachTurn(
                reply="The coach is not reachable right now, so here is a starting contract "
                "drawn from your words. Check the numbers, edit them, then lock it.",
                draft=draft,
                model="template",
                note=str(error),
            )
        if reply.ready and reply.draft is not None:
            return CoachTurn(reply.reply, reply.draft, model)
        if turn >= MAX_USER_TURNS:
            return CoachTurn(
                reply.reply + " Here is a starting contract from what you said; edit it "
                "and lock it.",
                template_draft(words),
                model,
                note="the model did not propose by the last turn",
            )
        return CoachTurn(reply.reply, None, model)

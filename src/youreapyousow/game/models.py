"""The challenge's records: the group, its seats, goal contracts, score events and result.

Every record is a frozen pydantic model stored through ``store.Records``; a change is a new
version of the record and an event on the ledger, in one transaction.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

CENT = Decimal("0.01")


class GroupStatus(StrEnum):
    """Where a group is in its life: the happy path, then the exceptional states."""

    OPEN_FOR_JOINING = "OPEN_FOR_JOINING"
    INTAKE = "INTAKE"
    READY_FOR_ACCEPTANCE = "READY_FOR_ACCEPTANCE"
    ACTIVE = "ACTIVE"
    RESULTS_PENDING = "RESULTS_PENDING"
    DISPUTE_WINDOW = "DISPUTE_WINDOW"
    FINALIZED = "FINALIZED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"
    REFUNDING = "REFUNDING"


class EntryState(StrEnum):
    """A seat's entry against the pool."""

    RESERVED = "RESERVED"
    REFUNDED = "REFUNDED"


class IntakeState(StrEnum):
    """How far a player is with the coach."""

    NOT_STARTED = "NOT_STARTED"
    CHATTING = "CHATTING"
    PROPOSED = "PROPOSED"
    LOCKED = "LOCKED"


class ContractStatus(StrEnum):
    """A goal contract's status."""

    PROPOSED = "PROPOSED"
    LOCKED = "LOCKED"
    ACCEPTED = "ACCEPTED"


class GoalType(StrEnum):
    """The small set of goal categories tonight's rubric covers."""

    PUSH_UP_IMPROVEMENT = "push_up_improvement"
    RUN_CONSISTENCY = "run_consistency"
    STRENGTH_ROUTINE = "strength_routine"


class EvidencePolicy(StrEnum):
    """What a check-in must carry for its milestone to score."""

    PHOTO_OR_CLIP = "photo_or_clip"
    LOG = "log"


class ScoreState(StrEnum):
    """A check-in's adjudication state."""

    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    DISPUTED = "DISPUTED"


class EvidenceKind(StrEnum):
    """What a check-in carried."""

    PHOTO = "photo"
    CLIP = "clip"
    LOG = "log"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class GroupTerms(_Frozen):
    """The terms a group opens with, published before anyone joins.

    Attributes:
        title: The drop's name.
        entry_amount: One entry, in test USDC recorded as USD.
        currency: Always ``USD`` (test USDC, 1:1).
        min_players: The quorum to start.
        max_players: The capacity.
        enrolment_window_s: Real seconds from publication to the fixed start; joining,
            the coach and acceptance happen in it.
        duration_days: The challenge's length in challenge days.
        seconds_per_day: Demo time: real seconds per challenge day.
        dispute_window_s: Real seconds the dispute window stays open.
        buffer_rate: The share of the gross pool held back from the prize ceiling.
    """

    title: str
    entry_amount: Decimal = Field(gt=0)
    currency: str = "USD"
    min_players: int = Field(default=3, ge=1)
    max_players: int = Field(default=3, ge=1)
    enrolment_window_s: float = Field(default=3600.0, gt=0)
    duration_days: int = Field(default=28, ge=1)
    seconds_per_day: float = Field(default=60 / 7, gt=0)
    dispute_window_s: float = Field(default=20.0, ge=0)
    buffer_rate: Decimal = Field(default=Decimal("0.10"), ge=0, lt=1)


class Milestone(_Frozen):
    """One milestone of a contract.

    Attributes:
        index: Its position, from 0.
        day: The challenge day it is due.
        target: The value that meets it.
        max_points: What it scores when met; the rubric's, not the player's.
    """

    index: int = Field(ge=0)
    day: int = Field(ge=1)
    target: Decimal
    max_points: int = Field(ge=0)


class Measure(_Frozen):
    """A value with its unit.

    Attributes:
        value: The number.
        unit: Its unit, such as ``reps`` or ``runs per week``.
        verified: Whether a check confirmed it (baselines are self-reported tonight).
    """

    value: Decimal
    unit: str
    verified: bool = False


class GoalContract(_Frozen):
    """A player's goal contract: the handover's ``GoalContract`` schema.

    Attributes:
        participant_id: The player.
        goal_type: One of the rubric's goal categories.
        goal_statement: The goal in one line.
        baseline: Where the player starts.
        target: Where the player commits to finish.
        duration_days: The challenge's length.
        milestones: Four milestones on the rubric's common format.
        evidence_policy: What a check-in must carry.
        total_max_points: Always the rubric's total.
        rubric_version: The rubric the contract was written under.
        comparability: The coach's one sentence on why the target is comparable; advisory.
        model: The model that proposed it.
        prompt_version: The coach prompt's version.
        status: Proposed, locked or accepted.
    """

    participant_id: str
    goal_type: GoalType
    goal_statement: str = Field(min_length=1, max_length=200)
    baseline: Measure
    target: Measure
    duration_days: int
    milestones: tuple[Milestone, ...]
    evidence_policy: EvidencePolicy
    total_max_points: int
    rubric_version: str
    comparability: str = ""
    model: str = ""
    prompt_version: str = ""
    status: ContractStatus = ContractStatus.PROPOSED


class ChatTurn(_Frozen):
    """One line of the coach chat.

    Attributes:
        role: ``user`` or ``assistant``.
        content: The text.
    """

    role: str
    content: str


class Player(_Frozen):
    """A seat in the group.

    Attributes:
        id: The player id.
        group_id: The group.
        name: The display name.
        seat: The seat number, from 1.
        joined_at: When the seat was reserved.
        entry: The entry's state.
        intake: How far the coach chat is.
        transcript: The coach chat.
        contract: The proposed or locked contract.
        accepted_at: When the player accepted the group agreement.
        last_checkin_at: The last check-in's time, for the rate limit.
    """

    id: str
    group_id: str
    name: str = Field(min_length=1, max_length=40)
    seat: int
    joined_at: datetime
    entry: EntryState = EntryState.RESERVED
    intake: IntakeState = IntakeState.NOT_STARTED
    transcript: tuple[ChatTurn, ...] = ()
    contract: GoalContract | None = None
    accepted_at: datetime | None = None
    last_checkin_at: datetime | None = None


class Standing(_Frozen):
    """One line of the leaderboard.

    Attributes:
        player_id: The player.
        name: The display name.
        score: Verified points.
        verified_milestones: How many milestones are verified.
        last_verified_at: The last verified check-in's time.
        rank: 1 is first; the tie-break decides equal scores.
    """

    player_id: str
    name: str
    score: int
    verified_milestones: int
    last_verified_at: datetime | None
    rank: int


class PrizePurchase(_Frozen):
    """The agent's prize purchase, as the screen shows it.

    Attributes:
        status: ``BUYING``, ``AWAITING_APPROVAL``, ``PURCHASED`` or ``FAILED``.
        step: The step the agent is on.
        backend: ``sandbox``, ``kwal`` or ``mock``.
        quote_final_amount: The landed quote the gate decided on.
        ceiling: The gate's ceiling: the pool less its buffer.
        gate: The gate's decision, disposition, rule and reason.
        order_id: The merchant's order id.
        checkout_id: Reap's checkout id.
        final_amount: What was charged.
        intent_id: The gate's intent.
        error: Why it failed, if it did.
        note: Why it ran where it ran, when that is not the configured backend.
        approval_url: Reap's hosted page where the card holder approves the charge.
        approval_expires_at: When that page stops accepting the approval.
        approved_at: When the agent first read the checkout completed after approval.
    """

    status: str
    step: str
    backend: str
    quote_final_amount: Decimal | None = None
    ceiling: Decimal | None = None
    gate: dict[str, str] | None = None
    order_id: str | None = None
    checkout_id: str | None = None
    final_amount: Decimal | None = None
    intent_id: str | None = None
    error: str | None = None
    note: str | None = None
    approval_url: str | None = None
    approval_expires_at: datetime | None = None
    approved_at: datetime | None = None


class Result(_Frozen):
    """The frozen standings and the winner.

    Attributes:
        winner_id: The winner.
        standings: The frozen leaderboard.
        tie_break_applied: Whether equal scores were split by the tie-break.
        purchase: The prize purchase, once begun.
    """

    winner_id: str | None
    standings: tuple[Standing, ...]
    tie_break_applied: bool = False
    purchase: PrizePurchase | None = None


class Group(_Frozen):
    """The group: one item, one entry amount, its seats and its clock.

    Attributes:
        id: The group id.
        terms: The published terms.
        status: Where it is.
        opened_at: When it opened.
        enrolment_deadline: When an unready group cancels: its fixed start.
        starts_at: The fixed start, set at publication.
        started_at: When the challenge actually started (its fixed start).
        ends_at: When submissions close, fixed at publication.
        dispute_window_ends_at: When the dispute window closes.
        rubric_version: The rubric in force.
        rubric_locked_at: When the rubric was locked.
        cancelled_reason: Why it was cancelled.
        result: The frozen result.
    """

    id: str
    terms: GroupTerms
    status: GroupStatus
    opened_at: datetime
    enrolment_deadline: datetime
    starts_at: datetime | None = None
    started_at: datetime | None = None
    ends_at: datetime | None = None
    dispute_window_ends_at: datetime | None = None
    rubric_version: str
    rubric_locked_at: datetime | None = None
    cancelled_reason: str | None = None
    result: Result | None = None


class Advisory(_Frozen):
    """The vision model's reading of a photo: advisory, never the score.

    Attributes:
        model: The model that read it.
        shows: Whether it says the photo shows the activity.
        count: The count it read, if any.
        note: Its one-line note, or why it was not read.
        agrees: Whether its count meets the claimed value.
    """

    model: str | None
    shows: bool | None
    count: Decimal | None
    note: str
    agrees: bool | None


class ScoreEvent(_Frozen):
    """An append-only score event; the leaderboard is folded from these.

    Attributes:
        event_id: Its id.
        seq: Its order in the group.
        participant_id: The player.
        group_id: The group.
        milestone: The milestone index.
        evidence_id: The stored evidence, if any.
        evidence_kind: Photo, clip or log.
        evidence_sha256: The evidence's hash, for duplicate rejection.
        claimed_value: The value the player claimed.
        rubric_version: The rubric that decided it.
        at: When it was recorded.
        delta: The points it adds (negative for a dispute that withdraws them).
        state: Its adjudication state.
        reason: The rubric's reason, one line.
        supersedes: The event a dispute or a review acts on.
        advisory: The vision model's reading.
    """

    event_id: str
    seq: int
    participant_id: str
    group_id: str
    milestone: int
    evidence_id: str | None
    evidence_kind: EvidenceKind
    evidence_sha256: str | None
    claimed_value: Decimal
    rubric_version: str
    at: datetime
    delta: int
    state: ScoreState
    reason: str
    supersedes: str | None = None
    advisory: Advisory | None = None

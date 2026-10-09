"""The common rubric: equal points, a common milestone format, and the published tie-break.

The rubric is versioned and locked when the challenge starts; the scoring and the
standings are pure functions of the locked contracts and the score events, so the
leaderboard can be recomputed from the events at any time and always gives the same answer.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from youreapyousow.game.models import Player, ScoreEvent, ScoreState, Standing

RUBRIC_VERSION = "rubric-v1"
MILESTONE_POINTS = (15, 20, 25, 40)
TOTAL_POINTS = sum(MILESTONE_POINTS)
MILESTONE_DAYS_FRACTION = (0.25, 0.5, 0.75, 1.0)
CHECKIN_MIN_INTERVAL_S = 10.0
TIE_BREAK = "Higher verified milestone score, then the earlier last verified check-in."


class Rubric(BaseModel):
    """The rubric every contract in a group is scored under.

    Attributes:
        version: Its version, stored on every contract and score event.
        total_points: Every player's ceiling.
        milestone_points: The points for each of the four milestones.
        rules: The published rules, in plain words.
        tie_break: The published tie-break.
    """

    model_config = ConfigDict(frozen=True)

    version: str
    total_points: int
    milestone_points: tuple[int, ...]
    rules: tuple[str, ...]
    tie_break: str


RUBRIC_V1 = Rubric(
    version=RUBRIC_VERSION,
    total_points=TOTAL_POINTS,
    milestone_points=MILESTONE_POINTS,
    rules=(
        "Everyone has the same 100 points across four milestones: 15, 20, 25 and 40.",
        "A milestone scores its full points once, when a check-in in its window shows "
        "the target met with the evidence the contract names; there is no partial credit.",
        "A milestone's window opens on the previous milestone's day and closes when the "
        "challenge ends; points are capped per period: one milestone, once.",
        "A photo is read by a vision model; its reading is advisory and shown beside the "
        "check-in. The rubric decides the points.",
        "The same file twice is rejected; one check-in per player every 10 seconds.",
        "A disputed check-in's points are withheld until a reviewer reinstates them.",
        f"Tie-break: {TIE_BREAK}",
    ),
    tie_break=TIE_BREAK,
)


def milestone_days(duration_days: int) -> tuple[int, ...]:
    """Return the four milestone days for a challenge of this length.

    Args:
        duration_days: The challenge's length in days.

    Returns:
        The days, the last one the final day.
    """
    return tuple(max(1, round(duration_days * f)) for f in MILESTONE_DAYS_FRACTION)


def effective_states(events: list[ScoreEvent]) -> dict[str, ScoreState]:
    """Fold the events into each check-in's current state.

    A check-in is an event that supersedes nothing; a dispute or a review is a later event
    that supersedes it, and the last one decides its state.

    Args:
        events: The score events, in order.

    Returns:
        Each check-in's state, by its event id.
    """
    states: dict[str, ScoreState] = {}
    for event in sorted(events, key=lambda e: e.seq):
        key = event.supersedes or event.event_id
        if event.supersedes is None or key in states:
            states[key] = event.state
    return states


def standings(players: list[Player], events: list[ScoreEvent]) -> list[Standing]:
    """Compute the leaderboard from the score events alone.

    A check-in counts its points while its state is ``VERIFIED``. Ranked by the published
    tie-break: higher verified score, then the earlier last verified check-in, then seat.

    Args:
        players: The players.
        events: The score events.

    Returns:
        The standings, first place first.
    """
    states = effective_states(events)
    score: dict[str, int] = {p.id: 0 for p in players}
    milestones: dict[str, int] = {p.id: 0 for p in players}
    last: dict[str, datetime | None] = {p.id: None for p in players}
    for event in events:
        if event.supersedes is not None or states.get(event.event_id) != ScoreState.VERIFIED:
            continue
        if event.participant_id not in score:
            continue
        score[event.participant_id] += event.delta
        milestones[event.participant_id] += 1
        previous = last[event.participant_id]
        last[event.participant_id] = event.at if previous is None else max(previous, event.at)
    far = datetime.max.replace(tzinfo=UTC)
    ordered = sorted(players, key=lambda p: (-score[p.id], last[p.id] or far, p.seat))
    return [
        Standing(
            player_id=p.id,
            name=p.name,
            score=score[p.id],
            verified_milestones=milestones[p.id],
            last_verified_at=last[p.id],
            rank=i + 1,
        )
        for i, p in enumerate(ordered)
    ]

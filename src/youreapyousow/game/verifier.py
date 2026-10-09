"""The photo reader: asks a vision model only "does this show X; count Y". Advisory.

Its answer is stored beside the check-in and shown on screen; it never decides the points.
A clip is not sent to the model.
"""

import base64
import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from youreapyousow.featherless import Message
from youreapyousow.game.llm import ChainError, Link, ask, json_object
from youreapyousow.game.models import Advisory, GoalContract, GoalType
from youreapyousow.game.service import EvidenceIn

ACTIVITY = {
    GoalType.PUSH_UP_IMPROVEMENT: ("a person doing push-ups", "push-up repetitions"),
    GoalType.RUN_CONSISTENCY: ("a run: a runner, a route or a run tracker", "runs"),
    GoalType.STRENGTH_ROUTINE: ("a strength-training session", "sessions or sets"),
}

PROMPT = """Look at the image. Answer only these two questions, about what is visible:
1. Does it show {activity}?
2. Count the {unit} you can see or read in it (null if none can be counted).
Answer with exactly one JSON object and nothing else:
{{"shows": true or false, "count": <number or null>, "note": "<one short sentence>"}}"""


class _Reading(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shows: bool
    count: Decimal | None = None
    note: str = ""


def _parse(text: str) -> _Reading:
    return _Reading.model_validate(json.loads(json_object(text)))


class Verifier:
    """Reads a photo through the vision chain."""

    def __init__(self, links: list[Link]) -> None:
        """Wire the reader.

        Args:
            links: The vision model chain, in order.
        """
        self.links = links

    async def read(
        self, evidence: EvidenceIn, contract: GoalContract, milestone: int, claimed: Decimal
    ) -> Advisory:
        """Read a photo for the milestone's activity and count.

        Args:
            evidence: The photo.
            contract: The player's contract.
            milestone: The milestone index.
            claimed: The value the player claims.

        Returns:
            The advisory: what the model saw, and whether its count meets the claim.
        """
        del milestone
        if not evidence.content_type.startswith("image/"):
            return Advisory(
                model=None,
                shows=None,
                count=None,
                note="A clip is not read by the model; the rubric decides.",
                agrees=None,
            )
        activity, unit = ACTIVITY[contract.goal_type]
        data = base64.b64encode(evidence.content).decode()
        messages: list[Message] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT.format(activity=activity, unit=unit)},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{evidence.content_type};base64,{data}"},
                    },
                ],
            }
        ]
        try:
            reading, model = await ask(self.links, messages, _parse)
        except ChainError:
            return Advisory(
                model=None, shows=None, count=None, note="vision model unavailable", agrees=None
            )
        agrees = reading.shows and (reading.count is None or reading.count >= claimed)
        return Advisory(
            model=model,
            shows=reading.shows,
            count=reading.count,
            note=reading.note[:300],
            agrees=agrees,
        )

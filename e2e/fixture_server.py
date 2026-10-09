"""A stand-in for the room's API, in the contract's shapes, for testing the web client alone.

It serves ``web/`` at ``/`` and a stateful, in-memory version of every ``/api`` route on a
fast clock. It is a test fixture: no money moves, the coach is scripted and the purchase is
simulated. ``--seed`` jumps straight to a group state so each screen can be checked on its own.

Run: ``uv run python e2e/fixture_server.py --port 8765 --seed active``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import HTTP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MILESTONE_POINTS = [15, 20, 25, 40]
DURATION_DAYS = 28
RUBRIC_VERSION = "rubric-v1"
SEEDS = ("open", "intake", "agreement", "active", "dispute", "fulfilled", "cancelled")

GOALS: dict[str, dict[str, Any]] = {
    "push_up_improvement": {
        "goal_statement": "Increase max consecutive strict push-ups",
        "baseline": 5,
        "target": 20,
        "unit": "reps",
        "milestones": [8, 11, 15, 20],
        "evidence_policy": "photo_or_clip",
        "comparability": "A fourfold rise in strict reps over four weeks is a stretch for a "
        "beginner but steady weekly gains make it reachable, like the other two goals.",
    },
    "run_consistency": {
        "goal_statement": "Run three times a week, every week",
        "baseline": 0,
        "target": 3,
        "unit": "runs a week",
        "milestones": [1, 2, 3, 3],
        "evidence_policy": "photo_or_clip",
        "comparability": "Going from no running to three runs a week asks for a new habit "
        "held for four weeks, a similar effort to the strength and push-up goals.",
    },
    "strength_routine": {
        "goal_statement": "Complete a full-body strength session, building to four a week",
        "baseline": 0,
        "target": 4,
        "unit": "sessions a week",
        "milestones": [1, 2, 3, 4],
        "evidence_policy": "log",
        "comparability": "Building to four sessions a week from none is a sustained routine "
        "change, comparable in effort to the push-up and running targets.",
    },
}


def iso(t: datetime | None) -> str | None:
    """Formats a time as the contract's ISO 8601 UTC string.

    Args:
        t: The time, or None.

    Returns:
        The string, or None when there is no time.
    """
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if t else None


def utcnow() -> datetime:
    """Returns the current UTC time.

    Returns:
        The current time, timezone aware.
    """
    return datetime.now(UTC)


class Refusal(Exception):  # noqa: N818 - named for what it is, a refusal the client shows
    """A 4xx answer in the contract's error shape."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        """Builds the refusal.

        Args:
            code: The machine-readable code.
            message: The one line a person can read.
            status: The HTTP status.
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class Player:
    """One seat in the group."""

    player_id: str
    name: str
    seat: int
    entry: str = "RESERVED"
    intake: str = "NOT_STARTED"
    accepted: bool = False
    contract: dict[str, Any] | None = None
    turns: int = 0


@dataclass
class Room:
    """The whole in-memory group, its ledger and its score events."""

    seconds_per_day: float
    dispute_seconds: float
    status: str = "OPEN_FOR_JOINING"
    players: list[Player] = field(default_factory=list[Player])
    ledger: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    evidence: dict[str, tuple[str, bytes]] = field(default_factory=dict[str, tuple[str, bytes]])
    hashes: set[str] = field(default_factory=set[str])
    started_at: datetime | None = None
    ends_at: datetime | None = None
    dispute_ends_at: datetime | None = None
    rubric_locked_at: datetime | None = None
    result: dict[str, Any] | None = None
    enrolment_deadline: datetime = field(default_factory=lambda: utcnow() + timedelta(minutes=30))
    lock: threading.RLock = field(default_factory=threading.RLock)

    def append(self, kind: str, subject: str, summary: str) -> None:
        """Appends an event to the hash-chained ledger.

        Args:
            kind: The ledger type.
            subject: The subject id.
            summary: The ready sentence for the ledger tail.
        """
        prev = self.ledger[-1]["hash"] if self.ledger else ""
        seq = len(self.ledger) + 1
        digest = hashlib.sha256(f"{prev}|{seq}|{kind}|{summary}".encode()).hexdigest()
        self.ledger.append(
            {
                "seq": seq,
                "event_id": f"evt_{secrets.token_hex(4)}",
                "type": kind,
                "at": iso(utcnow()),
                "subject_id": subject,
                "summary": summary,
                "hash": digest,
            }
        )

    def player(self, player_id: str) -> Player:
        """Finds a seat by its player id.

        Args:
            player_id: The id from the join.

        Returns:
            The player.

        Raises:
            Refusal: When there is no such player.
        """
        for p in self.players:
            if p.player_id == player_id:
                return p
        raise Refusal("UNKNOWN_PLAYER", "That seat is not in this group.", 404)

    def need(self, *states: str) -> None:
        """Refuses unless the group is in one of the given states.

        Args:
            *states: The states the action needs.

        Raises:
            Refusal: When the group is elsewhere.
        """
        if self.status not in states:
            raise Refusal("WRONG_STATE", f"The group is {self.status.lower()}, not ready for that.")

    def tick(self) -> None:
        """Moves the group on time, as the contract describes."""
        now = utcnow()
        if self.status == "ACTIVE" and self.ends_at and now >= self.ends_at:
            self.status = "DISPUTE_WINDOW"
            self.dispute_ends_at = now + timedelta(seconds=self.dispute_seconds)
            self.append("standings.frozen", "grp_main", "Standings frozen at the deadline.")

    def score(self, p: Player) -> tuple[int, int, str | None]:
        """Sums a player's verified score events.

        Args:
            p: The player.

        Returns:
            The score, the verified milestone count and the last verified time.
        """
        mine = [
            e
            for e in self.events
            if e["participant_id"] == p.player_id and e["state"] == "VERIFIED"
        ]
        last = max((e["at"] for e in mine), default=None)
        return sum(e["delta"] for e in mine), len(mine), last

    def leaderboard(self) -> list[dict[str, Any]]:
        """Computes the standings from the score events, with the published tie-break.

        Returns:
            The rows, ranked.
        """
        rows: list[dict[str, Any]] = []
        for p in self.players:
            total, count, last = self.score(p)
            rows.append(
                {
                    "player_id": p.player_id,
                    "name": p.name,
                    "score": total,
                    "verified_milestones": count,
                    "last_verified_at": last,
                }
            )
        rows.sort(key=lambda r: (-r["score"], r["last_verified_at"] or "9999"))
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        return rows

    def state(self) -> dict[str, Any]:
        """Builds the contract's state document.

        Returns:
            The state.
        """
        self.tick()
        now = utcnow()
        entry = 30.0
        gross = entry * len([p for p in self.players if p.entry == "RESERVED"])
        quote = 71.20
        board = self.leaderboard()
        ranks = {r["player_id"]: r for r in board}
        day = None
        if self.started_at:
            day = round((now - self.started_at).total_seconds() / self.seconds_per_day, 1)
        return {
            "group": {
                "id": "grp_main",
                "title": "Earn your Keychron B40",
                "status": self.status,
                "min_players": 3,
                "max_players": 3,
                "entry_amount": "30.00",
                "currency": "USD",
                "enrolment_deadline": iso(self.enrolment_deadline),
                "duration_days": DURATION_DAYS,
                "started_at": iso(self.started_at),
                "ends_at": iso(self.ends_at),
                "dispute_window_ends_at": iso(self.dispute_ends_at),
                "rubric_version": RUBRIC_VERSION,
                "rubric_locked": self.rubric_locked_at is not None,
            },
            "clock": {
                "now": iso(now),
                "seconds_per_day": self.seconds_per_day,
                "label": "Demo time: 1 minute = 1 week"
                if abs(self.seconds_per_day - 8.571) < 0.01
                else f"Demo time: {self.seconds_per_day:g} s = 1 day",
                "day": day,
            },
            "prize": {
                "name": "Keychron B40 keyboard",
                "merchant": "Keychron",
                "list_price": "49.99",
                "image_url": None,
                "quote": {
                    "final_amount": f"{quote:.2f}",
                    "items": "49.99",
                    "shipping": "21.21",
                    "tax": "0.00",
                    "quoted_at": iso(now),
                    "source": "mock",
                },
            },
            "pool": {
                "entries": len([p for p in self.players if p.entry == "RESERVED"]),
                "gross": f"{gross:.2f}",
                "prize_quote": f"{quote:.2f}",
                "buffer": "9.00",
                "ceiling": "81.00",
                "surplus": f"{max(0.0, gross - quote):.2f}",
                "stand_in": "The pool is test USDC in the Kwal vault on Ink Sepolia, a labelled "
                "stand-in for the card the agent charges. No cash value.",
                "disclosure": "Entry 30.00 test USDC. Pool = 3 x 30.00. The agent may spend up to "
                "the pool less a 10% buffer on the prize, its shipping and tax. Refunded in full "
                "if the group does not start. Surplus is refunded pro rata.",
            },
            "players": [
                {
                    "player_id": p.player_id,
                    "name": p.name,
                    "seat": p.seat,
                    "entry": p.entry,
                    "intake": p.intake,
                    "accepted": p.accepted,
                    "contract": p.contract,
                    "score": ranks[p.player_id]["score"],
                    "verified_milestones": ranks[p.player_id]["verified_milestones"],
                    "last_verified_at": ranks[p.player_id]["last_verified_at"],
                    "rank": ranks[p.player_id]["rank"],
                }
                for p in self.players
            ],
            "rubric": {
                "version": RUBRIC_VERSION,
                "total_points": 100,
                "milestone_points": MILESTONE_POINTS,
                "rules": [
                    "Everyone has the same 100 points across four milestones.",
                    "A milestone scores its full points once, when a check-in in its window shows "
                    "the target met with the evidence the contract names.",
                    "A milestone's window opens at the previous milestone's day and closes at the "
                    "end of the challenge; points are capped per period: one milestone, once.",
                    "The vision model's reading of a photo is advisory and shown beside the "
                    "check-in; the rubric decides the points.",
                    "Duplicate evidence (the same file) is rejected; at most one check-in per "
                    "player every 10 seconds.",
                    "Tie-break: higher verified milestone score, then the earlier last verified "
                    "check-in.",
                ],
                "tie_break": "Higher verified milestone score, then earlier last verified "
                "check-in.",
                "locked_at": iso(self.rubric_locked_at),
            },
            "leaderboard": board,
            "result": self.result,
            "ledger_tail": self.ledger[-8:],
            "ledger_intact": True,
        }

    def join(self, name: str) -> Player:
        """Reserves a seat and an entry.

        Args:
            name: The player's display name.

        Returns:
            The new player.

        Raises:
            Refusal: When the group is full or the name is empty.
        """
        self.need("OPEN_FOR_JOINING")
        if not name.strip():
            raise Refusal("NAME_REQUIRED", "Enter a name for your seat.", 422)
        if len(self.players) >= 3:
            raise Refusal("GROUP_FULL", "Every seat is taken.")
        p = Player(f"ply_{secrets.token_hex(4)}", name.strip()[:32], len(self.players) + 1)
        self.players.append(p)
        self.append("entry.reserved", p.player_id, f"{p.name}: entry of 30.00 test USDC reserved.")
        if len(self.players) == 3:
            self.status = "INTAKE"
        return p

    def propose(self, p: Player, goal_type: str) -> dict[str, Any]:
        """Builds a proposed contract for a player from a goal template.

        Args:
            p: The player.
            goal_type: The template key.

        Returns:
            The contract.
        """
        g = GOALS[goal_type]
        p.contract = {
            "participant_id": p.player_id,
            "goal_type": goal_type,
            "goal_statement": g["goal_statement"],
            "baseline": {"value": g["baseline"], "unit": g["unit"], "verified": False},
            "target": {"value": g["target"], "unit": g["unit"]},
            "duration_days": DURATION_DAYS,
            "milestones": [
                {
                    "index": i,
                    "day": 7 * (i + 1),
                    "target": t,
                    "max_points": MILESTONE_POINTS[i],
                    "opens_at": None,
                    "due_at": None,
                }
                for i, t in enumerate(g["milestones"])
            ],
            "evidence_policy": g["evidence_policy"],
            "total_max_points": 100,
            "rubric_version": RUBRIC_VERSION,
            "comparability": g["comparability"],
            "model": "fixture/scripted-coach",
            "prompt_version": "coach-v1",
            "status": "PROPOSED",
        }
        p.intake = "PROPOSED"
        self.append("contract.proposed", p.player_id, f"{p.name}: contract proposed.")
        return p.contract

    def intake(self, p: Player, message: str) -> dict[str, Any]:
        """Answers one chat turn from the scripted coach.

        Args:
            p: The player.
            message: What the player typed.

        Returns:
            The reply, the contract when proposed, and whether intake is done.
        """
        self.need("INTAKE")
        if p.contract and p.contract["status"] != "PROPOSED":
            raise Refusal("CONTRACT_LOCKED", "Your contract is locked.")
        p.turns += 1
        p.intake = "CHATTING" if p.intake == "NOT_STARTED" else p.intake
        text = message.lower()
        if p.turns == 1 and len(text) < 40:
            return {
                "reply": "Thanks. Where are you today, how much time can you give it each "
                "week, and how would you prove a session: a photo, a short clip or your own log?",
                "contract": None,
                "done": False,
            }
        goal = "push_up_improvement"
        if "run" in text:
            goal = "run_consistency"
        elif "strength" in text or "gym" in text or "weights" in text:
            goal = "strength_routine"
        contract = self.propose(p, goal)
        return {
            "reply": "Here is a contract I think is fair against the others: four milestones, "
            "the same 100 points as everyone. Change anything that does not fit, then lock it.",
            "contract": contract,
            "done": True,
        }

    def edit(self, p: Player, body: dict[str, Any]) -> dict[str, Any]:
        """Applies a player's edits to their proposed contract.

        Args:
            p: The player.
            body: The edited fields.

        Returns:
            The contract.

        Raises:
            Refusal: When locked or the edit is invalid.
        """
        self.need("INTAKE")
        c = p.contract
        if not c:
            raise Refusal("CONTRACT_INVALID", "Talk to the coach first.")
        if c["status"] != "PROPOSED":
            raise Refusal("CONTRACT_LOCKED", "Your contract is locked.")
        targets = body.get("milestone_targets")
        if targets is not None and (len(targets) != 4 or any(b < a for a, b in pairwise(targets))):
            raise Refusal(
                "CONTRACT_INVALID", "Milestone targets must rise from one to the next.", 422
            )
        if body.get("goal_statement"):
            c["goal_statement"] = str(body["goal_statement"])
        if body.get("unit"):
            c["target"]["unit"] = c["baseline"]["unit"] = str(body["unit"])
        if body.get("baseline_value") is not None:
            c["baseline"]["value"] = body["baseline_value"]
        if body.get("target_value") is not None:
            c["target"]["value"] = body["target_value"]
        if targets is not None:
            for m, t in zip(c["milestones"], targets, strict=True):
                m["target"] = t
        return c

    def lock_contract(self, p: Player) -> dict[str, Any]:
        """Locks a player's contract and opens the agreement when every seat has locked.

        Args:
            p: The player.

        Returns:
            The contract.

        Raises:
            Refusal: When there is no contract.
        """
        self.need("INTAKE")
        if not p.contract:
            raise Refusal("CONTRACT_INVALID", "Talk to the coach first.")
        p.contract["status"] = "LOCKED"
        p.intake = "LOCKED"
        self.append("contract.locked", p.player_id, f"{p.name}: contract locked.")
        if all(x.intake == "LOCKED" for x in self.players):
            self.status = "READY_FOR_ACCEPTANCE"
            self.rubric_locked_at = utcnow()
            self.append("rubric.locked", "grp_main", f"Rubric {RUBRIC_VERSION} locked.")
        return p.contract

    def accept(self, p: Player) -> None:
        """Records an acceptance and starts the challenge on the last one.

        Args:
            p: The player.

        Raises:
            Refusal: When already accepted.
        """
        self.need("READY_FOR_ACCEPTANCE")
        if p.accepted:
            raise Refusal("ALREADY_ACCEPTED", "You have already accepted.")
        p.accepted = True
        assert p.contract is not None
        p.contract["status"] = "ACCEPTED"
        self.append(
            "contract.accepted", p.player_id, f"{p.name}: accepted every contract and the rubric."
        )
        if all(x.accepted for x in self.players):
            self.start()

    def start(self) -> None:
        """Starts the timer and opens the milestone windows."""
        now = utcnow()
        self.status = "ACTIVE"
        self.started_at = now
        self.ends_at = now + timedelta(seconds=self.seconds_per_day * DURATION_DAYS)
        for p in self.players:
            assert p.contract is not None
            for m in p.contract["milestones"]:
                m["opens_at"] = iso(now + timedelta(seconds=self.seconds_per_day * (m["day"] - 7)))
                m["due_at"] = iso(now + timedelta(seconds=self.seconds_per_day * m["day"]))
        self.append("group.started", "grp_main", "The challenge started: every seat accepted.")

    def decline(self, p: Player) -> None:
        """Cancels the group and refunds every entry.

        Args:
            p: The player who declined.
        """
        self.need("READY_FOR_ACCEPTANCE")
        self.status = "CANCELLED"
        self.append("group.cancelled", "grp_main", f"{p.name} declined: the group is cancelled.")
        for x in self.players:
            x.entry = "REFUNDED"
            self.append("entry.refunded", x.player_id, f"{x.name}: entry of 30.00 refunded.")

    def checkin(self, fields: dict[str, str], upload: tuple[str, bytes] | None) -> dict[str, Any]:
        """Scores one check-in by the rubric.

        Args:
            fields: The form fields.
            upload: The evidence file's content type and bytes, if any.

        Returns:
            The score event.

        Raises:
            Refusal: On any rubric or integrity failure.
        """
        self.need("ACTIVE")
        p = self.player(fields.get("player_id", ""))
        assert p.contract is not None
        index = int(fields.get("milestone", "0"))
        milestone = p.contract["milestones"][index]
        if any(
            e["participant_id"] == p.player_id
            and e["milestone"] == index
            and e["state"] == "VERIFIED"
            for e in self.events
        ):
            raise Refusal("MILESTONE_ALREADY_SCORED", "That milestone is already verified.")
        if milestone["opens_at"] and milestone["opens_at"] > (iso(utcnow()) or ""):
            raise Refusal(
                "MILESTONE_NOT_OPEN", f"The day {milestone['day']} milestone is not open yet."
            )
        evidence_id = None
        kind = "log"
        if upload:
            digest = hashlib.sha256(upload[1]).hexdigest()
            if digest in self.hashes:
                raise Refusal("DUPLICATE_EVIDENCE", "That file was already used for a check-in.")
            self.hashes.add(digest)
            evidence_id = f"evd_{secrets.token_hex(4)}"
            self.evidence[evidence_id] = upload
            kind = "clip" if upload[0].startswith("video/") else "photo"
        elif p.contract["evidence_policy"] != "log":
            raise Refusal("EVIDENCE_REQUIRED", "This contract needs a photo or a clip.", 422)
        value = float(fields.get("value", "0") or 0)
        unit = p.contract["target"]["unit"]
        met = value >= milestone["target"]
        advisory = None
        if kind == "photo":
            advisory = {
                "model": "Qwen/Qwen3-VL-8B-Instruct",
                "shows": True,
                "count": int(value),
                "note": "fixture reading",
                "agrees": met,
            }
        event = {
            "event_id": f"sev_{secrets.token_hex(4)}",
            "participant_id": p.player_id,
            "group_id": "grp_main",
            "milestone": index,
            "evidence_id": evidence_id,
            "evidence_kind": kind,
            "claimed_value": value if value % 1 else int(value),
            "rubric_version": RUBRIC_VERSION,
            "at": iso(utcnow()),
            "delta": milestone["max_points"] if met else 0,
            "state": "VERIFIED" if met else "REJECTED",
            "reason": f"Target {milestone['target']} {unit} met with a {kind}."
            if met
            else f"Claimed {fields.get('value')} {unit}, below the target of "
            f"{milestone['target']}.",
            "advisory": advisory,
        }
        self.events.append(event)
        self.append(
            "score.recorded",
            event["event_id"],
            f"{p.name}: milestone {index + 1} "
            + (f"verified, +{event['delta']}" if met else "rejected, below the target"),
        )
        return event

    def dispute(self, p: Player, event_id: str, reason: str) -> dict[str, Any]:
        """Marks a score event disputed.

        Args:
            p: The disputing player.
            event_id: The score event.
            reason: Why.

        Returns:
            The event.

        Raises:
            Refusal: When the window is closed or the event is unknown.
        """
        self.need("DISPUTE_WINDOW")
        if self.dispute_ends_at and utcnow() >= self.dispute_ends_at:
            raise Refusal("DISPUTE_WINDOW_CLOSED", "The dispute window has closed.")
        for e in self.events:
            if e["event_id"] == event_id:
                e["state"] = "DISPUTED"
                e["reason"] = f"Disputed by {p.name}: {reason}"
                self.append("score.disputed", event_id, f"{p.name} disputed a score: {reason}")
                return e
        raise Refusal("UNKNOWN_EVENT", "No such check-in.", 404)

    def review(self, event_id: str, reinstate: bool) -> dict[str, Any]:
        """Records the reviewer's verdict on a disputed check-in.

        Args:
            event_id: The disputed score event.
            reinstate: True to restore its points, False to reject it.

        Returns:
            The event.

        Raises:
            Refusal: When the event is unknown or not disputed.
        """
        self.need("DISPUTE_WINDOW")
        for e in self.events:
            if e["event_id"] == event_id and e["state"] == "DISPUTED":
                e["state"] = "VERIFIED" if reinstate else "REJECTED"
                if not reinstate:
                    e["delta"] = 0
                verdict = "reinstated" if reinstate else "rejected"
                e["reason"] = f"Reviewed: {verdict}."
                self.append(
                    "score.reviewed", event_id, f"A reviewer {verdict} a disputed check-in."
                )
                return e
        raise Refusal("UNKNOWN_EVENT", "No disputed check-in with that id.", 404)

    def finalize(self) -> dict[str, Any]:
        """Names the winner and simulates the agent's purchase.

        Returns:
            The result.

        Raises:
            Refusal: While the dispute window is open.
        """
        self.need("DISPUTE_WINDOW", "FINALIZED")
        if (
            self.status == "DISPUTE_WINDOW"
            and self.dispute_ends_at
            and utcnow() < self.dispute_ends_at
        ):
            raise Refusal("WRONG_STATE", "The dispute window is still open.")
        board = self.leaderboard()
        winner = board[0]
        self.status = "FINALIZED"
        self.append(
            "group.finalized",
            winner["player_id"],
            f"{winner['name']} wins with {winner['score']} points.",
        )
        self.append(
            "quote.landed",
            "qt_fixture",
            "Landed quote 71.20 USD for the Keychron B40, shipped to Singapore.",
        )
        self.append(
            "policy.decided", "qt_fixture", "Gate: allow, 71.20 is within the ceiling of 81.00."
        )
        self.append("checkout.created", "chk_fixture", "Checkout created under an idempotency key.")
        order_id = f"ord_{secrets.token_hex(6)}"
        self.append(
            "prize.purchased", order_id, f"Prize purchased for {winner['name']}: order {order_id}."
        )
        self.status = "FULFILLED"
        self.result = {
            "winner": {
                "player_id": winner["player_id"],
                "name": winner["name"],
                "score": winner["score"],
            },
            "standings": board,
            "tie_break_applied": len(board) > 1 and board[0]["score"] == board[1]["score"],
            "purchase": {
                "status": "PURCHASED",
                "step": "done",
                "backend": "mock",
                "quote_final_amount": "71.20",
                "ceiling": "81.00",
                "gate": {
                    "disposition": "allow",
                    "rule": "all_rules_passed",
                    "reason": "71.20 is within the ceiling of 81.00.",
                },
                "order_id": order_id,
                "checkout_id": "chk_fixture",
                "final_amount": "71.20",
                "error": None,
                "stand_in": "The pool is test USDC in the Kwal vault on Ink Sepolia, a labelled "
                "stand-in for the card the agent charges.",
            },
        }
        return self.result


def seed(room: Room, stage: str) -> None:
    """Moves a fresh room to a named stage by the same steps a player takes.

    Args:
        room: The fresh room.
        stage: One of ``SEEDS``.
    """
    if stage == "open":
        room.join("Alice")
        return
    names = [("Alice", "push-ups"), ("Ben", "running"), ("Chloe", "strength")]
    players = [room.join(n) for n, _ in names]
    room.append("group.opened", "grp_main", "Group opened: Earn your Keychron B40.")
    if stage == "intake":
        room.intake(players[1], "I want to start running, three runs a week from nothing.")
        return
    for p, (_, goal) in zip(players, names, strict=True):
        room.intake(p, f"My goal is {goal}, a few times a week, I can log or film it.")
        room.lock_contract(p)
    if stage == "agreement":
        room.accept(players[1])
        return
    if stage == "cancelled":
        room.decline(players[2])
        return
    for p in players:
        room.accept(p)
    assert room.started_at is not None
    room.started_at -= timedelta(seconds=room.seconds_per_day * 9)
    for p in players:
        assert p.contract is not None
        for m in p.contract["milestones"]:
            m["opens_at"] = iso(
                room.started_at + timedelta(seconds=room.seconds_per_day * (m["day"] - 7))
            )
            m["due_at"] = iso(room.started_at + timedelta(seconds=room.seconds_per_day * m["day"]))
    room.ends_at = room.started_at + timedelta(seconds=room.seconds_per_day * DURATION_DAYS)
    room.checkin(
        {"player_id": players[0].player_id, "milestone": "0", "value": "9"}, ("image/jpeg", b"a")
    )
    room.checkin(
        {"player_id": players[1].player_id, "milestone": "0", "value": "1"}, ("image/jpeg", b"b")
    )
    room.checkin({"player_id": players[2].player_id, "milestone": "0", "value": "0"}, None)
    room.checkin(
        {"player_id": players[0].player_id, "milestone": "1", "value": "11"}, ("image/jpeg", b"c")
    )
    if stage == "active":
        return
    room.ends_at = utcnow()
    room.tick()
    if stage == "fulfilled":
        room.dispute_ends_at = utcnow()
        room.finalize()


class Handler(BaseHTTPRequestHandler):
    """Serves the static client and the API from one shared room."""

    room: Room
    seed_stage: str | None = None

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keeps the console quiet.

        Args:
            format: Unused.
            *args: Unused.
        """

    def send_json(self, status: int, body: object) -> None:
        """Writes a JSON answer.

        Args:
            status: The HTTP status.
            body: The document.
        """
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        """Reads a JSON body.

        Returns:
            The body, or an empty dict.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        """Serves the API's reads and the static files."""
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            self.api("GET", url.path, parse_qs(url.query))
            return
        rel = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
        target = (WEB_ROOT / rel).resolve()
        if not target.is_relative_to(WEB_ROOT) or not target.is_file():
            self.send_json(404, {"error": {"code": "NOT_FOUND", "message": "No such file."}})
            return
        data = target.read_bytes()
        self.send_response(200)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        """Serves the API's writes."""
        self.api("POST", urlparse(self.path).path, {})

    def do_PUT(self) -> None:
        """Serves the contract edit."""
        self.api("PUT", urlparse(self.path).path, {})

    def api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        """Routes one API call.

        Args:
            method: The HTTP method.
            path: The path.
            query: The query string.
        """
        room = self.room
        try:
            with room.lock:
                self.route(room, method, path, query)
        except Refusal as r:
            self.send_json(r.status, {"error": {"code": r.code, "message": r.message}})

    def route(  # noqa: PLR0912, PLR0915 - one flat table of the contract's routes
        self, room: Room, method: str, path: str, query: dict[str, list[str]]
    ) -> None:
        """Answers one API call inside the room's lock.

        Args:
            room: The room.
            method: The HTTP method.
            path: The path.
            query: The query string.

        Raises:
            Refusal: For an unknown route.
        """
        if method == "GET" and path == "/api/state":
            self.send_json(200, room.state())
        elif method == "GET" and path == "/api/ledger":
            after = int((query.get("after_seq") or ["0"])[0])
            self.send_json(
                200, {"chain_intact": True, "events": [e for e in room.ledger if e["seq"] > after]}
            )
        elif method == "GET" and path == "/api/events":
            pid = (query.get("player_id") or [""])[0]
            self.send_json(
                200, {"events": [e for e in room.events if not pid or e["participant_id"] == pid]}
            )
        elif method == "GET" and path.startswith("/api/evidence/"):
            item = room.evidence.get(path.rsplit("/", 1)[-1])
            if not item:
                raise Refusal("NOT_FOUND", "No such evidence.", 404)
            self.send_response(200)
            self.send_header("Content-Type", item[0])
            self.end_headers()
            self.wfile.write(item[1])
        elif path == "/api/join":
            p = room.join(str(self.read_json().get("name", "")))
            self.send_json(200, {"player_id": p.player_id, "state": room.state()})
        elif path == "/api/intake":
            body = self.read_json()
            time.sleep(0.6)
            self.send_json(
                200,
                room.intake(room.player(body.get("player_id", "")), str(body.get("message", ""))),
            )
        elif method == "PUT" and path == "/api/contract":
            body = self.read_json()
            self.send_json(
                200, {"contract": room.edit(room.player(body.get("player_id", "")), body)}
            )
        elif path == "/api/contract/lock":
            body = self.read_json()
            self.send_json(
                200, {"contract": room.lock_contract(room.player(body.get("player_id", "")))}
            )
        elif path == "/api/accept":
            room.accept(room.player(self.read_json().get("player_id", "")))
            self.send_json(200, {"state": room.state()})
        elif path == "/api/decline":
            room.decline(room.player(self.read_json().get("player_id", "")))
            self.send_json(200, {"state": room.state()})
        elif path == "/api/checkin":
            fields, upload = self.read_multipart()
            event = room.checkin(fields, upload)
            self.send_json(200, {"event": event, "state": room.state()})
        elif path == "/api/dispute":
            body = self.read_json()
            event = room.dispute(
                room.player(body.get("player_id", "")),
                body.get("event_id", ""),
                body.get("reason", ""),
            )
            self.send_json(200, {"event": event})
        elif path == "/api/dispute/review":
            body = self.read_json()
            event = room.review(str(body.get("event_id", "")), bool(body.get("reinstate")))
            self.send_json(200, {"event": event})
        elif path == "/api/finalize":
            self.read_json()
            time.sleep(1.5)
            result = room.finalize()
            self.send_json(200, {"result": result, "state": room.state()})
        elif path == "/api/reset":
            self.read_json()
            fresh = Room(room.seconds_per_day, room.dispute_seconds)
            Handler.room = fresh
            self.send_json(200, {"state": fresh.state()})
        else:
            raise Refusal("NOT_FOUND", f"No route {method} {path}.", HTTPStatus.NOT_FOUND)

    def read_multipart(self) -> tuple[dict[str, str], tuple[str, bytes] | None]:
        """Parses the check-in's multipart body.

        Returns:
            The text fields and the uploaded file, if any.
        """
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        head = f"Content-Type: {self.headers.get('Content-Type')}\r\n\r\n".encode()
        message = BytesParser(policy=HTTP).parsebytes(head + raw)
        fields: dict[str, str] = {}
        upload: tuple[str, bytes] | None = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if not isinstance(name, str) or not isinstance(payload, bytes):
                continue
            if part.get_filename():
                upload = (part.get_content_type(), payload)
            else:
                fields[name] = payload.decode()
        return fields, upload


def main() -> None:
    """Parses the arguments and serves until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--seed", choices=SEEDS, default=None)
    parser.add_argument("--seconds-per-day", type=float, default=8.571)
    parser.add_argument("--dispute-seconds", type=float, default=20.0)
    args = parser.parse_args()
    room = Room(args.seconds_per_day, args.dispute_seconds)
    if args.seed:
        seed(room, args.seed)
    Handler.room = room
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"fixture server on http://127.0.0.1:{args.port} ({args.seed or 'fresh'})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

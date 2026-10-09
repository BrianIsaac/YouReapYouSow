"""The challenge's state machine: seats, entries, contracts, acceptance, scores and finish.

One group runs at a time. Every change to a record and the ledger event that records it
commit in one transaction. Time moves the group on its own: ``tick`` is called on every
request and applies whatever deadline has passed (the enrolment deadline, the challenge's
end, the dispute window's end), so nothing depends on a background job.

The money is test USDC: an entry is a reservation on the ledger against the vault's
balance, never a transfer, and a refund is a release of that reservation.
"""

import hashlib
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from youreapyousow.clock import Clock
from youreapyousow.game.coach import (
    MAX_VALUE,
    CoachDraft,
    CoachTurn,
    ContractInvalidError,
    build_contract,
    parse_number,
    template_draft,
)
from youreapyousow.game.models import (
    CENT,
    Advisory,
    ChatTurn,
    ContractStatus,
    EntryState,
    EvidenceKind,
    EvidencePolicy,
    GoalContract,
    Group,
    GroupStatus,
    GroupTerms,
    IntakeState,
    Player,
    PrizePurchase,
    Result,
    ScoreEvent,
    ScoreState,
)
from youreapyousow.game.rubric import (
    CHECKIN_MIN_INTERVAL_S,
    RUBRIC_V1,
    Rubric,
    decide,
    effective_states,
    standings,
)
from youreapyousow.ids import new_id
from youreapyousow.ledger.events import EventType
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.store import Database, Records

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
STAND_IN = (
    "The pool is test USDC in the Kwal vault on Ink Sepolia, a labelled stand-in for the "
    "card the agent charges. No cash value."
)


def _first_line(error: Exception) -> str:
    if isinstance(error, ValidationError):
        first = error.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return f"{where}: {first['msg']}" if where else str(first["msg"])
    return str(error).splitlines()[0]


class GameError(Exception):
    """A refused action, with a code the screen branches on and a line a person reads."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        """Name the refusal.

        Args:
            code: The machine-readable code, such as ``GROUP_FULL``.
            message: One line a person can read.
            status: The HTTP status it maps to.
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class EvidenceIn:
    """A check-in's uploaded file.

    Attributes:
        content: The bytes.
        content_type: Its media type, ``image/...`` or ``video/...``.
    """

    content: bytes
    content_type: str

    def kind(self) -> EvidenceKind:
        """Say whether it is a photo or a clip.

        Returns:
            The kind.

        Raises:
            GameError: If it is neither.
        """
        if self.content_type.startswith("image/"):
            return EvidenceKind.PHOTO
        if self.content_type.startswith("video/"):
            return EvidenceKind.CLIP
        raise GameError("EVIDENCE_TYPE", "Evidence must be a photo or a clip.", 415)

    def suffix(self) -> str:
        """Return a file suffix for its type.

        Returns:
            Such as ``.jpg``.
        """
        return mimetypes.guess_extension(self.content_type.split(";")[0].strip()) or ".bin"


class Pool(BaseModel):
    """The pool's disclosure: gross, buffer, the prize ceiling and what is left.

    Attributes:
        entries: Entries reserved.
        entry_amount: One entry.
        gross: Entries times the entry amount.
        buffer: The share held back from the prize.
        ceiling: What the agent may spend on the prize: gross less the buffer.
        prize_quote: The landed quote for the prize, when known.
        surplus: The ceiling less the quote, refunded pro rata.
    """

    model_config = ConfigDict(frozen=True)

    entries: int
    entry_amount: Decimal
    gross: Decimal
    buffer: Decimal
    ceiling: Decimal
    prize_quote: Decimal | None
    surplus: Decimal | None


def compute_pool(
    entry_amount: Decimal, entries: int, buffer_rate: Decimal, prize_quote: Decimal | None
) -> Pool:
    """Compute the pool's figures: ``gross = N x E``, the buffer, the ceiling.

    Args:
        entry_amount: One entry.
        entries: How many are reserved.
        buffer_rate: The share of gross held back.
        prize_quote: The landed quote, if known.

    Returns:
        The pool.
    """
    gross = (entry_amount * entries).quantize(CENT)
    buffer = (gross * buffer_rate).quantize(CENT)
    ceiling = gross - buffer
    surplus = None if prize_quote is None else ceiling - prize_quote
    return Pool(
        entries=entries,
        entry_amount=entry_amount.quantize(CENT),
        gross=gross,
        buffer=buffer,
        ceiling=ceiling,
        prize_quote=prize_quote,
        surplus=surplus,
    )


def money(value: Decimal) -> str:
    """Format an amount with two decimals.

    Args:
        value: The amount.

    Returns:
        Such as ``"25.00"``.
    """
    return str(value.quantize(CENT))


@dataclass
class _Stored[T]:
    record: T
    version: int


class GameService:
    """The one group's state machine over versioned records and the ledger."""

    def __init__(
        self,
        *,
        db: Database,
        ledger: Ledger,
        clock: Clock,
        terms: GroupTerms,
        vault_balance: Decimal,
        vault_source: str,
        evidence_dir: Path,
        rubric: Rubric = RUBRIC_V1,
        drop_id: str = "drop",
        reserved_elsewhere: Callable[[], Decimal] = lambda: Decimal(0),
    ) -> None:
        """Wire the service.

        Args:
            db: The shared database.
            ledger: The ledger.
            clock: Time source.
            terms: The terms each new group opens with.
            vault_balance: The vault's test USDC that entries are reserved against.
            vault_source: Where that balance was read from, for the ledger.
            evidence_dir: Where check-in evidence is stored.
            rubric: The rubric groups are scored under.
            drop_id: The drop this service runs; its groups are listed under it.
            reserved_elsewhere: What other drops have reserved against the same vault.
        """
        self.db = db
        self.ledger = ledger
        self.clock = clock
        self.terms = terms
        self.vault_balance = vault_balance
        self.vault_source = vault_source
        self.evidence_dir = evidence_dir
        self.rubric = rubric
        self.drop_id = drop_id
        self.reserved_elsewhere = reserved_elsewhere
        self.prize_quote: Decimal | None = None
        self._groups = Records(db, "game_group", Group)
        self._players = Records(db, "game_player", Player)
        self._scores = Records(db, "score_event", ScoreEvent)

    # Reading

    def _current(self) -> _Stored[Group]:
        groups = self._groups.list(objective_id=self.drop_id)
        if not groups:
            raise GameError("NO_GROUP", "No group is open.", 404)
        record, version = self._groups.require(groups[-1].id)
        return _Stored(record, version)

    def has_group(self) -> bool:
        """Say whether any group was ever opened.

        Returns:
            True when one was.
        """
        return bool(self._groups.list(objective_id=self.drop_id))

    def group_ids(self) -> list[str]:
        """Return every group this drop has run, oldest first.

        Returns:
            The group ids.
        """
        return [g.id for g in self._groups.list(objective_id=self.drop_id)]

    def reserved(self) -> Decimal:
        """Return what this drop's current group holds reserved against the vault.

        Returns:
            The entries reserved, in test USDC.
        """
        if not self.has_group():
            return Decimal(0)
        entries = sum(1 for p in self.players() if p.entry == EntryState.RESERVED)
        return self.group().terms.entry_amount * entries

    def group(self) -> Group:
        """Return the current group.

        Returns:
            The group.
        """
        return self._current().record

    def players(self) -> list[Player]:
        """Return the current group's players, by seat.

        Returns:
            The players.
        """
        return sorted(self._players.list(objective_id=self.group().id), key=lambda p: p.seat)

    def player(self, player_id: str) -> _Stored[Player]:
        """Return a player of the current group with its version.

        Args:
            player_id: The player.

        Returns:
            The stored player.

        Raises:
            GameError: If no such player is in the current group.
        """
        found = self._players.get(player_id)
        if found is None or found[0].group_id != self.group().id:
            raise GameError("UNKNOWN_PLAYER", "That player is not in this group.", 404)
        return _Stored(found[0], found[1])

    def score_events(self) -> list[ScoreEvent]:
        """Return the current group's score events, in order.

        Returns:
            The events.
        """
        return sorted(self._scores.list(objective_id=self.group().id), key=lambda e: e.seq)

    def pool(self) -> Pool:
        """Return the current pool's disclosure.

        Returns:
            The pool.
        """
        entries = sum(1 for p in self.players() if p.entry == EntryState.RESERVED)
        return compute_pool(
            self.terms.entry_amount, entries, self.terms.buffer_rate, self.prize_quote
        )

    def _require(self, *statuses: GroupStatus) -> _Stored[Group]:
        current = self._current()
        if current.record.status not in statuses:
            wanted = " or ".join(s.value for s in statuses)
            raise GameError(
                "WRONG_STATE",
                f"The group is {current.record.status.value}; this needs {wanted}.",
            )
        return current

    # Writing

    def _event(
        self,
        type_: EventType,
        group: Group,
        subject_id: str,
        payload: dict[str, JsonValue] | None = None,
        refs: dict[str, str] | None = None,
    ) -> None:
        self.ledger.append(
            type_, subject_id=subject_id, objective_id=group.id, refs=refs, payload=payload
        )

    def _save_group(self, stored: _Stored[Group], group: Group) -> _Stored[Group]:
        version = self._groups.update(group, record_id=group.id, expected_version=stored.version)
        return _Stored(group, version)

    def _save_player(self, stored: _Stored[Player], player: Player) -> _Stored[Player]:
        version = self._players.update(player, record_id=player.id, expected_version=stored.version)
        return _Stored(player, version)

    def open_group(self, lead_s: float | None = None) -> Group:
        """Publish a new group on the configured terms, with its fixed date range.

        Args:
            lead_s: Real seconds from now to the fixed start; the terms' window when None.

        Returns:
            The group, open for joining.
        """
        now = self.clock()
        terms = self.terms
        if lead_s is not None:
            terms = terms.model_copy(update={"enrolment_window_s": max(1.0, lead_s)})
        starts_at = now + timedelta(seconds=terms.enrolment_window_s)
        group = Group(
            id=new_id("grp"),
            terms=terms,
            status=GroupStatus.OPEN_FOR_JOINING,
            opened_at=now,
            enrolment_deadline=starts_at,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(seconds=terms.duration_days * terms.seconds_per_day),
            rubric_version=self.rubric.version,
        )
        with self.db.transaction():
            self._groups.insert(group, record_id=group.id, objective_id=self.drop_id)
            self._event(
                EventType.GROUP_OPENED,
                group,
                group.id,
                {
                    "title": group.terms.title,
                    "entry_amount": money(group.terms.entry_amount),
                    "currency": group.terms.currency,
                    "min_players": group.terms.min_players,
                    "max_players": group.terms.max_players,
                    "duration_days": group.terms.duration_days,
                    "seconds_per_day": group.terms.seconds_per_day,
                    "enrolment_deadline": group.enrolment_deadline.isoformat(),
                    "drop_id": self.drop_id,
                    "starts_at": starts_at.isoformat(),
                    "ends_at": group.ends_at.isoformat() if group.ends_at else None,
                    "rubric_version": group.rubric_version,
                    "buffer_rate": str(group.terms.buffer_rate),
                },
            )
        return group

    def reset(self, lead_s: float | None = None) -> Group:
        """Cancel the current group if unfinished, refunding its entries, and republish.

        Args:
            lead_s: Real seconds from now to the new fixed start; the terms' window when None.

        Returns:
            The new group.
        """
        current = self._current()
        if current.record.status not in (GroupStatus.FULFILLED, GroupStatus.CANCELLED):
            self._cancel(current, "reset by the operator")
        return self.open_group(lead_s)

    def join(self, name: str) -> Player:
        """Reserve a seat and an entry against the vault.

        Args:
            name: The display name.

        Returns:
            The new player.

        Raises:
            GameError: If the name is blank, the group is not open or full, or the vault
                cannot cover another entry.
        """
        self.tick()
        name = " ".join(name.split())
        if not name or len(name) > 40:
            raise GameError("INVALID_NAME", "Give a name of 1 to 40 characters.", 422)
        stored = self._current()
        group = stored.record
        players = self.players()
        if group.status != GroupStatus.OPEN_FOR_JOINING or len(players) >= group.terms.max_players:
            if len(players) >= group.terms.max_players:
                raise GameError("GROUP_FULL", "Every seat in this group is taken.")
            self._require(GroupStatus.OPEN_FOR_JOINING)
        entry = group.terms.entry_amount
        reserved_total = entry * (len(players) + 1) + self.reserved_elsewhere()
        if reserved_total > self.vault_balance:
            raise GameError(
                "VAULT_INSUFFICIENT",
                f"The vault holds {money(self.vault_balance)} test USDC; another entry "
                f"would reserve {money(reserved_total)}.",
            )
        player = Player(
            id=new_id("ply"),
            group_id=group.id,
            name=name,
            seat=len(players) + 1,
            joined_at=self.clock(),
        )
        with self.db.transaction():
            self._players.insert(player, record_id=player.id, objective_id=group.id)
            self._event(
                EventType.ENTRY_RESERVED,
                group,
                player.id,
                {
                    "name": player.name,
                    "seat": player.seat,
                    "amount": money(entry),
                    "currency": group.terms.currency,
                    "reserved_total": money(reserved_total),
                    "vault_balance": money(self.vault_balance),
                    "vault_source": self.vault_source,
                    "stand_in": STAND_IN,
                },
            )
            if player.seat == group.terms.max_players:
                self._save_group(stored, group.model_copy(update={"status": GroupStatus.INTAKE}))
                self._event(EventType.GROUP_INTAKE, group, group.id, {"players": player.seat})
        return player

    def _cancel(self, stored: _Stored[Group], reason: str) -> None:
        group = stored.record
        with self.db.transaction():
            stored = self._save_group(
                stored,
                group.model_copy(
                    update={"status": GroupStatus.REFUNDING, "cancelled_reason": reason}
                ),
            )
            self._event(EventType.GROUP_REFUNDING, group, group.id, {"reason": reason})
            for player in self.players():
                if player.entry != EntryState.RESERVED:
                    continue
                pstored = self.player(player.id)
                self._save_player(pstored, player.model_copy(update={"entry": EntryState.REFUNDED}))
                self._event(
                    EventType.ENTRY_REFUNDED,
                    group,
                    player.id,
                    {
                        "name": player.name,
                        "amount": money(group.terms.entry_amount),
                        "currency": group.terms.currency,
                        "reason": reason,
                    },
                )
            self._save_group(
                stored, stored.record.model_copy(update={"status": GroupStatus.CANCELLED})
            )
            self._event(EventType.GROUP_CANCELLED, group, group.id, {"reason": reason})

    # The coach and the contract

    def intake_player(self, player_id: str) -> Player:
        """Return a player who may talk to the coach now.

        Args:
            player_id: The player.

        Returns:
            The player.

        Raises:
            GameError: If the group is not in intake or the contract is locked.
        """
        self.tick()
        self._require(GroupStatus.INTAKE)
        player = self.player(player_id).record
        if player.intake == IntakeState.LOCKED:
            raise GameError("CONTRACT_LOCKED", "Your contract is locked.")
        return player

    def record_intake(self, player_id: str, message: str, turn: CoachTurn) -> Player:
        """Store a coach turn and, when it drafts one, the proposed contract.

        Args:
            player_id: The player.
            message: What the player said.
            turn: What the coach answered.

        Returns:
            The player, with the transcript and any proposed contract.

        Raises:
            GameError: If the state changed meanwhile, or the draft cannot be a contract.
        """
        self.intake_player(player_id)
        group = self.group()
        stored = self.player(player_id)
        player = stored.record
        transcript = (
            *player.transcript,
            ChatTurn(role="user", content=message),
            ChatTurn(role="assistant", content=turn.reply),
        )
        update: dict[str, object] = {"transcript": transcript, "intake": IntakeState.CHATTING}
        contract = None
        if turn.draft is not None:
            try:
                contract = build_contract(
                    participant_id=player.id,
                    draft=turn.draft,
                    rubric=self.rubric,
                    duration_days=group.terms.duration_days,
                    model=turn.model,
                )
            except ContractInvalidError:
                contract = build_contract(
                    participant_id=player.id,
                    draft=template_draft(message),
                    rubric=self.rubric,
                    duration_days=group.terms.duration_days,
                    model="template",
                )
            update |= {"contract": contract, "intake": IntakeState.PROPOSED}
        with self.db.transaction():
            saved = self._save_player(stored, player.model_copy(update=update))
            if contract is not None:
                self._contract_event(EventType.CONTRACT_PROPOSED, group, contract, message, turn)
        return saved.record

    def _contract_event(
        self,
        type_: EventType,
        group: Group,
        contract: GoalContract,
        message: str = "",
        turn: CoachTurn | None = None,
    ) -> None:
        payload: dict[str, JsonValue] = {
            "contract": contract.model_dump(mode="json"),
            "model": contract.model,
            "prompt_version": contract.prompt_version,
            "rubric_version": contract.rubric_version,
        }
        if turn is not None:
            payload["input_summary"] = message[:200]
            payload["coach_note"] = turn.note
        self._event(type_, group, contract.participant_id, payload)

    def edit_contract(self, player_id: str, changes: dict[str, object]) -> GoalContract:
        """Apply the player's edits to a proposed contract, re-validated; points unchanged.

        Args:
            player_id: The player.
            changes: Any of ``goal_statement``, ``baseline_value``, ``target_value``,
                ``unit`` and ``milestone_targets``.

        Returns:
            The edited contract.

        Raises:
            GameError: If there is no contract to edit, it is locked, or the edit is not
                a fair contract.
        """
        self.intake_player(player_id)
        group = self.group()
        stored = self.player(player_id)
        current = stored.record.contract
        if current is None:
            raise GameError("NO_CONTRACT", "Talk to the coach first; there is no contract yet.")
        try:
            statement = str(changes.get("goal_statement") or current.goal_statement).strip()
            raw_targets = changes.get("milestone_targets")
            targets = (
                [parse_number(v) for v in cast(list[object], raw_targets)]
                if isinstance(raw_targets, list)
                else [m.target for m in current.milestones]
            )
            draft = CoachDraft(
                goal_type=current.goal_type,
                goal_statement=statement,
                baseline_value=parse_number(changes.get("baseline_value", current.baseline.value)),
                unit=str(changes.get("unit") or current.baseline.unit),
                target_value=parse_number(changes.get("target_value", current.target.value)),
                milestone_targets=targets,
                evidence_policy=current.evidence_policy,
                comparability=current.comparability,
            )
            contract = build_contract(
                participant_id=player_id,
                draft=draft,
                rubric=self.rubric,
                duration_days=group.terms.duration_days,
                model=current.model,
            )
        except (ContractInvalidError, ValidationError) as error:
            raise GameError("CONTRACT_INVALID", _first_line(error), 422) from error
        with self.db.transaction():
            self._save_player(stored, stored.record.model_copy(update={"contract": contract}))
            self._contract_event(
                EventType.CONTRACT_PROPOSED, group, contract, "edited by the player"
            )
        return contract

    def lock_contract(self, player_id: str) -> GoalContract:
        """Lock the player's contract; when every seat is locked, the group is ready.

        Args:
            player_id: The player.

        Returns:
            The locked contract.

        Raises:
            GameError: If there is no contract or it is already locked.
        """
        self.intake_player(player_id)
        stored_group = self._current()
        group = stored_group.record
        stored = self.player(player_id)
        current = stored.record.contract
        if current is None:
            raise GameError("NO_CONTRACT", "Talk to the coach first; there is no contract yet.")
        locked = current.model_copy(update={"status": ContractStatus.LOCKED})
        with self.db.transaction():
            self._save_player(
                stored,
                stored.record.model_copy(update={"contract": locked, "intake": IntakeState.LOCKED}),
            )
            self._contract_event(EventType.CONTRACT_LOCKED, group, locked)
            players = self.players()
            if len(players) >= group.terms.min_players and all(
                p.intake == IntakeState.LOCKED for p in players
            ):
                self._save_group(
                    stored_group,
                    group.model_copy(update={"status": GroupStatus.READY_FOR_ACCEPTANCE}),
                )
                self._event(
                    EventType.GROUP_READY,
                    group,
                    group.id,
                    {"players": len(players), "rubric_version": group.rubric_version},
                )
        return locked

    # Acceptance and start

    def accept(self, player_id: str) -> Group:
        """Record the player's acceptance; the last one starts the challenge.

        The challenge starts only on the quorum, every seat funded, every contract locked
        and every player accepting. The rubric is locked in the same transaction.

        Args:
            player_id: The player.

        Returns:
            The group after the acceptance.

        Raises:
            GameError: If the group is not ready or the player has already accepted.
        """
        self.tick()
        stored_group = self._require(GroupStatus.READY_FOR_ACCEPTANCE)
        group = stored_group.record
        stored = self.player(player_id)
        player = stored.record
        if player.accepted_at is not None:
            raise GameError("ALREADY_ACCEPTED", "You have already accepted.")
        if player.contract is None or player.contract.status != ContractStatus.LOCKED:
            raise GameError("NOT_LOCKED", "Lock your contract before accepting.")
        now = self.clock()
        accepted = player.contract.model_copy(update={"status": ContractStatus.ACCEPTED})
        with self.db.transaction():
            self._save_player(
                stored, player.model_copy(update={"accepted_at": now, "contract": accepted})
            )
            self._contract_event(EventType.CONTRACT_ACCEPTED, group, accepted)
        return self.group()

    def _startable(self, group: Group) -> bool:
        players = self.players()
        return (
            group.status == GroupStatus.READY_FOR_ACCEPTANCE
            and len(players) >= group.terms.min_players
            and all(p.entry == EntryState.RESERVED for p in players)
            and all(p.intake == IntakeState.LOCKED for p in players)
            and all(p.accepted_at is not None for p in players)
        )

    def _start(self, stored: _Stored[Group], now: datetime, players: int) -> None:
        group = stored.record
        ends_at = group.ends_at or now + timedelta(
            seconds=group.terms.duration_days * group.terms.seconds_per_day
        )
        started = group.model_copy(
            update={
                "status": GroupStatus.ACTIVE,
                "started_at": now,
                "ends_at": ends_at,
                "rubric_locked_at": now,
            }
        )
        self._save_group(stored, started)
        self._event(EventType.RUBRIC_LOCKED, group, group.id, self.rubric.model_dump(mode="json"))
        pool = self.pool()
        self._event(
            EventType.GROUP_STARTED,
            group,
            group.id,
            {
                "players": players,
                "started_at": now.isoformat(),
                "ends_at": ends_at.isoformat(),
                "duration_days": group.terms.duration_days,
                "seconds_per_day": group.terms.seconds_per_day,
                "pool_gross": money(pool.gross),
                "prize_ceiling": money(pool.ceiling),
            },
        )

    def decline(self, player_id: str) -> Group:
        """Record a player declining the agreement: the group cancels and refunds.

        Args:
            player_id: The player.

        Returns:
            The cancelled group.
        """
        self.tick()
        stored = self._require(GroupStatus.READY_FOR_ACCEPTANCE)
        player = self.player(player_id).record
        self._cancel(stored, f"{player.name} declined the agreement; every entry is refunded.")
        return self.group()

    # Check-ins and scoring

    def checkin_contract(self, player_id: str, milestone: int) -> GoalContract:
        """Check that a check-in may be made now, before any model is asked.

        Args:
            player_id: The player.
            milestone: The milestone index.

        Returns:
            The player's accepted contract.

        Raises:
            GameError: If the challenge is not running, the milestone is unknown, not open
                or already scored, or the player checked in too recently.
        """
        self.tick()
        group = self._require(GroupStatus.ACTIVE).record
        player = self.player(player_id).record
        contract = player.contract
        if contract is None:
            raise GameError("NO_CONTRACT", "You have no contract.")
        if not 0 <= milestone < len(contract.milestones):
            raise GameError("UNKNOWN_MILESTONE", "There is no such milestone.", 422)
        now = self.clock()
        if group.started_at is not None and milestone > 0:
            opens = group.started_at + timedelta(
                seconds=contract.milestones[milestone - 1].day * group.terms.seconds_per_day
            )
            if now < opens:
                day = contract.milestones[milestone - 1].day
                raise GameError(
                    "MILESTONE_NOT_OPEN", f"Milestone {milestone + 1} opens on day {day}."
                )
        states = effective_states(self.score_events())
        for event in self.score_events():
            if (
                event.supersedes is None
                and event.participant_id == player_id
                and event.milestone == milestone
                and states.get(event.event_id) in (ScoreState.VERIFIED, ScoreState.DISPUTED)
            ):
                raise GameError(
                    "MILESTONE_ALREADY_SCORED", f"Milestone {milestone + 1} is already scored."
                )
        last = player.last_checkin_at
        if last is not None and (now - last).total_seconds() < CHECKIN_MIN_INTERVAL_S:
            raise GameError(
                "RATE_LIMITED", "One check-in every 10 seconds; try again in a moment.", 429
            )
        return contract

    def checkin(
        self,
        player_id: str,
        *,
        milestone: int,
        value: Decimal,
        evidence: EvidenceIn | None,
        note: str = "",
        advisory: Advisory | None = None,
    ) -> ScoreEvent:
        """Record a check-in as a score event decided by the rubric.

        Args:
            player_id: The player.
            milestone: The milestone index.
            value: The value the player claims.
            evidence: The photo or clip, or None for a log entry.
            note: The player's note.
            advisory: The vision model's reading, if one was taken.

        Returns:
            The score event.

        Raises:
            GameError: If the check-in is not allowed now, the evidence is missing, too
                large or not a photo or clip, or the same file was used before.
        """
        contract = self.checkin_contract(player_id, milestone)
        group = self.group()
        kind = EvidenceKind.LOG
        digest = None
        if evidence is not None:
            kind = evidence.kind()
            if len(evidence.content) > MAX_EVIDENCE_BYTES:
                raise GameError("EVIDENCE_TOO_LARGE", "Evidence must be at most 8 MB.", 413)
            digest = hashlib.sha256(evidence.content).hexdigest()
            if any(e.evidence_sha256 == digest for e in self.score_events()):
                raise GameError("DUPLICATE_EVIDENCE", "This file was already used as evidence.")
        if contract.evidence_policy == EvidencePolicy.PHOTO_OR_CLIP and evidence is None:
            raise GameError(
                "EVIDENCE_REQUIRED", "This goal needs a photo or a clip as evidence.", 422
            )
        if value < 0 or value > MAX_VALUE:
            raise GameError("INVALID_VALUE", f"The value must be between 0 and {MAX_VALUE}.", 422)
        state, delta, reason = decide(contract, milestone, value, kind)
        if note.strip():
            reason = f"{reason} Note: {note.strip()[:200]}"
        evidence_id = None
        if evidence is not None:
            evidence_id = new_id("evd")
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            (self.evidence_dir / f"{evidence_id}{evidence.suffix()}").write_bytes(evidence.content)
        now = self.clock()
        event = ScoreEvent(
            event_id=new_id("sev"),
            seq=len(self.score_events()) + 1,
            participant_id=player_id,
            group_id=group.id,
            milestone=milestone,
            evidence_id=evidence_id,
            evidence_kind=kind,
            evidence_sha256=digest,
            claimed_value=value,
            rubric_version=group.rubric_version,
            at=now,
            delta=delta,
            state=state,
            reason=reason,
            advisory=advisory,
        )
        stored = self.player(player_id)
        with self.db.transaction():
            self._scores.insert(event, record_id=event.event_id, objective_id=group.id)
            self._save_player(stored, stored.record.model_copy(update={"last_checkin_at": now}))
            self._score_event(EventType.SCORE_RECORDED, group, event)
        return event

    def _score_event(self, type_: EventType, group: Group, event: ScoreEvent) -> None:
        self._event(
            type_,
            group,
            event.event_id,
            event.model_dump(mode="json"),
            refs={"player": event.participant_id}
            | ({"supersedes": event.supersedes} if event.supersedes else {}),
        )

    def evidence_path(self, evidence_id: str) -> Path | None:
        """Find a stored evidence file.

        Args:
            evidence_id: The evidence id.

        Returns:
            Its path, or None if there is none.
        """
        if not evidence_id.startswith("evd_") or "/" in evidence_id:
            return None
        found = sorted(self.evidence_dir.glob(f"{evidence_id}.*"))
        return found[0] if found else None

    def replay_scores(self) -> list[ScoreEvent]:
        """Rebuild the current group's score events from the ledger alone.

        Returns:
            The events, in order, as the ledger recorded them.
        """
        types = [EventType.SCORE_RECORDED, EventType.SCORE_DISPUTED, EventType.SCORE_REVIEWED]
        events = self.ledger.events(objective_id=self.group().id, types=types)
        return [ScoreEvent.model_validate(e.payload) for e in events]

    # Disputes, the result and the prize

    def _original(self, event_id: str) -> ScoreEvent:
        events = self.score_events()
        found = next((e for e in events if e.event_id == event_id), None)
        if found is not None and found.supersedes is not None:
            found = next((e for e in events if e.event_id == found.supersedes), None)
        if found is None or found.supersedes is not None:
            raise GameError("UNKNOWN_EVENT", "No such check-in in this group.", 404)
        return found

    def checkins(self) -> list[tuple[ScoreEvent, list[ScoreEvent]]]:
        """Fold the score events into one current line per check-in, with its history.

        Returns:
            Each check-in as it stands now (state, points and reason from its latest
            event), and every event about it, oldest first.
        """
        events = self.score_events()
        out: list[tuple[ScoreEvent, list[ScoreEvent]]] = []
        for original in (e for e in events if e.supersedes is None):
            history = [original] + [e for e in events if e.supersedes == original.event_id]
            latest = history[-1]
            delta = original.delta if latest.state == ScoreState.VERIFIED else 0
            current = original.model_copy(
                update={"state": latest.state, "delta": delta, "reason": latest.reason}
            )
            out.append((current, history))
        return out

    def _supersede(
        self, original: ScoreEvent, state: ScoreState, delta: int, reason: str, type_: EventType
    ) -> ScoreEvent:
        group = self.group()
        event = original.model_copy(
            update={
                "event_id": new_id("sev"),
                "seq": len(self.score_events()) + 1,
                "at": self.clock(),
                "delta": delta,
                "state": state,
                "reason": reason,
                "supersedes": original.event_id,
            }
        )
        with self.db.transaction():
            self._scores.insert(event, record_id=event.event_id, objective_id=group.id)
            self._score_event(type_, group, event)
        return event

    def dispute(self, player_id: str, event_id: str, reason: str) -> ScoreEvent:
        """Dispute a verified check-in inside the window; its points are withheld.

        Args:
            player_id: The player disputing, a member of the group.
            event_id: The check-in.
            reason: Why, in the player's words.

        Returns:
            The dispute event.

        Raises:
            GameError: If the window is closed or the check-in is not verified.
        """
        self.tick()
        group = self.group()
        if group.status != GroupStatus.DISPUTE_WINDOW or (
            group.dispute_window_ends_at is not None
            and self.clock() >= group.dispute_window_ends_at
        ):
            raise GameError("DISPUTE_WINDOW_CLOSED", "Disputes are taken only in the window.")
        who = self.player(player_id).record
        original = self._original(event_id)
        if effective_states(self.score_events()).get(original.event_id) != ScoreState.VERIFIED:
            raise GameError("NOT_DISPUTABLE", "Only a verified check-in can be disputed.")
        return self._supersede(
            original,
            ScoreState.DISPUTED,
            -original.delta,
            f"Disputed by {who.name}: {reason.strip()[:200] or 'no reason given'}",
            EventType.SCORE_DISPUTED,
        )

    def review(self, event_id: str, *, reinstate: bool) -> ScoreEvent:
        """Resolve a disputed check-in: reinstate its points or uphold the dispute.

        Args:
            event_id: The disputed check-in.
            reinstate: True to restore its points, False to reject it.

        Returns:
            The review event.

        Raises:
            GameError: If the result is final or the check-in is not disputed.
        """
        self.tick()
        self._require(GroupStatus.DISPUTE_WINDOW)
        original = self._original(event_id)
        if effective_states(self.score_events()).get(original.event_id) != ScoreState.DISPUTED:
            raise GameError("NOT_DISPUTED", "That check-in is not disputed.")
        if reinstate:
            return self._supersede(
                original,
                ScoreState.VERIFIED,
                original.delta,
                "Reviewed: the check-in stands.",
                EventType.SCORE_REVIEWED,
            )
        return self._supersede(
            original,
            ScoreState.REJECTED,
            0,
            "Reviewed: the dispute is upheld.",
            EventType.SCORE_REVIEWED,
        )

    def finalize(self) -> Group:
        """Name the winner by the published tie-break once the dispute window has closed.

        With no verified progress at all, there is no winner: the group cancels and refunds.

        Returns:
            The finalised group, its result naming the winner.

        Raises:
            GameError: If the window is still open, or the result is already final.
        """
        self.tick()
        stored = self._current()
        group = stored.record
        if group.status == GroupStatus.FINALIZED:
            return group
        stored = self._require(GroupStatus.DISPUTE_WINDOW)
        if group.dispute_window_ends_at is not None and self.clock() < group.dispute_window_ends_at:
            raise GameError("DISPUTE_WINDOW_OPEN", "The dispute window is still open.")
        board = standings(self.players(), self.score_events())
        if not board or board[0].score == 0:
            self._cancel(
                stored, "No verified progress: there is no winner; every entry is refunded."
            )
            return self.group()
        winner = board[0]
        tie = len(board) > 1 and board[1].score == winner.score
        result = Result(winner_id=winner.player_id, standings=tuple(board), tie_break_applied=tie)
        with self.db.transaction():
            self._save_group(
                stored, group.model_copy(update={"status": GroupStatus.FINALIZED, "result": result})
            )
            self._event(
                EventType.GROUP_FINALIZED,
                group,
                group.id,
                {
                    "winner_id": winner.player_id,
                    "winner_name": winner.name,
                    "score": winner.score,
                    "tie_break_applied": tie,
                    "tie_break": self.rubric.tie_break,
                    "standings": [s.model_dump(mode="json") for s in board],
                },
            )
        return self.group()

    def purchase_progress(self, purchase: PrizePurchase) -> None:
        """Show the purchase's step on the result, without a ledger event.

        Args:
            purchase: The purchase as it stands.
        """
        stored = self._current()
        result = stored.record.result
        if result is None:
            return
        self._save_group(
            stored,
            stored.record.model_copy(
                update={"result": result.model_copy(update={"purchase": purchase})}
            ),
        )

    def record_purchase(self, purchase: PrizePurchase) -> Group:
        """Record the purchase's outcome: on an order id, the group is fulfilled.

        Args:
            purchase: The finished purchase.

        Returns:
            The group.
        """
        stored = self._current()
        group = stored.record
        result = group.result
        if result is None:
            return group
        winner = next((s for s in result.standings if s.player_id == result.winner_id), None)
        update: dict[str, object] = {"result": result.model_copy(update={"purchase": purchase})}
        pool = self.pool()
        with self.db.transaction():
            if purchase.status == "PURCHASED":
                update["status"] = GroupStatus.FULFILLED
                stored = self._save_group(stored, group.model_copy(update=update))
                charged = purchase.final_amount or Decimal(0)
                self._event(
                    EventType.PRIZE_PURCHASED,
                    group,
                    purchase.order_id or "order",
                    {
                        "order_id": purchase.order_id,
                        "checkout_id": purchase.checkout_id,
                        "final_amount": money(charged),
                        "ceiling": money(pool.ceiling),
                        "winner_id": result.winner_id,
                        "winner_name": winner.name if winner else None,
                        "backend": purchase.backend,
                        "stand_in": STAND_IN,
                    },
                    refs={"intent": purchase.intent_id} if purchase.intent_id else None,
                )
                self._event(
                    EventType.GROUP_FULFILLED,
                    group,
                    group.id,
                    {
                        "pool_gross": money(pool.gross),
                        "prize": money(charged),
                        "surplus_refunded_pro_rata": money(pool.gross - charged),
                    },
                )
            else:
                self._save_group(stored, group.model_copy(update=update))
                self._event(
                    EventType.PRIZE_PURCHASE_FAILED,
                    group,
                    group.id,
                    {"error": purchase.error, "step": purchase.step, "backend": purchase.backend},
                )
        return self.group()

    # Time

    def tick(self, now: datetime | None = None) -> Group:
        """Apply every deadline that has passed.

        Args:
            now: The time to apply; the clock's when None.

        Returns:
            The group after the deadlines.
        """
        now = now or self.clock()
        stored = self._current()
        group = stored.record
        before_start = (
            GroupStatus.OPEN_FOR_JOINING,
            GroupStatus.INTAKE,
            GroupStatus.READY_FOR_ACCEPTANCE,
        )
        if group.status in before_start and now >= group.enrolment_deadline:
            if self._startable(group):
                with self.db.transaction():
                    self._start(stored, group.starts_at or now, len(self.players()))
                return self.tick(now)
            self._cancel(
                stored,
                "The group did not fill, lock its contracts and accept by the deadline; "
                "every entry is refunded.",
            )
        elif group.status == GroupStatus.ACTIVE and group.ends_at and now >= group.ends_at:
            self._freeze(stored, now)
        return self.group()

    def _freeze(self, stored: _Stored[Group], now: datetime) -> None:
        group = stored.record
        board = standings(self.players(), self.score_events())
        result = Result(winner_id=None, standings=tuple(board))
        window_ends = now + timedelta(seconds=group.terms.dispute_window_s)
        with self.db.transaction():
            stored = self._save_group(
                stored,
                group.model_copy(update={"status": GroupStatus.RESULTS_PENDING, "result": result}),
            )
            self._event(
                EventType.STANDINGS_FROZEN,
                group,
                group.id,
                {"standings": [s.model_dump(mode="json") for s in board]},
            )
            self._save_group(
                stored,
                stored.record.model_copy(
                    update={
                        "status": GroupStatus.DISPUTE_WINDOW,
                        "dispute_window_ends_at": window_ends,
                    }
                ),
            )
            self._event(
                EventType.DISPUTE_WINDOW_OPENED,
                group,
                group.id,
                {"ends_at": window_ends.isoformat()},
            )

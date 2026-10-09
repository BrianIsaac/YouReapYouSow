"""The challenge's state machine: seats, entries, contracts, acceptance, scores and finish.

One group runs at a time. Every change to a record and the ledger event that records it
commit in one transaction. Time moves the group on its own: ``tick`` is called on every
request and applies whatever deadline has passed (the enrolment deadline, the challenge's
end, the dispute window's end), so nothing depends on a background job.

The money is test USDC: an entry is a reservation on the ledger against the vault's
balance, never a transfer, and a refund is a release of that reservation.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from youreapyousow.clock import Clock
from youreapyousow.game.coach import (
    CoachDraft,
    CoachTurn,
    ContractInvalidError,
    build_contract,
    parse_number,
    template_draft,
)
from youreapyousow.game.models import (
    CENT,
    ChatTurn,
    ContractStatus,
    EntryState,
    GoalContract,
    Group,
    GroupStatus,
    GroupTerms,
    IntakeState,
    Player,
    ScoreEvent,
)
from youreapyousow.game.rubric import RUBRIC_V1, Rubric
from youreapyousow.ids import new_id
from youreapyousow.ledger.events import EventType
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.store import Database, Records

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
        """
        self.db = db
        self.ledger = ledger
        self.clock = clock
        self.terms = terms
        self.vault_balance = vault_balance
        self.vault_source = vault_source
        self.evidence_dir = evidence_dir
        self.rubric = rubric
        self.prize_quote: Decimal | None = None
        self._groups = Records(db, "game_group", Group)
        self._players = Records(db, "game_player", Player)
        self._scores = Records(db, "score_event", ScoreEvent)

    # Reading

    def _current(self) -> _Stored[Group]:
        groups = self._groups.list()
        if not groups:
            raise GameError("NO_GROUP", "No group is open.", 404)
        record, version = self._groups.require(groups[-1].id)
        return _Stored(record, version)

    def has_group(self) -> bool:
        """Say whether any group was ever opened.

        Returns:
            True when one was.
        """
        return bool(self._groups.list())

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

    def open_group(self) -> Group:
        """Open a new group on the configured terms.

        Returns:
            The group, open for joining.
        """
        now = self.clock()
        group = Group(
            id=new_id("grp"),
            terms=self.terms,
            status=GroupStatus.OPEN_FOR_JOINING,
            opened_at=now,
            enrolment_deadline=now + timedelta(seconds=self.terms.enrolment_window_s),
            rubric_version=self.rubric.version,
        )
        with self.db.transaction():
            self._groups.insert(group, record_id=group.id, objective_id=group.id)
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
                    "rubric_version": group.rubric_version,
                    "buffer_rate": str(group.terms.buffer_rate),
                },
            )
        return group

    def reset(self) -> Group:
        """Cancel the current group if unfinished, refunding its entries, and open a new one.

        Returns:
            The new group.
        """
        current = self._current()
        if current.record.status not in (GroupStatus.FULFILLED, GroupStatus.CANCELLED):
            self._cancel(current, "reset by the operator")
        return self.open_group()

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
        reserved_total = entry * (len(players) + 1)
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
            players = self.players()
            startable = (
                len(players) >= group.terms.min_players
                and all(p.entry == EntryState.RESERVED for p in players)
                and all(p.intake == IntakeState.LOCKED for p in players)
                and all(p.accepted_at is not None for p in players)
            )
            if startable:
                self._start(stored_group, now, len(players))
        return self.group()

    def _start(self, stored: _Stored[Group], now: datetime, players: int) -> None:
        group = stored.record
        ends_at = now + timedelta(seconds=group.terms.duration_days * group.terms.seconds_per_day)
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
            self._cancel(
                stored,
                "The group did not fill, lock its contracts and accept by the deadline; "
                "every entry is refunded.",
            )
        return self.group()

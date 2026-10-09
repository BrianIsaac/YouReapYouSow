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

from pydantic import BaseModel, ConfigDict, JsonValue

from youreapyousow.clock import Clock
from youreapyousow.game.models import (
    CENT,
    EntryState,
    Group,
    GroupStatus,
    GroupTerms,
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

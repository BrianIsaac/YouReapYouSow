"""The mock Reap: state and deterministic enforcement behind the documented shapes.

Behaviour follows Reap's documentation where it speaks, and each place where the mock
has to choose is marked "(mock choice)" so it can be checked against Reap:

* Policies: the five documented types at PROJECT, USER and CARD scope. A transaction is
  declined if any active policy blocks it, and the decline names that policy. Running
  totals use UTC calendar windows (never rolling). Refunds would not restore usage.
* Declines: frozen or blocked cards, the platform-blocked MCC 5542, then policies,
  then (in External authorisation mode) the real-time authoriser, which must answer
  within 1.6 s or Reap fails closed.
* Insufficient funds on a simulated authorisation returns 400, as the simulator's
  OpenAPI description says, not a declined transaction.
* Funding follows the Program-Funded model: simulated fiat deposits land in the
  project's master collateral account and every card draws on it (mock choice for how
  balances are presented per account).
* Webhooks are signed exactly as Reap signs them and delivered through a pluggable
  delivery function, once and in order (Reap guarantees neither; mock choice).
* Omitted simulated merchant fields are filled deterministically rather than at random
  (mock choice), so tests are reproducible.
"""

import asyncio
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from youreapyousow.clock import Clock, utc_now
from youreapyousow.reap import webhooks
from youreapyousow.reap.models import (
    Account,
    Activity,
    ApplicationStatus,
    AuthorisedAmounts,
    AuthorizationCountLimitConfig,
    AuthorizationCountLimitPolicy,
    AuthorizationMerchant,
    Balance,
    BalanceAssets,
    BalanceLiabilities,
    Card,
    CardAuthorizationRequest,
    CardDebt,
    CardStatus,
    CardTransaction,
    ChannelRestrictionPolicy,
    ClearedAmounts,
    ClearedCardTransaction,
    CountryMatch,
    CreateAccountRequest,
    CreateCardRequest,
    CreateUserRequest,
    CreateWebhookRequest,
    DeclineCode,
    DeclinedCardTransaction,
    DeclinedPolicy,
    DeclineReason,
    EffectivePolicies,
    EffectivePolicy,
    ExternalAuthDecision,
    Fees,
    FiatDeposit,
    LimitFilter,
    MccMatch,
    MerchantMatch,
    MerchantRestrictionPolicy,
    Page,
    PendingCardTransaction,
    Period,
    Policy,
    PolicyCreate,
    PolicyLimitUsage,
    PolicyType,
    ScopeType,
    SimulateAuthorizationRequest,
    SimulateClearingRequest,
    SimulatedMerchant,
    SimulateFiatDepositRequest,
    SpendLimitConfig,
    SpendLimitPolicy,
    TransactionAmountLimitPolicy,
    TransactionEvent,
    TransactionMerchant,
    User,
    UserApplication,
    VoidCardTransaction,
    WebhookEndpoint,
    WebhookEvent,
    WebhookEventType,
)

EXTERNAL_DEADLINE_SECONDS = 1.6
PLATFORM_BLOCKED_MCCS = frozenset({"5542"})
CURRENCY = "USD"

_POLICY = TypeAdapter[Policy](Policy)

type EventKind = Literal["AUTHORIZATION", "CLEARING", "REVERSAL", "REFUND", "DECLINE"]


class AuthorizationMode(StrEnum):
    """Set by Reap at project setup and fixed after launch."""

    MANAGED = "MANAGED"
    EXTERNAL = "EXTERNAL"


class MockReapError(Exception):
    """An error the mock returns as ``{"error": {...}}`` with an HTTP status."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Create the error.

        Args:
            status: HTTP status.
            code: Reap error code.
            message: Human-readable message.
        """
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DeliveryResponse:
    """What a webhook receiver answered.

    Attributes:
        status: HTTP status.
        body: Raw response body.
    """

    status: int
    body: bytes


type WebhookDelivery = Callable[[str, dict[str, str], bytes], Awaitable[DeliveryResponse]]


@dataclass(frozen=True)
class DeliveryRecord:
    """One webhook delivery attempt, kept for inspection.

    Attributes:
        url: Where it was sent.
        event: The envelope.
        status: The receiver's HTTP status, or None if delivery raised.
        error: The error, if delivery raised.
    """

    url: str
    event: WebhookEvent
    status: int | None
    error: str | None = None


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _new_uuid() -> str:
    return str(uuid.uuid4())


def window_bounds(period: Period, now: datetime) -> tuple[datetime | None, datetime | None]:
    """Return the calendar window containing ``now``, in UTC.

    Args:
        period: The window period.
        now: The current time (aware, UTC).

    Returns:
        The window's start and its reset time; both None for a lifetime window.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    match period:
        case "LIFETIME":
            return None, None
        case "DAILY":
            return midnight, midnight + timedelta(days=1)
        case "WEEKLY":
            start = midnight - timedelta(days=midnight.weekday())
            return start, start + timedelta(days=7)
        case "MONTHLY":
            start = midnight.replace(day=1)
            nxt = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
            return start, nxt
        case "YEARLY":
            start = midnight.replace(month=1, day=1)
            return start, start.replace(year=start.year + 1)


@dataclass
class _Txn:
    """Internal transaction state; rendered to the wire shape on demand."""

    id: str
    card_id: str
    account_id: str
    user_id: str
    channel: Any
    merchant: TransactionMerchant
    status: str
    authorised: Decimal
    cleared: Decimal = Decimal(0)
    reversed: Decimal = Decimal(0)
    refunded: Decimal = Decimal(0)
    occurred_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    events: list[TransactionEvent] = field(default_factory=list[TransactionEvent])
    decline_code: str | None = None
    decline_message: str = ""
    declined_by: DeclinedPolicy | None = None

    @property
    def usage(self) -> Decimal:
        """Amount counted against running-total limits; refunds never restore it.

        Returns:
            The counted amount.
        """
        if self.status == "PENDING":
            return self.authorised - self.reversed
        if self.status == "CLEARED":
            return self.cleared
        return Decimal(0)

    def to_wire(self) -> CardTransaction:
        """Render in Reap's shape for the current status.

        Returns:
            The transaction variant for its status.
        """
        common: dict[str, Any] = {
            "id": self.id,
            "account_id": self.account_id,
            "card_id": self.card_id,
            "channel": self.channel,
            "digital_wallet": None,
            "original_currency": CURRENCY,
            "currency": CURRENCY,
            "merchant": self.merchant,
            "fees": Fees(atm=Decimal(0), fx=Decimal(0)),
            "events": list(self.events),
            "occurred_at": _iso(self.occurred_at),
            "created_at": _iso(self.occurred_at),
            "updated_at": _iso(self.updated_at),
        }
        pending = AuthorisedAmounts(
            authorized=self.authorised,
            reversed=self.reversed,
            current=self.authorised - self.reversed,
        )
        match self.status:
            case "PENDING":
                return PendingCardTransaction(
                    **common,
                    status="PENDING",
                    original_amount=pending,
                    amount=pending,
                    conversion_rate=Decimal(1),
                )
            case "CLEARED":
                cleared = ClearedAmounts(
                    cleared=self.cleared,
                    refunded=self.refunded,
                    current=self.cleared - self.refunded,
                )
                return ClearedCardTransaction(
                    **common,
                    status="CLEARED",
                    original_amount=cleared,
                    amount=cleared,
                    conversion_rate=Decimal(1),
                )
            case "DECLINED":
                return DeclinedCardTransaction(
                    **common,
                    status="DECLINED",
                    original_amount=self.authorised,
                    decline_reason=DeclineReason(
                        code=self.decline_code or DeclineCode.INTERNAL_ERROR,
                        message=self.decline_message,
                    ),
                    policy=self.declined_by,
                )
            case _:
                return VoidCardTransaction(
                    **common, status="VOID", original_amount=pending, amount=pending
                )


@dataclass
class _CardState:
    card: Card
    deleted: bool = False


def _match_blocks(match: MerchantMatch, merchant: TransactionMerchant) -> bool:
    if isinstance(match, MccMatch):
        return merchant.mcc_code in match.codes
    assert isinstance(match, CountryMatch)
    return (merchant.country or "") in match.countries


def _filter_counts(flt: LimitFilter | None, txn: _Txn) -> bool:
    if flt is None:
        return True
    if flt.channels is not None and txn.channel not in flt.channels:
        return False
    return flt.merchant is None or _match_blocks(flt.merchant, txn.merchant)


class MockReapEngine:
    """In-memory Reap project with deterministic policy enforcement."""

    def __init__(
        self,
        *,
        clock: Clock = utc_now,
        authorization_mode: AuthorizationMode = AuthorizationMode.MANAGED,
        delivery: WebhookDelivery | None = None,
    ) -> None:
        """Create an empty project.

        Args:
            clock: Time source for timestamps and calendar windows.
            authorization_mode: Managed, or External with a real-time authoriser.
            delivery: How webhooks reach receivers; without one they are only logged.
        """
        self._clock = clock
        self.authorization_mode = authorization_mode
        self.delivery = delivery
        self.deliveries: list[DeliveryRecord] = []
        self._users: dict[str, User] = {}
        self._accounts: dict[str, Account] = {}
        self._cards: dict[str, _CardState] = {}
        self._policies: dict[str, Policy] = {}
        self._txns: dict[str, _Txn] = {}
        self._deposits: list[FiatDeposit] = []
        self._webhooks: dict[str, WebhookEndpoint] = {}
        now = _iso(self._clock())
        self.master_account = Account(
            id=_new_uuid(),
            status="ACTIVE",
            owner_type="PROJECT",
            owner_id=None,
            chain_addresses=[],
            created_at=now,
            updated_at=now,
        )

    # Users

    def create_user(self, req: CreateUserRequest) -> User:
        """Create a user whose KYC has not started.

        Args:
            req: The request.

        Returns:
            The user.
        """
        now = _iso(self._clock())
        user = User(
            id=_new_uuid(),
            external_id=req.external_id,
            email=req.email,
            phone_number=req.phone_number,
            first_name=req.first_name,
            last_name=req.last_name,
            country=None,
            date_of_birth=None,
            company_id=None,
            application=UserApplication(
                status=ApplicationStatus.NOT_STARTED, rejection_reason=None, documents=None
            ),
            created_at=now,
            updated_at=now,
        )
        self._users[user.id] = user
        return user

    def get_user(self, user_id: str) -> User:
        """Fetch a user.

        Args:
            user_id: The user.

        Returns:
            The user.

        Raises:
            MockReapError: 404 if unknown.
        """
        if user_id not in self._users:
            raise MockReapError(404, "USER_NOT_FOUND", "User not found")
        return self._users[user_id]

    async def simulate_application(self, user_id: str, status: str) -> None:
        """Set a user's KYC status, as the sandbox simulator does.

        Args:
            user_id: The user.
            status: The new status.
        """
        user = self.get_user(user_id)
        updated = user.model_copy(
            update={
                "application": user.application.model_copy(
                    update={"status": ApplicationStatus(status)}
                ),
                "updated_at": _iso(self._clock()),
            }
        )
        self._users[user_id] = updated
        await self._emit(WebhookEventType.USER_APPLICATION_STATUS_UPDATED, updated.to_wire())

    # Accounts and funding

    def create_account(self, req: CreateAccountRequest) -> Account:
        """Open an account for a user.

        Args:
            req: The request.

        Returns:
            The account.
        """
        self.get_user(req.owner_id)
        now = _iso(self._clock())
        account = Account(
            id=_new_uuid(),
            status="ACTIVE",
            owner_type="USER",
            owner_id=req.owner_id,
            chain_addresses=[],
            created_at=now,
            updated_at=now,
        )
        self._accounts[account.id] = account
        return account

    def _deposited(self) -> Decimal:
        return sum((d.amount for d in self._deposits), Decimal(0))

    def _debt(self, account_id: str | None = None) -> tuple[Decimal, Decimal]:
        pending = cleared = Decimal(0)
        for txn in self._txns.values():
            if account_id is not None and txn.account_id != account_id:
                continue
            if txn.status == "PENDING":
                pending += txn.authorised - txn.reversed
            elif txn.status == "CLEARED":
                cleared += txn.cleared - txn.refunded
        return pending, cleared

    def available(self) -> Decimal:
        """Return the master balance not yet spent or held by any card.

        Returns:
            Available funds in USD.
        """
        pending, cleared = self._debt()
        return self._deposited() - pending - cleared

    def get_balance(self, account_id: str) -> Balance:
        """Read an account's balance under the Program-Funded model.

        Args:
            account_id: The account.

        Returns:
            Its card debt, with the project's shared available balance.

        Raises:
            MockReapError: 404 if unknown.
        """
        if account_id not in self._accounts and account_id != self.master_account.id:
            raise MockReapError(404, "ACCOUNT_NOT_FOUND", "Account not found")
        master = account_id == self.master_account.id
        pending, cleared = self._debt(None if master else account_id)
        deposited = self._deposited() if master else Decimal(0)
        return Balance(
            currency=CURRENCY,
            total_asset_value=deposited,
            total_liabilities=pending + cleared,
            available_balance=self.available(),
            assets=BalanceAssets(crypto=None, fiat=deposited, virtual=None),
            liabilities=BalanceLiabilities(
                card_debt=CardDebt(pending=pending, cleared=cleared, total=pending + cleared),
                deposit_fees=None,
            ),
        )

    async def simulate_fiat_deposit(self, req: SimulateFiatDepositRequest) -> FiatDeposit:
        """Credit the project's master collateral account.

        Args:
            req: The request.

        Returns:
            The settled deposit.

        Raises:
            MockReapError: 400 for a non-positive amount or a non-USD currency.
        """
        if req.amount <= 0 or req.currency != CURRENCY:
            raise MockReapError(400, "VALIDATION_ERROR", "amount must be positive, in USD")
        now = _iso(self._clock())
        deposit = FiatDeposit(
            id=_new_uuid(),
            account_id=self.master_account.id,
            transaction_id=_new_uuid(),
            status="SETTLED",
            amount=req.amount,
            currency=req.currency,
            original_amount=req.amount,
            original_currency=req.currency,
            sender_name=req.sender_name,
            reference=req.reference,
            occurred_at=now,
            created_at=now,
            updated_at=now,
        )
        self._deposits.append(deposit)
        await self._emit(WebhookEventType.FIAT_DEPOSIT_CREATED, deposit.to_wire())
        return deposit

    # Cards

    def create_card(self, req: CreateCardRequest) -> Card:
        """Issue a card to a KYC-approved user.

        Args:
            req: The request.

        Returns:
            The active card.

        Raises:
            MockReapError: For an unapproved user, an unknown account, or an External
                project without its authorisation endpoint.
        """
        user = self.get_user(req.user_id)
        if user.application.status != ApplicationStatus.APPROVED:
            raise MockReapError(400, "USER_APPLICATION_NOT_APPROVED", "KYC is not approved")
        account = self._accounts.get(req.account_id)
        if account is None or account.owner_id != req.user_id:
            raise MockReapError(404, "ACCOUNT_NOT_FOUND", "Account not found")
        if self.authorization_mode == AuthorizationMode.EXTERNAL and not self._request_endpoint():
            raise MockReapError(
                409,
                "AUTHORIZATION_ENDPOINT_REQUIRED",
                "Register a REQUEST webhook before issuing cards",
            )
        now = _iso(self._clock())
        card = Card(
            id=_new_uuid(),
            account_id=req.account_id,
            user_id=req.user_id,
            type=req.type,
            card_design_id=req.card_design_id or "default",
            status=CardStatus.ACTIVE,
            block_reason=None,
            block_liftable=False,
            frozen=False,
            cardholder_name=" ".join(filter(None, [user.first_name, user.last_name])) or "Mock",
            last4=f"{secrets.randbelow(10_000):04d}",
            three_ds_challenge_method=req.three_ds_challenge_method or "SMS",
            physical_card_status=None,
            created_at=now,
            updated_at=now,
        )
        self._cards[card.id] = _CardState(card)
        return card

    def get_card(self, card_id: str) -> Card:
        """Fetch a live card.

        Args:
            card_id: The card.

        Returns:
            The card.

        Raises:
            MockReapError: 404 if unknown or deleted.
        """
        state = self._cards.get(card_id)
        if state is None or state.deleted:
            raise MockReapError(404, "CARD_NOT_FOUND", "Card not found")
        return state.card

    async def set_frozen(self, card_id: str, *, frozen: bool) -> Card:
        """Freeze or unfreeze a card.

        Args:
            card_id: The card.
            frozen: True to freeze.

        Returns:
            The updated card.

        Raises:
            MockReapError: 400 when the card is not in a state that allows it.
        """
        card = self.get_card(card_id)
        wanted_from = CardStatus.ACTIVE if frozen else CardStatus.FROZEN
        if card.status != wanted_from:
            raise MockReapError(
                400, "CARD_OPERATION_NOT_ALLOWED", "Card operation not allowed in current status"
            )
        updated = card.model_copy(
            update={
                "status": CardStatus.FROZEN if frozen else CardStatus.ACTIVE,
                "frozen": frozen,
                "updated_at": _iso(self._clock()),
            }
        )
        self._cards[card_id] = _CardState(updated)
        await self._emit(WebhookEventType.CARD_STATUS_UPDATED, updated.to_wire())
        return updated

    def delete_card(self, card_id: str) -> None:
        """Delete a card irreversibly.

        Args:
            card_id: The card.
        """
        self.get_card(card_id)
        self._cards[card_id].deleted = True

    # Policies

    def create_policy(self, req: PolicyCreate) -> Policy:
        """Store an active policy.

        Args:
            req: The request.

        Returns:
            The stored policy, with its currency for amount limits.

        Raises:
            MockReapError: 400 when a USER or CARD scope lacks an id.
        """
        if req.scope.type != ScopeType.PROJECT and not req.scope.id:
            raise MockReapError(400, "VALIDATION_ERROR", "scope id is required")
        now = _iso(self._clock())
        data = req.to_wire() | {"id": _new_uuid(), "status": "ACTIVE"}
        data |= {"createdAt": now, "updatedAt": now}
        if req.type in ("TRANSACTION_AMOUNT_LIMIT", "SPEND_LIMIT"):
            data["currency"] = CURRENCY
        policy = _POLICY.validate_python(data)
        self._policies[policy.id] = policy
        return policy

    def disable_policy(self, policy_id: str) -> Policy:
        """Disable a policy.

        Args:
            policy_id: The policy.

        Returns:
            The disabled policy.

        Raises:
            MockReapError: 404 if unknown.
        """
        policy = self._policies.get(policy_id)
        if policy is None:
            raise MockReapError(404, "POLICY_NOT_FOUND", "Policy not found")
        disabled = policy.model_copy(
            update={"status": "DISABLED", "updated_at": _iso(self._clock())}
        )
        self._policies[policy_id] = disabled
        return disabled

    def _applicable(self, card: Card) -> list[Policy]:
        order = {ScopeType.PROJECT: 0, ScopeType.USER: 1, ScopeType.CARD: 2}
        applicable = [
            p
            for p in self._policies.values()
            if p.status == "ACTIVE"
            and (
                p.scope.type == ScopeType.PROJECT
                or (p.scope.type == ScopeType.USER and p.scope.id == card.user_id)
                or (p.scope.type == ScopeType.CARD and p.scope.id == card.id)
            )
        ]
        return sorted(applicable, key=lambda p: order[p.scope.type])

    def _in_scope(self, policy: Policy, txn: _Txn) -> bool:
        match policy.scope.type:
            case ScopeType.PROJECT:
                return True
            case ScopeType.USER:
                return txn.user_id == policy.scope.id
            case ScopeType.CARD:
                return txn.card_id == policy.scope.id

    def _window_txns(
        self, policy: Policy, config: SpendLimitConfig | AuthorizationCountLimitConfig
    ) -> list[_Txn]:
        start, _ = window_bounds(config.window.period, self._clock())
        return [
            t
            for t in self._txns.values()
            if t.status in ("PENDING", "CLEARED")
            and self._in_scope(policy, t)
            and (start is None or t.occurred_at >= start)
            and _filter_counts(config.filter, t)
        ]

    def _usage(self, policy: Policy) -> PolicyLimitUsage | None:
        if isinstance(policy, SpendLimitPolicy):
            config = policy.config
            used = sum((t.usage for t in self._window_txns(policy, config)), Decimal(0))
            limit = config.max_amount
        elif isinstance(policy, AuthorizationCountLimitPolicy):
            config = policy.config
            used = Decimal(len(self._window_txns(policy, config)))
            limit = Decimal(config.max_count)
        else:
            return None
        _, resets = window_bounds(config.window.period, self._clock())
        return PolicyLimitUsage(
            limit=limit,
            used=used,
            remaining=max(limit - used, Decimal(0)),
            resets_at=_iso(resets) if resets else None,
        )

    def effective_policies(self, scope_type: ScopeType, scope_id: str | None) -> EffectivePolicies:
        """List the policies in force for a scope, with usage for running totals.

        Args:
            scope_type: PROJECT, USER or CARD.
            scope_id: The user or card id.

        Returns:
            The effective policies.
        """
        if scope_type == ScopeType.CARD:
            policies = self._applicable(self.get_card(scope_id or ""))
        else:
            policies = [
                p
                for p in self._policies.values()
                if p.status == "ACTIVE"
                and (
                    p.scope.type == ScopeType.PROJECT
                    or (scope_type == ScopeType.USER and p.scope.id == scope_id)
                )
            ]
        return EffectivePolicies(
            items=[EffectivePolicy(policy=p, usage=self._usage(p)) for p in policies]
        )

    def _policy_decline(self, card: Card, txn: _Txn) -> tuple[str, Policy] | None:
        for policy in self._applicable(card):
            match policy:
                case MerchantRestrictionPolicy():
                    if _match_blocks(policy.config.match, txn.merchant):
                        return DeclineCode.MERCHANT_NOT_ALLOWED, policy
                case ChannelRestrictionPolicy():
                    if txn.channel in policy.config.channels:
                        return DeclineCode.CHANNEL_NOT_ALLOWED, policy
                case TransactionAmountLimitPolicy():
                    if txn.authorised > policy.config.max_amount:
                        return DeclineCode.TRANSACTION_AMOUNT_LIMIT_EXCEEDED, policy
                case SpendLimitPolicy():
                    usage = self._usage(policy)
                    if usage is not None and usage.used + txn.authorised > usage.limit:
                        return DeclineCode.SPEND_LIMIT_EXCEEDED, policy
                case AuthorizationCountLimitPolicy():
                    usage = self._usage(policy)
                    if usage is not None and usage.used + 1 > usage.limit:
                        return DeclineCode.AUTHORIZATION_COUNT_LIMIT_EXCEEDED, policy
        return None

    # Transactions

    def _merchant(self, given: SimulatedMerchant | None) -> TransactionMerchant:
        given = given or SimulatedMerchant()
        return TransactionMerchant(
            id=_new_uuid(),
            card_acceptor_id=f"MOCK{secrets.randbelow(10**8):08d}",
            name=given.name or "Mock Merchant",
            city=given.city,
            post_code=given.post_code,
            state=given.state,
            country=given.country or "US",
            mcc_code=given.mcc_code or "5999",
            mcc_category=given.mcc_category or "Miscellaneous Retail",
        )

    def _event(self, kind: EventKind, amount: Decimal) -> TransactionEvent:
        now = _iso(self._clock())
        return TransactionEvent(
            id=_new_uuid(),
            type=kind,
            original_amount=amount,
            original_currency=CURRENCY,
            amount=amount,
            fees=Fees(atm=Decimal(0), fx=Decimal(0)),
            occurred_at=now,
            created_at=now,
        )

    async def _external_decision(self, txn: _Txn) -> tuple[bool, str]:
        endpoint = self._request_endpoint()
        if endpoint is None or self.delivery is None:
            return False, "no authorisation endpoint reachable; Reap fails closed"
        request = CardAuthorizationRequest(
            transaction_id=txn.id,
            event_id=_new_uuid(),
            account_id=txn.account_id,
            card_id=txn.card_id,
            channel=txn.channel,
            digital_wallet=None,
            currency=CURRENCY,
            amount=txn.authorised,
            original_currency=CURRENCY,
            original_amount=txn.authorised,
            fees=Fees(atm=Decimal(0), fx=Decimal(0)),
            merchant=AuthorizationMerchant(
                card_acceptor_id=txn.merchant.card_acceptor_id,
                name=txn.merchant.name,
                city=txn.merchant.city,
                post_code=txn.merchant.post_code,
                state=txn.merchant.state,
                country=txn.merchant.country,
                mcc_code=txn.merchant.mcc_code,
                mcc_category=txn.merchant.mcc_category,
            ),
            occurred_at=_iso(txn.occurred_at),
        )
        event = WebhookEvent(
            id=_new_uuid(),
            type=WebhookEventType.CARD_AUTHORIZATION_REQUEST,
            data=request.to_wire(),
        )
        try:
            async with asyncio.timeout(EXTERNAL_DEADLINE_SECONDS):
                response = await self._send(endpoint, event)
        except TimeoutError:
            return False, "authorisation endpoint missed the 1.6 s deadline; failed closed"
        except Exception as error:  # fail closed on any delivery failure
            return False, f"authorisation endpoint unreachable ({type(error).__name__})"
        if not 200 <= response.status < 300:
            return False, f"authorisation endpoint answered {response.status}; failed closed"
        try:
            decision = ExternalAuthDecision.model_validate_json(response.body)
        except ValidationError:
            return False, "authorisation endpoint answered an invalid body; failed closed"
        if decision.decision == "APPROVE":
            return True, "approved by the authorisation endpoint"
        return False, f"declined by the authorisation endpoint ({decision.reason})"

    async def simulate_authorization(self, req: SimulateAuthorizationRequest) -> CardTransaction:
        """Simulate a merchant authorisation on a card.

        Args:
            req: The request.

        Returns:
            A PENDING transaction when approved, or a DECLINED one naming the reason.

        Raises:
            MockReapError: 404 for an unknown card, 400 for insufficient funds or an
                incremental authorisation (unsupported by the mock).
        """
        card = self.get_card(req.card_id)
        if req.transaction_id is not None:
            raise MockReapError(
                400, "SIMULATION_INVALID_STATE", "incremental authorisation is not mocked"
            )
        if req.amount <= 0:
            raise MockReapError(400, "VALIDATION_ERROR", "amount must be positive")
        now = self._clock()
        txn = _Txn(
            id=_new_uuid(),
            card_id=card.id,
            account_id=card.account_id,
            user_id=card.user_id,
            channel=req.channel or "ECOMMERCE",
            merchant=self._merchant(req.merchant),
            status="PENDING",
            authorised=req.amount,
            occurred_at=now,
            updated_at=now,
        )
        decline = await self._decline_reason(card, txn)
        if decline is None and self.available() < req.amount:
            raise MockReapError(400, "SIMULATION_INVALID_STATE", "Insufficient funds")
        if decline is None:
            txn.events.append(self._event("AUTHORIZATION", req.amount))
        else:
            txn.status = "DECLINED"
            txn.decline_code, txn.decline_message, txn.declined_by = decline
            txn.events.append(self._event("DECLINE", req.amount))
        self._txns[txn.id] = txn
        wire = txn.to_wire()
        await self._emit(WebhookEventType.CARD_TRANSACTION_CREATED, wire.to_wire())
        return wire

    async def _decline_reason(
        self, card: Card, txn: _Txn
    ) -> tuple[str, str, DeclinedPolicy | None] | None:
        if card.status == CardStatus.FROZEN:
            return DeclineCode.CARD_FROZEN, "Card is frozen", None
        if card.status != CardStatus.ACTIVE:
            return DeclineCode.CARD_BLOCKED, f"Card is {card.status.value}", None
        if txn.merchant.mcc_code in PLATFORM_BLOCKED_MCCS:
            return (
                DeclineCode.MERCHANT_CATEGORY_CODE_NOT_ALLOWED,
                "MCC blocked by the platform",
                None,
            )
        blocked = self._policy_decline(card, txn)
        if blocked is not None:
            code, policy = blocked
            return (
                code,
                f"Declined by policy {policy.name}",
                DeclinedPolicy(
                    id=policy.id,
                    type=PolicyType(policy.type),
                    scope_type=policy.scope.type,
                    name=policy.name,
                ),
            )
        if self.authorization_mode == AuthorizationMode.EXTERNAL:
            approved, message = await self._external_decision(txn)
            if not approved:
                return DeclineCode.TRANSACTION_NOT_ALLOWED, message, None
        return None

    def _pending_txn(self, card_id: str, transaction_id: str | None) -> _Txn:
        self.get_card(card_id)
        txn = self._txns.get(transaction_id or "")
        if txn is None or txn.card_id != card_id:
            raise MockReapError(404, "CARD_TRANSACTION_NOT_FOUND", "Card transaction not found")
        if txn.status != "PENDING":
            raise MockReapError(400, "SIMULATION_INVALID_STATE", f"transaction is {txn.status}")
        return txn

    async def simulate_clearing(self, req: SimulateClearingRequest) -> CardTransaction:
        """Clear a pending authorisation at the settled amount.

        Clearing below the authorisation releases the rest; over-clearing is accepted
        without a policy re-check, as Reap documents.

        Args:
            req: The request; ``transaction_id`` is required by the mock.

        Returns:
            The cleared transaction.
        """
        txn = self._pending_txn(req.card_id, req.transaction_id)
        txn.status = "CLEARED"
        txn.cleared = req.amount
        txn.updated_at = self._clock()
        txn.events.append(self._event("CLEARING", req.amount))
        wire = txn.to_wire()
        await self._emit(WebhookEventType.CARD_TRANSACTION_UPDATED, wire.to_wire())
        return wire

    def get_transaction(self, transaction_id: str) -> CardTransaction:
        """Fetch a transaction.

        Args:
            transaction_id: The transaction.

        Returns:
            The transaction.

        Raises:
            MockReapError: 404 if unknown.
        """
        txn = self._txns.get(transaction_id)
        if txn is None:
            raise MockReapError(404, "CARD_TRANSACTION_NOT_FOUND", "Card transaction not found")
        return txn.to_wire()

    def activities(
        self,
        *,
        card_id: str | None,
        type_: str | None,
        cursor: str | None,
        limit: int,
    ) -> Page[Activity]:
        """List card transactions and fiat deposits, newest first.

        Args:
            card_id: Only this card's transactions, when given.
            type_: Only this activity type, when given.
            cursor: Opaque cursor from a previous page.
            limit: Page size, 1 to 100.

        Returns:
            One page of activities.
        """
        items: list[Activity] = [
            Activity(
                type="CARD_TRANSACTION",
                id=t.id,
                occurred_at=_iso(t.occurred_at),
                data=t.to_wire().to_wire(),
            )
            for t in self._txns.values()
            if card_id is None or t.card_id == card_id
        ]
        if card_id is None:
            items += [
                Activity(type="FIAT_DEPOSIT", id=d.id, occurred_at=d.occurred_at, data=d.to_wire())
                for d in self._deposits
            ]
        if type_ is not None:
            items = [a for a in items if a.type == type_]
        items.sort(key=lambda a: a.occurred_at, reverse=True)
        start = int(cursor or 0)
        page = items[start : start + limit]
        more = start + limit < len(items)
        return Page[Activity](items=page, next_cursor=str(start + limit) if more else None)

    # Webhooks

    def _request_endpoint(self) -> WebhookEndpoint | None:
        return next(
            (w for w in self._webhooks.values() if w.mode == "REQUEST" and w.status == "ACTIVE"),
            None,
        )

    def create_webhook(self, req: CreateWebhookRequest) -> WebhookEndpoint:
        """Register a webhook endpoint and return its signing secret once.

        Args:
            req: The request.

        Returns:
            The endpoint, including ``signing_secret``.

        Raises:
            MockReapError: 403 for a REQUEST endpoint outside External mode, 409 for a
                second REQUEST endpoint or a sixth NOTIFICATION endpoint.
        """
        mode = req.mode or "NOTIFICATION"
        if mode == "REQUEST":
            if self.authorization_mode != AuthorizationMode.EXTERNAL:
                raise MockReapError(
                    403, "FORBIDDEN", "REQUEST endpoints need External authorization mode"
                )
            if self._request_endpoint() is not None:
                raise MockReapError(409, "CONFLICT", "Only one REQUEST endpoint is allowed")
        elif sum(1 for w in self._webhooks.values() if w.mode == "NOTIFICATION") >= 5:
            raise MockReapError(409, "CONFLICT", "At most five NOTIFICATION endpoints")
        now = _iso(self._clock())
        endpoint = WebhookEndpoint(
            id=_new_uuid(),
            name=req.name,
            url=req.url,
            status="ACTIVE",
            mode=mode,
            last_used_at=None,
            created_at=now,
            updated_at=now,
            signing_secret=f"whsec_{secrets.token_hex(24)}",
        )
        self._webhooks[endpoint.id] = endpoint
        return endpoint

    async def _send(self, endpoint: WebhookEndpoint, event: WebhookEvent) -> DeliveryResponse:
        if self.delivery is None:
            raise RuntimeError("no webhook delivery configured")
        body = json.dumps(event.to_wire(), separators=(",", ":")).encode()
        timestamp = int(self._clock().timestamp())
        headers = {
            "Content-Type": "application/json",
            webhooks.SIGNATURE_HEADER: webhooks.sign(endpoint.signing_secret, body, timestamp),
        }
        return await self.delivery(endpoint.url, headers, body)

    async def _emit(self, type_: WebhookEventType, data: dict[str, Any]) -> None:
        event = WebhookEvent(id=_new_uuid(), type=type_, data=data)
        for endpoint in list(self._webhooks.values()):
            if endpoint.mode != "NOTIFICATION" or endpoint.status != "ACTIVE":
                continue
            if self.delivery is None:
                self.deliveries.append(DeliveryRecord(endpoint.url, event, None, "no delivery"))
                continue
            try:
                response = await self._send(endpoint, event)
            except Exception as error:  # a receiver failure never breaks the API call
                self.deliveries.append(
                    DeliveryRecord(endpoint.url, event, None, f"{type(error).__name__}: {error}")
                )
                continue
            self.deliveries.append(DeliveryRecord(endpoint.url, event, response.status))

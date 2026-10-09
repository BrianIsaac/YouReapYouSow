"""Tests for the mock Reap's policy enforcement, driven through the real HTTP client."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.reap.conftest import Funded, issue_card
from youreapyousow.clock import ManualClock
from youreapyousow.reap.client import ReapError, ReapMock
from youreapyousow.reap.mock.engine import (
    AuthorizationMode,
    DeliveryResponse,
    window_bounds,
)
from youreapyousow.reap.models import (
    AuthorizationCountLimitConfig,
    AuthorizationCountLimitCreate,
    CalendarWindow,
    CardTransaction,
    ChannelRestrictionConfig,
    ChannelRestrictionCreate,
    ClearedCardTransaction,
    CountryMatch,
    CreateAccountRequest,
    CreateCardRequest,
    CreateUserRequest,
    CreateWebhookRequest,
    DeclinedCardTransaction,
    ExternalAuthDecision,
    MccMatch,
    MerchantRestrictionConfig,
    MerchantRestrictionCreate,
    PendingCardTransaction,
    PolicyCreate,
    PolicyScope,
    ScopeType,
    SimulateAuthorizationRequest,
    SimulateClearingRequest,
    SimulatedMerchant,
    SpendLimitConfig,
    SpendLimitCreate,
    TransactionAmountLimitConfig,
    TransactionAmountLimitCreate,
    WebhookEvent,
)
from youreapyousow.reap.webhooks import SIGNATURE_HEADER, verify

pytestmark = pytest.mark.anyio

COMPUTE = SimulatedMerchant(name="Vast.ai", mcc_code="7372", country="US")


async def _charge(
    funded: Funded, amount: str, merchant: SimulatedMerchant = COMPUTE, channel: str | None = None
) -> CardTransaction:
    req = SimulateAuthorizationRequest.model_validate(
        {"card_id": funded.card.id, "amount": Decimal(amount), "merchant": merchant}
        | ({"channel": channel} if channel else {})
    )
    return await funded.reap.simulate_authorization(req, idempotency_key=None)


async def _attach(funded: Funded, policy: PolicyCreate) -> None:
    await funded.reap.create_policy(policy, idempotency_key=None)


def _card_scope(funded: Funded) -> PolicyScope:
    return PolicyScope(type=ScopeType.CARD, id=funded.card.id)


def _spend(
    funded: Funded, period: str, amount: str, scope: PolicyScope | None = None
) -> SpendLimitCreate:
    return SpendLimitCreate(
        scope=scope or _card_scope(funded),
        name=f"{period.lower()} limit",
        config=SpendLimitConfig(
            window=CalendarWindow.model_validate({"period": period}), max_amount=Decimal(amount)
        ),
    )


def _declined(txn: CardTransaction) -> DeclinedCardTransaction:
    assert isinstance(txn, DeclinedCardTransaction)
    return txn


async def test_approved_charge_is_pending_and_reduces_balance(funded: Funded) -> None:
    """An unrestricted charge is authorised and held against the balance."""
    txn = await _charge(funded, "1.46")
    assert isinstance(txn, PendingCardTransaction)
    assert txn.amount.authorized == Decimal("1.46")
    assert txn.merchant.name == "Vast.ai"
    balance = await funded.reap.get_balance(funded.card.account_id)
    assert balance.liabilities.card_debt.pending == Decimal("1.46")
    assert balance.available_balance == Decimal("98.54")


async def test_lifetime_limit_declines_and_names_the_policy(funded: Funded) -> None:
    """The lifetime budget is enforced across charges and the decline names it."""
    await _attach(funded, _spend(funded, "LIFETIME", "5"))
    assert isinstance(await _charge(funded, "3"), PendingCardTransaction)
    declined = _declined(await _charge(funded, "2.01"))
    assert declined.decline_reason.code == "SPEND_LIMIT_EXCEEDED"
    assert declined.policy is not None
    assert (declined.policy.name, declined.policy.scope_type) == ("lifetime limit", "CARD")
    assert isinstance(await _charge(funded, "2"), PendingCardTransaction)


async def test_per_transaction_cap(funded: Funded) -> None:
    """A single charge above the cap is declined; at the cap it passes."""
    await _attach(
        funded,
        TransactionAmountLimitCreate(
            scope=_card_scope(funded),
            name="per-tx",
            config=TransactionAmountLimitConfig(max_amount=Decimal(5)),
        ),
    )
    assert _declined(await _charge(funded, "5.01")).decline_reason.code == (
        "TRANSACTION_AMOUNT_LIMIT_EXCEEDED"
    )
    assert isinstance(await _charge(funded, "5"), PendingCardTransaction)


async def test_daily_limit_resets_at_utc_midnight(funded: Funded, clock: ManualClock) -> None:
    """The daily window is calendar-based: tomorrow starts from zero."""
    await _attach(funded, _spend(funded, "DAILY", "10"))
    assert isinstance(await _charge(funded, "8"), PendingCardTransaction)
    assert _declined(await _charge(funded, "3")).decline_reason.code == "SPEND_LIMIT_EXCEEDED"
    clock.advance(days=1)
    assert isinstance(await _charge(funded, "3"), PendingCardTransaction)


async def test_mcc_deny_list_and_platform_block(funded: Funded) -> None:
    """Denied categories and the platform-blocked fuel MCC are declined."""
    await _attach(
        funded,
        MerchantRestrictionCreate(
            scope=_card_scope(funded),
            name="no gambling",
            config=MerchantRestrictionConfig(match=MccMatch(dimension="MCC", codes=["7995"])),
        ),
    )
    casino = SimulatedMerchant(name="Casino", mcc_code="7995")
    assert _declined(await _charge(funded, "1", casino)).decline_reason.code == (
        "MERCHANT_NOT_ALLOWED"
    )
    fuel = _declined(await _charge(funded, "1", SimulatedMerchant(mcc_code="5542")))
    assert fuel.decline_reason.code == "MERCHANT_CATEGORY_CODE_NOT_ALLOWED"
    assert fuel.policy is None
    assert isinstance(await _charge(funded, "1"), PendingCardTransaction)


async def test_country_deny_and_channel_restriction(funded: Funded) -> None:
    """Country and channel restrictions decline matching charges only."""
    await _attach(
        funded,
        MerchantRestrictionCreate(
            scope=_card_scope(funded),
            name="no KP",
            config=MerchantRestrictionConfig(
                match=CountryMatch(dimension="MERCHANT_COUNTRY", countries=["KP"])
            ),
        ),
    )
    await _attach(
        funded,
        ChannelRestrictionCreate(
            scope=_card_scope(funded),
            name="no ATM",
            config=ChannelRestrictionConfig(channels=["ATM"]),
        ),
    )
    abroad = SimulatedMerchant(name="X", mcc_code="7372", country="KP")
    assert _declined(await _charge(funded, "1", abroad)).decline_reason.code == (
        "MERCHANT_NOT_ALLOWED"
    )
    atm = await _charge(funded, "1", channel="ATM")
    assert _declined(atm).decline_reason.code == "CHANNEL_NOT_ALLOWED"
    assert isinstance(await _charge(funded, "1"), PendingCardTransaction)


async def test_authorisation_count_limit_approximates_single_use(funded: Funded) -> None:
    """One lifetime authorisation, then every further charge is declined."""
    await _attach(
        funded,
        AuthorizationCountLimitCreate(
            scope=_card_scope(funded),
            name="single use",
            config=AuthorizationCountLimitConfig(
                window=CalendarWindow(period="LIFETIME"), max_count=1
            ),
        ),
    )
    assert isinstance(await _charge(funded, "1"), PendingCardTransaction)
    assert _declined(await _charge(funded, "1")).decline_reason.code == (
        "AUTHORIZATION_COUNT_LIMIT_EXCEEDED"
    )


async def test_user_and_project_scopes_apply_and_strictest_wins(funded: Funded) -> None:
    """A stricter USER-scope limit binds even when the card's own limit is looser."""
    await _attach(funded, _spend(funded, "LIFETIME", "50"))
    await _attach(
        funded,
        _spend(funded, "LIFETIME", "4", PolicyScope(type=ScopeType.USER, id=funded.card.user_id)),
    )
    declined = _declined(await _charge(funded, "5"))
    assert declined.policy is not None
    assert declined.policy.scope_type == "USER"

    other = await issue_card(funded.reap, suffix="2")
    await funded.reap.create_policy(
        _spend(funded, "LIFETIME", "1", PolicyScope(type=ScopeType.PROJECT)), idempotency_key=None
    )
    txn = await funded.reap.simulate_authorization(
        SimulateAuthorizationRequest(card_id=other.id, amount=Decimal(2), merchant=COMPUTE),
        idempotency_key=None,
    )
    assert _declined(txn).policy is not None


async def test_disabled_policy_stops_applying(funded: Funded) -> None:
    """Disabling a policy lifts it."""
    policy = await funded.reap.create_policy(_spend(funded, "LIFETIME", "1"), idempotency_key=None)
    assert isinstance(await _charge(funded, "2"), DeclinedCardTransaction)
    disabled = await funded.reap.disable_policy(policy.id)
    assert disabled.status == "DISABLED"
    assert isinstance(await _charge(funded, "2"), PendingCardTransaction)


async def test_frozen_card_declines_and_unfreezes(funded: Funded) -> None:
    """A frozen card declines every charge until unfrozen."""
    frozen = await funded.reap.freeze_card(funded.card.id)
    assert (frozen.status, frozen.frozen) == ("FROZEN", True)
    assert _declined(await _charge(funded, "1")).decline_reason.code == "CARD_FROZEN"
    with pytest.raises(ReapError) as error:
        await funded.reap.freeze_card(funded.card.id)
    assert error.value.status == 400
    await funded.reap.unfreeze_card(funded.card.id)
    assert isinstance(await _charge(funded, "1"), PendingCardTransaction)


async def test_deleted_card_is_gone(funded: Funded) -> None:
    """Deletion is irreversible."""
    await funded.reap.delete_card(funded.card.id)
    with pytest.raises(ReapError) as error:
        await funded.reap.get_card(funded.card.id)
    assert error.value.code == "CARD_NOT_FOUND"


async def test_insufficient_funds_is_a_400(reap: ReapMock) -> None:
    """The simulator refuses a charge beyond the balance, as its OpenAPI describes."""
    card = await issue_card(reap, deposit="1")
    with pytest.raises(ReapError) as error:
        await reap.simulate_authorization(
            SimulateAuthorizationRequest(card_id=card.id, amount=Decimal(2)), idempotency_key=None
        )
    assert (error.value.status, error.value.code) == (400, "SIMULATION_INVALID_STATE")


async def test_clearing_settles_below_the_authorisation(funded: Funded) -> None:
    """Clearing at the metered amount releases the rest of the hold."""
    pending = await _charge(funded, "1.46")
    cleared = await funded.reap.simulate_clearing(
        SimulateClearingRequest(
            card_id=funded.card.id, transaction_id=pending.id, amount=Decimal("0.44")
        )
    )
    assert isinstance(cleared, ClearedCardTransaction)
    assert cleared.amount.cleared == Decimal("0.44")
    assert [e.type for e in cleared.events] == ["AUTHORIZATION", "CLEARING"]
    balance = await funded.reap.get_balance(funded.card.account_id)
    assert balance.available_balance == Decimal("99.56")
    with pytest.raises(ReapError, match="SIMULATION_INVALID_STATE"):
        await funded.reap.simulate_clearing(
            SimulateClearingRequest(
                card_id=funded.card.id, transaction_id=pending.id, amount=Decimal(1)
            )
        )
    fetched = await funded.reap.get_card_transaction(pending.id)
    assert fetched.status == "CLEARED"


async def test_effective_policies_report_usage_and_reset(funded: Funded) -> None:
    """Running totals report limit, used, remaining and the next UTC reset."""
    await _attach(funded, _spend(funded, "DAILY", "20"))
    await _attach(funded, _spend(funded, "LIFETIME", "25"))
    await _charge(funded, "1.46")
    effective = await funded.reap.effective_policies(ScopeType.CARD, funded.card.id)
    usage = {e.policy.name: e.usage for e in effective.items}
    daily, lifetime = usage["daily limit"], usage["lifetime limit"]
    assert daily is not None
    assert lifetime is not None
    assert (daily.used, daily.remaining) == (Decimal("1.46"), Decimal("18.54"))
    assert daily.resets_at == "2037-10-10T00:00:00Z"
    assert lifetime.resets_at is None
    assert lifetime.remaining == Decimal("23.54")


async def test_card_needs_approved_kyc(reap: ReapMock) -> None:
    """Cards are only issued to KYC-approved users."""
    user = await reap.create_user(CreateUserRequest(email="x@example.com", phone_number="+65"))
    account = await reap.create_account(CreateAccountRequest(owner_id=user.id), idempotency_key="a")
    with pytest.raises(ReapError, match="USER_APPLICATION_NOT_APPROVED"):
        await reap.create_card(
            CreateCardRequest(user_id=user.id, account_id=account.id, type="VIRTUAL"),
            idempotency_key="c",
        )


def test_window_bounds_are_calendar_utc() -> None:
    """Weekly windows start on Monday, monthly on the 1st, yearly on 1 January."""
    moment = datetime(2037, 10, 9, 15, 0, tzinfo=UTC)
    assert window_bounds("WEEKLY", moment) == (
        datetime(2037, 10, 5, tzinfo=UTC),
        datetime(2037, 10, 12, tzinfo=UTC),
    )
    assert window_bounds("MONTHLY", datetime(2037, 12, 31, tzinfo=UTC)) == (
        datetime(2037, 12, 1, tzinfo=UTC),
        datetime(2038, 1, 1, tzinfo=UTC),
    )
    assert window_bounds("YEARLY", moment)[0] == datetime(2037, 1, 1, tzinfo=UTC)
    assert window_bounds("LIFETIME", moment) == (None, None)


class Receiver:
    """Captures webhook deliveries and answers authorisation requests."""

    def __init__(self, decision: ExternalAuthDecision | None = None, delay: float = 0) -> None:
        self.received: list[tuple[str, dict[str, str], bytes]] = []
        self.decision = decision
        self.delay = delay

    async def __call__(self, url: str, headers: dict[str, str], body: bytes) -> DeliveryResponse:
        """Record a delivery and answer it.

        Returns:
            The canned response.
        """
        self.received.append((url, headers, body))
        if self.delay:
            await asyncio.sleep(self.delay)
        if url.endswith("/authorization"):
            if self.decision is None:
                return DeliveryResponse(200, b"not json")
            return DeliveryResponse(200, self.decision.model_dump_json(exclude_none=True).encode())
        return DeliveryResponse(200, b"")


async def test_notifications_are_signed_and_delivered(funded: Funded, clock: ManualClock) -> None:
    """Every state change is delivered with a signature the receiver can verify."""
    receiver = Receiver()
    funded.reap.engine.delivery = receiver
    endpoint = await funded.reap.create_webhook(
        CreateWebhookRequest(name="cp", url="https://cp.example/webhooks/reap")
    )
    await _charge(funded, "1")
    await funded.reap.freeze_card(funded.card.id)
    types: list[str] = []
    for _, headers, body in receiver.received:
        event: WebhookEvent = verify(
            endpoint.signing_secret, body, headers[SIGNATURE_HEADER], clock()
        )
        types.append(event.type)
    assert types == ["CARD_TRANSACTION_CREATED", "CARD_STATUS_UPDATED"]
    assert all(d.status == 200 for d in funded.reap.engine.deliveries)


async def test_request_endpoint_needs_external_mode(funded: Funded) -> None:
    """A REQUEST endpoint on a Managed project is refused with 403."""
    with pytest.raises(ReapError) as error:
        await funded.reap.create_webhook(
            CreateWebhookRequest(name="auth", url="https://cp/authorization", mode="REQUEST")
        )
    assert error.value.status == 403


async def _external(clock: ManualClock, receiver: Receiver) -> Funded:
    reap = ReapMock(clock=clock, authorization_mode=AuthorizationMode.EXTERNAL, delivery=receiver)
    with pytest.raises(ReapError, match="AUTHORIZATION_ENDPOINT_REQUIRED"):
        await issue_card(reap)
    await reap.create_webhook(
        CreateWebhookRequest(
            name="auth", url="https://cp/webhooks/reap/authorization", mode="REQUEST"
        )
    )
    return Funded(reap, await issue_card(reap, suffix="ext"))


async def test_external_mode_approves_on_approve(clock: ManualClock) -> None:
    """In External mode the endpoint's APPROVE authorises the charge."""
    funded = await _external(clock, Receiver(ExternalAuthDecision(decision="APPROVE")))
    assert isinstance(await _charge(funded, "1"), PendingCardTransaction)


@pytest.mark.parametrize(
    ("receiver", "reason"),
    [
        (
            Receiver(ExternalAuthDecision(decision="DECLINE", reason="TRANSACTION_NOT_ALLOWED")),
            "declined",
        ),
        (Receiver(None), "invalid body"),
        (Receiver(ExternalAuthDecision(decision="APPROVE"), delay=2.0), "1.6 s deadline"),
    ],
)
async def test_external_mode_fails_closed(
    clock: ManualClock, receiver: Receiver, reason: str
) -> None:
    """A decline, a bad body or a missed deadline all decline the charge."""
    funded = await _external(clock, receiver)
    declined = _declined(await _charge(funded, "1"))
    assert declined.decline_reason.code == "TRANSACTION_NOT_ALLOWED"
    assert reason in declined.decline_reason.message
    request = receiver.received[0]
    assert b"CARD_AUTHORIZATION_REQUEST" in request[2]

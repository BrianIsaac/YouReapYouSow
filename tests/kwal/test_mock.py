"""The Kwal participant gateway mock: Kwal's own state over the agentic mock's catalogue.

Behaviours follow the skill's references at ``be5f52c``: a sandbox payment is a
simulated spend on the participant's own card, held on the vault and then cleared; a
payment id replays its payment; a vault that cannot cover the quote, or a card another
payment holds, saves nothing.
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest

from youreapyousow.clock import ManualClock
from youreapyousow.kwal.mock import (
    MOCK_BASE_URL,
    MOCK_TOKEN,
    KwalMockConfig,
    KwalMockEngine,
    KwalMockError,
    Route,
    create_kwal_mock_app,
)
from youreapyousow.kwal.models import (
    FundingState,
    KwalShippingAddress,
    PaymentRequest,
    PaymentState,
    QuoteLine,
    QuoteRequest,
    SetupState,
    ShippingRequest,
    VariantRequest,
)

pytestmark = pytest.mark.anyio

NVME = "var-northwind-nvme-1tb"
SITE = KwalShippingAddress(
    first_name="Site",
    last_name="Engineer",
    phone="+6591234567",
    line1="1 Fusionopolis Way",
    city="Singapore",
    postal_code="138632",
    country="SG",
)


def quote_for(engine: KwalMockEngine, variant: str = NVME, quantity: int = 1) -> str:
    """Quote one line to the site address and return the quote id."""
    request = QuoteRequest(
        email="operator@example.com",
        lines=[QuoteLine(variant_id=variant, quantity=quantity)],
        shipping_address=SITE,
    )
    return engine.create_quote(request).quote_id


@pytest.fixture
def engine(clock: ManualClock) -> KwalMockEngine:
    """A set-up, funded participant on the manual clock."""
    return KwalMockEngine(clock=clock)


def test_the_participant_starts_ready_and_funded(engine: KwalMockEngine) -> None:
    """Like the Reap mock's own test card, the mock's participant is set up and funded."""
    assert engine.status().state == SetupState.READY
    funding = engine.funding()
    assert funding.state == FundingState.READY
    assert funding.available is not None
    assert funding.available.value == Decimal(500)


def test_a_funding_check_against_an_amount_names_the_shortfall(engine: KwalMockEngine) -> None:
    """Asked for more than the vault holds, the answer is funds needed and the gap."""
    funding = engine.funding(required_minor_units=600_000_000)
    assert funding.state == FundingState.FUNDS_NEEDED
    assert funding.shortfall is not None
    assert funding.shortfall.value == Decimal(100)


def test_search_details_and_variant_come_from_the_catalogue(engine: KwalMockEngine) -> None:
    """Kwal's catalogue is Reap's: the agentic mock's products, in Kwal's shapes."""
    found = engine.search("1TB NVMe SSD", limit=10).products
    nvme = next(p for p in found if p.product_id == "prd-northwind-nvme-1tb")
    assert nvme.merchant == "Northwind Components"
    assert nvme.price is not None
    assert nvme.price.value == Decimal(69)
    detail = engine.product("prd-northwind-nvme-1tb")
    ids = [value.option_id for option in detail.options for value in option.values]
    variant = engine.variant("prd-northwind-nvme-1tb", VariantRequest(option_ids=ids))
    assert (variant.variant_id, variant.purchasable) == (NVME, True)
    default = engine.variant("prd-northwind-nvme-1tb", VariantRequest(option_ids=[]))
    assert default.variant_id == NVME


def test_an_unknown_product_is_not_found(engine: KwalMockEngine) -> None:
    """A product the catalogue does not hold is ``ParticipantNotFound``."""
    with pytest.raises(KwalMockError) as missing:
        engine.product("prd-nowhere")
    assert (missing.value.status, missing.value.tag) == (404, "ParticipantNotFound")


def test_a_quote_lands_with_the_catalogue_s_breakdown(engine: KwalMockEngine) -> None:
    """Items 69 and Standard shipping 8, tax included in Singapore: a total of 77 USD."""
    quote = engine.get_quote(quote_for(engine))
    assert quote.total.value == Decimal(77)
    assert quote.total.currency == "USD"
    assert quote.selected_shipping_option_id == "northwind-standard"
    express = engine.select_shipping(
        quote.quote_id, ShippingRequest(shipping_option_id="northwind-express")
    )
    assert express.total.value == Decimal(87)


def test_local_quote_mode_prices_one_dollar_an_item(clock: ManualClock) -> None:
    """The skill's local quote mode: ``sandbox_quote_`` ids, 1 per item, no shipping."""
    engine = KwalMockEngine(clock=clock, config=KwalMockConfig(local_quotes=True))
    quote = engine.get_quote(quote_for(engine, quantity=2))
    assert quote.quote_id.startswith("sandbox_quote_")
    assert quote.total.value == Decimal(2)
    assert quote.shipping_options == []


def test_a_payment_is_a_card_spend_held_then_cleared(engine: KwalMockEngine) -> None:
    """Held on the vault at authorisation, charged at clearing, the card transaction named."""
    quote_id = quote_for(engine)
    created = engine.create_payment(PaymentRequest(payment_id="pay_1", quote_id=quote_id))
    assert (created.state, created.step) == (PaymentState.PENDING, "card_authorization")
    assert created.held is not None
    assert created.held.value == Decimal(77)
    assert engine.funding().available.value == Decimal(423)  # pyright: ignore[reportOptionalMemberAccess]
    cleared = engine.get_payment("pay_1")
    assert (cleared.state, cleared.step) == (PaymentState.COMPLETED, "card_clearing")
    assert cleared.charged is not None
    assert (cleared.charged.value, cleared.charged.currency) == (Decimal(77), "USDC")
    assert cleared.card_transaction_id is not None
    assert cleared.order_id is None


def test_the_same_payment_id_replays_its_payment(engine: KwalMockEngine) -> None:
    """A repeated checkout reuses the saved payment id and never pays twice."""
    quote_id = quote_for(engine)
    first = engine.create_payment(PaymentRequest(payment_id="pay_1", quote_id=quote_id))
    again = engine.create_payment(PaymentRequest(payment_id="pay_1", quote_id=quote_id))
    assert again.payment_id == first.payment_id
    assert engine.funding().available.value == Decimal(423)  # pyright: ignore[reportOptionalMemberAccess]


def test_a_vault_that_cannot_cover_the_quote_saves_nothing(clock: ManualClock) -> None:
    """``ParticipantFundingRequest``: nothing was sent, the same checkout can run later."""
    engine = KwalMockEngine(clock=clock, config=KwalMockConfig(funded_usdc=Decimal(10)))
    with pytest.raises(KwalMockError) as refused:
        engine.create_payment(PaymentRequest(payment_id="pay_1", quote_id=quote_for(engine)))
    assert (refused.value.status, refused.value.tag) == (400, "ParticipantFundingRequest")
    with pytest.raises(KwalMockError):
        engine.get_payment("pay_1")


def test_a_card_another_payment_holds_saves_nothing(engine: KwalMockEngine) -> None:
    """One sandbox payment per card at a time; the holder is named when it is the caller's."""
    engine.create_payment(PaymentRequest(payment_id="pay_1", quote_id=quote_for(engine)))
    second = quote_for(engine, "var-northwind-nvme-2tb")
    with pytest.raises(KwalMockError) as busy:
        engine.create_payment(PaymentRequest(payment_id="pay_2", quote_id=second))
    assert (busy.value.status, busy.value.tag) == (409, "ParticipantCardBusy")
    assert busy.value.data == {"holdingPaymentId": "pay_1"}


def test_a_quote_another_payment_holds_is_refused_as_already_used(
    engine: KwalMockEngine,
) -> None:
    """The second payment is recorded as an error at ``quote_already_used``."""
    quote_id = quote_for(engine)
    engine.create_payment(PaymentRequest(payment_id="pay_1", quote_id=quote_id))
    engine.get_payment("pay_1")
    used = engine.create_payment(PaymentRequest(payment_id="pay_2", quote_id=quote_id))
    assert (used.state, used.step) == (PaymentState.ERROR, "quote_already_used")
    assert engine.get_quote(quote_id).payment_id == "pay_1"


def test_hosted_approval_mode_waits_for_the_person(clock: ManualClock) -> None:
    """An agentic checkout waits on its approval link, then completes with an order id."""
    engine = KwalMockEngine(clock=clock, config=KwalMockConfig(hosted_approval=True))
    created = engine.create_payment(PaymentRequest(payment_id="pay_1", quote_id=quote_for(engine)))
    assert created.state == PaymentState.REQUIRES_ACTION
    assert created.approval_url is not None
    engine.approve("pay_1")
    completed = engine.get_payment("pay_1")
    assert completed.state == PaymentState.COMPLETED
    assert completed.order_id is not None


@pytest.fixture
async def http(engine: KwalMockEngine) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client on the mock app with the participant's token."""
    transport = httpx.ASGITransport(app=create_kwal_mock_app(engine))
    async with httpx.AsyncClient(
        transport=transport,
        base_url=MOCK_BASE_URL,
        headers={"Authorization": f"Bearer {MOCK_TOKEN}"},
    ) as client:
        yield client


async def test_the_app_serves_the_participant_routes(http: httpx.AsyncClient) -> None:
    """The routes are the skill's, the bodies camelCase, the enums by their full names."""
    status = await http.get("/kwal/participant/v1/status")
    assert status.json()["state"] == "PARTICIPANT_SETUP_STATE_READY"
    found = await http.get("/kwal/participant/v1/products", params={"query": "NVMe SSD"})
    assert found.json()["products"][0]["productId"]
    assert (await http.get("/kwal/participant/v1/payments")).status_code == 405


async def test_the_app_refuses_another_token(engine: KwalMockEngine) -> None:
    """Only the participant's own token is accepted."""
    transport = httpx.ASGITransport(app=create_kwal_mock_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url=MOCK_BASE_URL) as client:
        refused = await client.get(
            "/kwal/participant/v1/status", headers={"Authorization": "Bearer other"}
        )
    assert refused.status_code == 401
    assert refused.json()["type"] == "tag:kraken.com,2025:ParticipantUnauthenticated"


async def test_an_injected_error_is_answered_before_the_route_runs(
    engine: KwalMockEngine, http: httpx.AsyncClient
) -> None:
    """A fault answers in Kwal's error shape, with ``Retry-After`` when given."""
    engine.inject(Route.CREATE_QUOTE, 503, "ParticipantUnavailable", retry_after_s=2)
    body = {"email": "operator@example.com", "lines": [{"variantId": NVME, "quantity": 1}]}
    answered = await http.post("/kwal/participant/v1/quotes", json=body)
    assert answered.status_code == 503
    assert answered.headers["Retry-After"] == "2"
    assert answered.json()["type"] == "tag:kraken.com,2025:ParticipantUnavailable"
    again = await http.post(
        "/kwal/participant/v1/quotes", json=body | {"shippingAddress": SITE.to_wire()}
    )
    assert again.status_code == 200


async def test_a_dropped_response_loses_the_answer_after_the_payment_is_saved(
    engine: KwalMockEngine, http: httpx.AsyncClient
) -> None:
    """The client sees a read failure; the payment exists and replays under its id."""
    quote_id = quote_for(engine)
    engine.drop_next(Route.CREATE_PAYMENT)
    with pytest.raises(httpx.ReadError):
        await http.post(
            "/kwal/participant/v1/payments", json={"paymentId": "pay_1", "quoteId": quote_id}
        )
    assert engine.get_payment("pay_1").payment_id == "pay_1"

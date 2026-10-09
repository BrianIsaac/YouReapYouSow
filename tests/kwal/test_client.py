"""``KwalClient``: the agentic seam, answered by the Kwal participant gateway.

Every test runs the client's real HTTP code against the gateway mock in process
(``kwal/mock.py``), so the translation from the participant contract to the seam's Reap
shapes is exercised line by line, as the agentic mock exercises the Reap client.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from youreapyousow.clock import ManualClock
from youreapyousow.kwal.client import KwalClient
from youreapyousow.kwal.mock import (
    MOCK_BASE_URL,
    MOCK_TOKEN,
    KwalMockConfig,
    KwalMockEngine,
    Route,
    create_kwal_mock_app,
)
from youreapyousow.kwal.models import KwalAmount, KwalSetup, SetupState
from youreapyousow.kwal.session import KwalSession
from youreapyousow.reap.client import ReapError, ReapTransportError
from youreapyousow.reap.models import (
    AgenticErrorCode,
    CheckoutStatus,
    ClientReferenceOwner,
    CreateCheckoutRequest,
    CreateExternalCheckoutQuoteRequest,
    CreateExternalEnrollmentRequest,
    CreateItemsQuoteRequest,
    CreateUserRequest,
    EnrollmentStatus,
    ExternalCheckout,
    Presentation,
    ProductDetailsRequest,
    ProductSearchRequest,
    QuoteItem,
    ResolveVariantRequest,
    SearchFilters,
    SelectShippingOptionRequest,
    ShippingAddress,
)

pytestmark = pytest.mark.anyio

NVME_PRODUCT = "prd-northwind-nvme-1tb"
NVME = "var-northwind-nvme-1tb"
SITE = ShippingAddress(
    first_name="Site",
    last_name="Engineer",
    phone="+6591234567",
    address_line1="1 Fusionopolis Way",
    city="Singapore",
    postal_code="138632",
    country="SG",
)
RETURN = Presentation(return_url="https://example.com/return")
OPERATOR = CreateExternalEnrollmentRequest(
    owner=ClientReferenceOwner(id="operator", email="operator@example.com"),
    presentation=RETURN,
)

type Build = Callable[..., KwalClient]


async def no_sleep(_: float) -> None:
    """Wait for nothing: the tests read the mock's state at once."""


@pytest.fixture
def engine(clock: ManualClock) -> KwalMockEngine:
    """A set-up, funded participant on the manual clock."""
    return KwalMockEngine(clock=clock)


@pytest.fixture
async def kwal(engine: KwalMockEngine) -> AsyncIterator[KwalClient]:
    """The client on the gateway mock, with the participant's session."""
    client = client_for(engine)
    yield client
    await client.aclose()


class FakeTime:
    """A monotonic clock that only moves when the client sleeps."""

    def __init__(self) -> None:
        """Start at zero with no waits."""
        self.now = 0.0
        self.waits: list[float] = []

    async def sleep(self, seconds: float) -> None:
        """Record the wait and move the clock."""
        self.waits.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        """Return the clock."""
        return self.now


def _nvme_quote() -> CreateItemsQuoteRequest:
    return CreateItemsQuoteRequest(
        email="operator@example.com",
        items=[QuoteItem(variant_id=NVME, quantity=1)],
        shipping_address=SITE,
    )


def client_for(
    engine: KwalMockEngine, token: str = MOCK_TOKEN, time: FakeTime | None = None
) -> KwalClient:
    """Build a client for the mock, as ``from_settings`` builds one for the gateway."""
    session = KwalSession(
        service_url=MOCK_BASE_URL,
        token=SecretStr(token),
        expires_at=int(datetime(2037, 10, 15, tzinfo=UTC).timestamp()),
        path=Path("/nowhere/credentials.json"),
    )
    return KwalClient(
        session,
        transport=httpx.ASGITransport(app=create_kwal_mock_app(engine)),
        sleep=time.sleep if time else no_sleep,
        monotonic=time.monotonic if time else FakeTime().monotonic,
    )


async def quote(kwal: KwalClient, variant: str = NVME, key: str = "quo:1") -> str:
    """Land a quote for one item to the site and return its id."""
    request = CreateItemsQuoteRequest(
        email="operator@example.com",
        items=[QuoteItem(variant_id=variant, quantity=1)],
        shipping_address=SITE,
    )
    return (await kwal.create_quote(request, idempotency_key=key)).id


async def checkout_for(kwal: KwalClient, quote_id: str, key: str = "claim:1") -> str:
    """Check a quote out under a claim key and return the checkout id."""
    enrolment = await kwal.create_enrollment(OPERATOR, idempotency_key="enr:o1")
    request = CreateCheckoutRequest(
        quote_id=quote_id, enrollment_id=enrolment.id, presentation=RETURN
    )
    return (await kwal.create_checkout(request, idempotency_key=key)).id


async def test_the_client_names_its_backend(kwal: KwalClient) -> None:
    """``/status``, the ledger and the dashboard read ``kwal``; a payment is a card spend."""
    assert kwal.backend == "kwal"
    assert kwal.card_spend


async def test_a_ready_participant_is_an_active_enrolment(kwal: KwalClient) -> None:
    """The participant's set-up card is what purchases are charged to: ACTIVE when ready."""
    created = await kwal.create_enrollment(OPERATOR, idempotency_key="enr:o1")
    assert created.status == EnrollmentStatus.ACTIVE
    uuid.UUID(created.id)
    read = await kwal.get_enrollment(created.id)
    assert (read.id, read.status) == (created.id, EnrollmentStatus.ACTIVE)
    assert read.payment_method is None
    assert read.next_action is None


async def test_the_enrolment_never_carries_the_vault_address(
    kwal: KwalClient, engine: KwalMockEngine
) -> None:
    """The id is derived from the vault, so the address itself never reaches the ledger."""
    read = await kwal.get_enrollment(
        (await kwal.create_enrollment(OPERATOR, idempotency_key="enr:o1")).id
    )
    assert engine.setup.vault_address is not None
    assert engine.setup.vault_address.lower() not in read.model_dump_json().lower()


async def test_the_enrolment_keeps_its_id_as_the_setup_advances(
    kwal: KwalClient, engine: KwalMockEngine
) -> None:
    """Bound while setup is pending, the same enrolment reads ACTIVE once it is ready."""
    ready = engine.setup
    engine.set_setup(KwalSetup(state=SetupState.PENDING, step="vault_deployment"))
    created = await kwal.create_enrollment(OPERATOR, idempotency_key="enr:o1")
    assert created.status == EnrollmentStatus.REQUIRES_ACTION
    engine.set_setup(
        ready.model_copy(
            update={
                "enrollment_id": "11111111-1111-4111-8111-111111111111",
                "enrollment_status": "ACTIVE",
            }
        )
    )
    read = await kwal.get_enrollment(created.id)
    assert (read.id, read.status) == (created.id, EnrollmentStatus.ACTIVE)


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (SetupState.NOT_STARTED, EnrollmentStatus.REQUIRES_ACTION),
        (SetupState.PENDING, EnrollmentStatus.REQUIRES_ACTION),
        (SetupState.NEEDS_OPERATOR, EnrollmentStatus.FAILED),
    ],
)
async def test_an_unfinished_setup_is_not_an_active_enrolment(
    kwal: KwalClient, engine: KwalMockEngine, state: SetupState, status: EnrollmentStatus
) -> None:
    """Rule 6 refuses until the owner finishes the skill's setup and funds the vault."""
    engine.set_setup(KwalSetup(state=state, step="vault_deployment"))
    created = await kwal.create_enrollment(OPERATOR, idempotency_key="enr:o1")
    assert created.status == status


async def test_another_enrolment_id_is_not_found(kwal: KwalClient) -> None:
    """Only the participant's own enrolment exists on this backend."""
    with pytest.raises(ReapError) as missing:
        await kwal.get_enrollment(str(uuid.uuid4()))
    assert missing.value.code == AgenticErrorCode.ENROLLMENT_NOT_FOUND


async def test_a_search_answers_in_the_seam_s_shape(kwal: KwalClient) -> None:
    """Each product carries its merchant and price; what Kwal does not take is warned of."""
    found = await kwal.search_products(
        ProductSearchRequest(
            query="1TB NVMe SSD", filters=SearchFilters(availability="AVAILABLE_ONLY")
        )
    )
    nvme = next(p for p in found.products if p.id == NVME_PRODUCT)
    assert nvme.merchant.name == "Northwind Components"
    assert nvme.price_range.min.amount == nvme.price_range.max.amount == Decimal(69)
    assert any("filters" in warning for warning in found.warnings)
    assert found.pagination.returned_count == len(found.products)
    again = await kwal.search_products(ProductSearchRequest(query="1TB NVMe SSD"))
    assert again.id != found.id


async def test_a_result_without_a_merchant_or_price_is_dropped_and_named(
    kwal: KwalClient, engine: KwalMockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 9 needs the merchant and rule 10 the price; neither is invented."""
    original = engine.search

    def stripped(query: str, *, limit: int | None = None):  # noqa: ANN202
        products = original(query, limit=limit)
        first, *rest = products.products
        return products.model_copy(
            update={"products": [first.model_copy(update={"merchant": None}), *rest]}
        )

    monkeypatch.setattr(engine, "search", stripped)
    found = await kwal.search_products(ProductSearchRequest(query="1TB NVMe SSD"))
    assert any("no merchant" in warning for warning in found.warnings)
    assert all(p.merchant.name for p in found.products)


async def test_details_resolve_the_default_variant_rather_than_invent_one(
    kwal: KwalClient,
) -> None:
    """Kwal reports no default variant, so it is asked for one."""
    details = await kwal.product_details(
        ProductDetailsRequest(product_ids=[NVME_PRODUCT, "prd-nowhere"])
    )
    (product,) = details.products
    assert product.default_variant.id == NVME
    assert {o.name for o in product.options} == {"Capacity", "Interface", "Form factor"}
    assert [e.product_id for e in details.errors] == ["prd-nowhere"]


async def test_a_resolved_variant_carries_its_options_by_name(kwal: KwalClient) -> None:
    """Rule 10 judges a part by its options; Kwal's variant names none, its product does."""
    details = await kwal.product_details(ProductDetailsRequest(product_ids=[NVME_PRODUCT]))
    ids = [o.values[0].option_id for o in details.products[0].options]
    variant = await kwal.resolve_variant(
        ResolveVariantRequest(product_id=NVME_PRODUCT, option_ids=ids)
    )
    assert variant.id == NVME
    assert {(o.name, o.value) for o in variant.options or []} >= {("Capacity", "1 TB")}
    assert (variant.price.amount, variant.available) == (Decimal(69), True)
    assert variant.requires_shipping is None


async def test_a_quote_lands_its_breakdown(kwal: KwalClient) -> None:
    """``finalAmount`` is Kwal's total as sent; the deadline is an ISO instant."""
    landed = await kwal.get_quote(await quote(kwal))
    breakdown = landed.amount_breakdown
    assert breakdown.final_amount.amount == Decimal(77)
    assert breakdown.items_subtotal.amount == Decimal(69)
    assert breakdown.shipping is not None
    assert breakdown.shipping.amount == Decimal(8)
    datetime.fromisoformat(landed.expires_at)
    assert [o.selected for o in landed.shipping_options] == [True, False]
    express = await kwal.select_shipping_option(
        landed.id,
        SelectShippingOptionRequest(shipping_option_id="northwind-express"),
        idempotency_key=None,
    )
    assert express.amount_breakdown.final_amount.amount == Decimal(87)


async def test_a_quote_waiting_in_kwal_s_line_is_asked_again_with_the_same_inputs(
    engine: KwalMockEngine,
) -> None:
    """``ParticipantUnavailable`` on a quote is Kwal's queue: the same request keeps its place.

    The skill: "A call that waits in line keeps its place ... A retry with the same inputs
    gets the result of the call in line" (``references/quotes.md:68``), every 15 to 30 s
    (``references/debug.md:30``). It is one attempt, so no new attempt key is minted.
    """
    clock = FakeTime()
    kwal = client_for(engine, time=clock)
    engine.inject(Route.CREATE_QUOTE, 503, "ParticipantUnavailable", times=2)
    minted: list[str] = []
    landed = await kwal.create_quote(
        _nvme_quote(), idempotency_key="quo:1", retry_key=lambda: minted.append("x") or "quo:2"
    )
    assert landed.amount_breakdown.final_amount.amount == Decimal(77)
    assert clock.waits == [20.0, 20.0]
    assert minted == []
    await kwal.aclose()


async def test_a_quote_kwal_keeps_queued_past_its_patience_surfaces(
    engine: KwalMockEngine,
) -> None:
    """Past the client's patience the seam's ``QUOTE_TEMPORARILY_UNAVAILABLE`` surfaces."""
    clock = FakeTime()
    kwal = client_for(engine, time=clock)
    engine.inject(Route.CREATE_QUOTE, 503, "ParticipantUnavailable", times=20)
    with pytest.raises(ReapError) as unavailable:
        await kwal.create_quote(_nvme_quote(), idempotency_key="quo:1")
    assert unavailable.value.code == AgenticErrorCode.QUOTE_TEMPORARILY_UNAVAILABLE
    assert sum(clock.waits) <= 120.0
    await kwal.aclose()


async def test_a_checkout_url_quote_is_refused(kwal: KwalClient) -> None:
    """Kwal quotes catalogue variants only."""
    request = CreateExternalCheckoutQuoteRequest(
        email="operator@example.com",
        external_checkout=ExternalCheckout(
            merchant_domain="shop.example", checkout_url="https://shop.example/cart"
        ),
        shipping_address=SITE,
    )
    with pytest.raises(ReapError) as refused:
        await kwal.create_quote(request, idempotency_key="quo:1")
    assert refused.value.code == AgenticErrorCode.AGENTIC_REQUEST_REJECTED


async def test_a_local_mode_quote_id_is_carried_as_a_uuid(clock: ManualClock) -> None:
    """A checkout request takes a UUID; ``sandbox_quote_`` ids are mapped and mapped back."""
    engine = KwalMockEngine(clock=clock, config=KwalMockConfig(local_quotes=True))
    kwal = client_for(engine)
    quote_id = await quote(kwal)
    uuid.UUID(quote_id)
    assert (await kwal.get_quote(quote_id)).amount_breakdown.final_amount.amount == Decimal(1)
    checkout_id = await checkout_for(kwal, quote_id)
    assert (await kwal.poll_checkout(checkout_id)).status == CheckoutStatus.COMPLETED
    await kwal.aclose()


async def test_a_checkout_is_a_card_spend_that_completes(
    kwal: KwalClient, engine: KwalMockEngine
) -> None:
    """Held, then cleared: COMPLETED with the card transaction and the USDC charged."""
    checkout_id = await checkout_for(kwal, await quote(kwal))
    assert checkout_id.startswith("pay_")
    completed = await kwal.poll_checkout(checkout_id, every_s=0.01)
    assert completed.status == CheckoutStatus.COMPLETED
    assert completed.final_amount is not None
    assert (completed.final_amount.amount, completed.final_amount.currency) == (
        Decimal(77),
        "USD",
    )
    payment = engine.get_payment(checkout_id)
    assert completed.order_id == payment.card_transaction_id


async def test_the_claim_key_is_the_payment_id_so_a_replay_never_pays_twice(
    kwal: KwalClient, engine: KwalMockEngine
) -> None:
    """The same key sends the same payment id; Kwal replays the saved payment."""
    quote_id = await quote(kwal)
    first = await checkout_for(kwal, quote_id, key="claim:1")
    again = await checkout_for(kwal, quote_id, key="claim:1")
    assert first == again
    assert engine.funding().available.value == Decimal(423)  # pyright: ignore[reportOptionalMemberAccess]


async def test_a_lost_answer_is_ambiguous_and_its_replay_settles_it(
    kwal: KwalClient, engine: KwalMockEngine
) -> None:
    """The payment may exist: the error says so, and the same key reads it back."""
    quote_id = await quote(kwal)
    engine.drop_next(Route.CREATE_PAYMENT)
    with pytest.raises(ReapTransportError) as lost:
        await checkout_for(kwal, quote_id)
    assert lost.value.maybe_sent
    assert await checkout_for(kwal, quote_id) == next(iter(engine._payments))  # pyright: ignore[reportPrivateUsage]


async def test_a_gateway_failure_on_checkout_may_have_saved_a_payment(
    kwal: KwalClient, engine: KwalMockEngine
) -> None:
    """A 5xx on the create is never a definite refusal (``references/checkout.md:62``)."""
    engine.inject(Route.CREATE_PAYMENT, 503, "ParticipantUnavailable")
    with pytest.raises(ReapTransportError) as unknown:
        await checkout_for(kwal, await quote(kwal))
    assert unknown.value.maybe_sent


@pytest.mark.parametrize(
    ("status", "tag", "code"),
    [
        (400, "ParticipantFundingRequest", "KWAL_FUNDS_NEEDED"),
        (409, "ParticipantCardBusy", "KWAL_CARD_BUSY"),
    ],
)
async def test_a_refusal_that_saved_nothing_is_a_definite_error(
    kwal: KwalClient, engine: KwalMockEngine, status: int, tag: str, code: str
) -> None:
    """An unfunded vault or a busy card saved nothing: the intent fails, nothing is charged."""
    engine.inject(Route.CREATE_PAYMENT, status, tag)
    with pytest.raises(ReapError) as refused:
        await checkout_for(kwal, await quote(kwal))
    assert (refused.value.status, refused.value.code) == (status, code)


async def test_a_payment_waiting_for_approval_carries_its_link(clock: ManualClock) -> None:
    """In hosted-approval mode the seam sees ``REQUIRES_ACTION`` and Kwal's approval link."""
    engine = KwalMockEngine(clock=clock, config=KwalMockConfig(hosted_approval=True))
    kwal = client_for(engine)
    checkout_id = await checkout_for(kwal, await quote(kwal))
    waiting = await kwal.poll_checkout(checkout_id)
    assert waiting.status == CheckoutStatus.REQUIRES_ACTION
    assert waiting.next_action is not None
    assert waiting.next_action.url.startswith("https://")
    engine.approve(checkout_id)
    completed = await kwal.get_checkout(checkout_id)
    assert completed.status == CheckoutStatus.COMPLETED
    assert completed.order_id == "KWAL-ORDER-000001"
    await kwal.aclose()


async def test_a_quote_another_payment_holds_fails_the_checkout(
    kwal: KwalClient, engine: KwalMockEngine
) -> None:
    """``quote_already_used`` charged nothing: the checkout reads FAILED."""
    quote_id = await quote(kwal)
    first = await checkout_for(kwal, quote_id, key="claim:1")
    await kwal.poll_checkout(first)
    second = await checkout_for(kwal, quote_id, key="claim:2")
    assert (await kwal.get_checkout(second)).status == CheckoutStatus.FAILED
    assert engine.get_payment(second).step == "quote_already_used"


async def test_an_unknown_checkout_is_not_found(kwal: KwalClient) -> None:
    """A payment id Kwal never saved is ``CHECKOUT_NOT_FOUND``."""
    with pytest.raises(ReapError) as missing:
        await kwal.get_checkout("pay_nothing")
    assert missing.value.code == AgenticErrorCode.CHECKOUT_NOT_FOUND


async def test_a_rejected_session_says_so(engine: KwalMockEngine) -> None:
    """An expired or wrong token is named for what it is, never echoed."""
    kwal = client_for(engine, token="expired-token")
    with pytest.raises(ReapError) as rejected:
        await kwal.setup()
    assert rejected.value.code == "KWAL_SESSION_REJECTED"
    assert "expired-token" not in str(rejected.value)
    await kwal.aclose()


async def test_the_card_path_is_not_offered(kwal: KwalClient) -> None:
    """Kwal issues its own card; the dormant card path has nothing to call on it."""
    with pytest.raises(ReapError) as refused:
        await kwal.create_user(
            CreateUserRequest(email="op@example.com", phone_number="+6500000000", first_name="Op")
        )
    assert refused.value.code == "KWAL_CARD_PATH_UNSUPPORTED"


async def test_setup_and_funding_are_read_for_status(kwal: KwalClient) -> None:
    """``/status`` and ``swap_check`` read the setup and what the card can spend."""
    assert (await kwal.setup()).state == SetupState.READY
    funding = await kwal.funding()
    assert funding.available is not None
    assert funding.available.value == Decimal(500)


async def test_tax_kwal_does_not_add_to_the_total_is_read_as_included(kwal: KwalClient) -> None:
    """Items 69 and shipping 8 land at 77 with tax 6.36 inside: the figures say it is included.

    Kwal sends no flag, so the breakdown would otherwise read as not summing to its total.
    """
    breakdown = (await kwal.get_quote(await quote(kwal))).amount_breakdown
    assert breakdown.tax is not None
    assert breakdown.tax.included_in_prices is True


async def test_a_zero_tax_is_not_called_included(kwal: KwalClient, engine: KwalMockEngine) -> None:
    """The real gateway quotes tax 0; nothing is inferred from a zero."""
    quote_id = await quote(kwal)
    held = engine._quotes[quote_id]  # pyright: ignore[reportPrivateUsage]
    engine._quotes[quote_id] = held.model_copy(  # pyright: ignore[reportPrivateUsage]
        update={"tax": KwalAmount(minor_units="0", currency="USD", decimals=2)}
    )
    breakdown = (await kwal.get_quote(quote_id)).amount_breakdown
    assert breakdown.tax is not None
    assert breakdown.tax.included_in_prices is None

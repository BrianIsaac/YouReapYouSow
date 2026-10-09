"""Tests for the shared data model."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.conftest import START
from tests.factories import make_offer, make_quote
from tests.reap.agentic_examples import (
    ENROLLMENT_RESPONSE,
    QUOTE_RESPONSE,
    SEARCH_RESPONSE,
    SHIPPING_OPTION_RESPONSE,
    VARIANT_RESPONSE,
    load,
)
from youreapyousow.domain import (
    BUDGET_CURRENCY,
    COMMITTED_STATES,
    Availability,
    CatalogueItem,
    CatalogueVariant,
    Comparator,
    Constraint,
    Enrolment,
    IntentState,
    LandedQuote,
    Price,
    PurchaseIntent,
    PurchaseTerms,
    ResourceSpec,
    committed_amount,
    usd,
)
from youreapyousow.reap.models import Enrollment as WireEnrollment
from youreapyousow.reap.models import (
    EnrollmentStatus,
    ProductSearchResponse,
    Quote,
    Variant,
)


@pytest.mark.parametrize(
    ("comparator", "value", "expected"),
    [
        (Comparator.LT, 499, True),
        (Comparator.LT, 500, False),
        (Comparator.LE, 500, True),
        (Comparator.GT, 500, False),
        (Comparator.GE, 500, True),
    ],
)
def test_constraint_comparators(comparator: Comparator, value: int, expected: bool) -> None:
    """Each comparator treats the threshold boundary correctly."""
    constraint = Constraint(metric="p95", comparator=comparator, threshold=Decimal(500))
    assert constraint.is_met(Decimal(value)) is expected


def test_usd_rounds_half_up_to_cents() -> None:
    """Money is quantised to cents, half up."""
    assert usd("1.005") == Decimal("1.01")
    assert usd(2) == Decimal("2.00")


def test_resource_spec_matching() -> None:
    """A spec filters by count, memory, price, availability, GPU name and region."""
    spec = ResourceSpec(
        min_vram_gb=24,
        max_price_usd_per_hour=Decimal(1),
        gpu_names=("rtx 4090",),
        regions=("us",),
    )
    assert spec.matches(make_offer())
    assert not spec.matches(make_offer(vram_gb=16))
    assert not spec.matches(make_offer(price="1.01"))
    assert not spec.matches(make_offer(gpu_name="A6000"))
    assert not spec.matches(make_offer(region="Montreal, CA"))
    assert not spec.matches(make_offer(region=None))
    assert not spec.matches(make_offer(availability=Availability.UNAVAILABLE))
    assert not spec.matches(make_offer().model_copy(update={"gpu_count": 2}))
    assert ResourceSpec().matches(make_offer(region=None))


def test_offer_key() -> None:
    """An offer's key joins provider and offer id."""
    assert make_offer(provider="runpod", offer_id="x").key == "runpod:x"


@pytest.mark.parametrize(
    ("state", "settled", "expected"),
    [
        (IntentState.PROPOSED, None, "0"),
        (IntentState.REFUSED, None, "0"),
        (IntentState.DECLINED, None, "0"),
        (IntentState.EXECUTING, None, "1.46"),
        (IntentState.OUTCOME_UNKNOWN, None, "1.46"),
        (IntentState.AUTHORISED, None, "1.46"),
        (IntentState.SETTLED, "0.40", "0.40"),
        (IntentState.AWAITING_APPROVAL, None, "1.46"),
        (IntentState.COMPLETED, None, "1.46"),
        (IntentState.FAILED, None, "0"),
        (IntentState.EXPIRED, None, "0"),
    ],
)
def test_committed_amount_by_state(state: IntentState, settled: str | None, expected: str) -> None:
    """Only money that may have moved counts, and settled spend at what cleared."""
    intent = PurchaseIntent(
        id="i",
        objective_id="o",
        quote_id="q",
        provider="vast",
        offer_id="1",
        amount_usd=Decimal("1.46"),
        rationale="r",
        created_at=START,
        state=state,
        settled_usd=Decimal(settled) if settled else None,
    )
    assert intent.committed_usd == Decimal(expected)


def _variant() -> CatalogueVariant:
    return CatalogueVariant.from_reap(
        Variant.model_validate(load(VARIANT_RESPONSE)),
        product_id="<product-id>",
        merchant="<merchant-name>",
    )


def _landed(text: str = QUOTE_RESPONSE) -> LandedQuote:
    return LandedQuote.from_reap(
        Quote.model_validate(load(text)),
        quote_id="lq_1",
        objective_id="obj_1",
        need_id="need_1",
        attempt=1,
        variant=_variant(),
        quantity=1,
        idempotency_key="quo:need_1:<variant-id>:1",
        created_at=START,
    )


@pytest.mark.parametrize(
    ("state", "final", "expected"),
    [
        (IntentState.COMPLETED, "1.40", "1.40"),
        (IntentState.COMPLETED, "1.52", "1.52"),
        (IntentState.COMPLETED, None, "1.46"),
        (IntentState.AWAITING_APPROVAL, None, "1.46"),
        (IntentState.EXPIRED, None, "0"),
    ],
)
def test_a_completed_checkout_counts_at_its_final_amount(
    state: IntentState, final: str | None, expected: str
) -> None:
    """The quoted amount counts until a completed checkout's final amount replaces it."""
    intent = PurchaseIntent(
        id="i",
        objective_id="o",
        quote_id="q",
        provider="<merchant-name>",
        offer_id="<variant-id>",
        amount_usd=Decimal("1.46"),
        rationale="r",
        created_at=START,
        state=state,
        final_amount_usd=Decimal(final) if final else None,
    )
    assert intent.committed_usd == Decimal(expected)
    assert committed_amount(
        state, Decimal("1.46"), final=Decimal(final) if final else None
    ) == Decimal(expected)


def test_committed_states_are_the_agentic_set_plus_the_dormant_card_states() -> None:
    """Executing, awaiting approval, completed and outcome unknown count; so do the card's."""
    assert {
        IntentState.EXECUTING,
        IntentState.AWAITING_APPROVAL,
        IntentState.COMPLETED,
        IntentState.OUTCOME_UNKNOWN,
    } <= COMMITTED_STATES
    assert COMMITTED_STATES - {
        IntentState.EXECUTING,
        IntentState.AWAITING_APPROVAL,
        IntentState.COMPLETED,
        IntentState.OUTCOME_UNKNOWN,
    } == {IntentState.AUTHORISED, IntentState.SETTLED}
    for state in (IntentState.REFUSED, IntentState.FAILED, IntentState.EXPIRED):
        assert state not in COMMITTED_STATES


def test_a_landed_quote_keeps_reaps_final_amount_as_sent() -> None:
    """The guide's own example leaves tax out of its total; the quote is not recomputed."""
    quote = _landed()
    assert quote.final_amount == Price(amount=Decimal(142), currency="USD")
    breakdown = quote.breakdown
    assert breakdown.items_subtotal.amount == Decimal(129)
    assert breakdown.shipping == Price(amount=Decimal(13), currency="USD")
    assert breakdown.tax == Price(amount=Decimal(5), currency="USD")
    assert breakdown.tax_included_in_prices is False
    assert (breakdown.discounts, breakdown.additional_charges) == ((), ())
    assert quote.reap_quote_id == "<quote-id>"
    assert quote.expires_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert quote.shipping is not None
    assert (quote.shipping.id, quote.shipping.name) == ("<express-option-id>", "Express")
    assert quote.merchant == "<merchant-name>"


def test_a_repriced_quote_reads_its_new_final_amount_and_shipping() -> None:
    """After a shipping change the breakdown omits discounts; the new total stands."""
    quote = _landed(SHIPPING_OPTION_RESPONSE)
    assert quote.final_amount.amount == Decimal(134)
    assert quote.shipping is not None
    assert quote.shipping.name == "Standard"
    assert quote.breakdown.discounts == ()


def test_a_landed_quote_offers_the_terms_the_gate_decides_on() -> None:
    """Merchant, variant, quantity, final amount, currency and expiry, from Reap's quote."""
    assert _landed().terms == PurchaseTerms(
        quote_id="lq_1",
        objective_id="obj_1",
        merchant="<merchant-name>",
        item="<variant-id>",
        quantity=1,
        amount=Decimal(142),
        currency="USD",
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_a_lease_quote_offers_the_same_terms() -> None:
    """A compute lease is one item from its provider, priced in the budget's currency."""
    quote = make_quote()
    assert quote.terms == PurchaseTerms(
        quote_id="quo_1",
        objective_id="obj_1",
        merchant="vast",
        item="52727526",
        quantity=1,
        amount=Decimal("1.46"),
        currency=BUDGET_CURRENCY,
        expires_at=START + timedelta(seconds=60),
    )


def test_a_quote_expiry_without_a_zone_is_refused() -> None:
    """An expiry the gate could misread by hours is rejected, never guessed."""
    raw = load(QUOTE_RESPONSE) | {"expiresAt": "2026-01-01T00:00:00"}
    with pytest.raises(ValueError, match="time zone"):
        LandedQuote.from_reap(
            Quote.model_validate(raw),
            quote_id="lq_1",
            objective_id="obj_1",
            need_id="need_1",
            attempt=1,
            variant=_variant(),
            quantity=1,
            idempotency_key="k",
            created_at=START,
        )


def test_a_landed_quote_needs_a_positive_quantity_and_attempt() -> None:
    """Quantity and attempt numbers start at one."""
    quote = _landed()
    with pytest.raises(ValidationError, match="quantity"):
        LandedQuote.model_validate(quote.model_dump() | {"quantity": 0})
    with pytest.raises(ValidationError, match="attempt"):
        LandedQuote.model_validate(quote.model_dump() | {"attempt": 0})


def test_a_resolved_variant_keeps_its_options_by_name() -> None:
    """Option names map to values so a need can be checked attribute by attribute."""
    variant = _variant()
    assert variant.id == "<variant-id>"
    assert variant.options == {"Color": "Black"}
    assert variant.price == Price(amount=Decimal(129), currency="USD")
    assert variant.requires_shipping is True
    assert variant.key == "<merchant-name>:<variant-id>"


def test_a_search_result_becomes_a_catalogue_item() -> None:
    """Each product keeps its merchant, price range and previewed variant."""
    response = ProductSearchResponse.model_validate(load(SEARCH_RESPONSE))
    item = CatalogueItem.from_reap(response.products[0], search_id=response.id)
    assert item.search_id == "<search-id>"
    assert (item.product_id, item.merchant) == ("<product-id>", "<merchant-name>")
    assert (item.price_min.amount, item.price_max.amount) == (Decimal(129), Decimal(149))
    assert item.preview_variant_id == "<variant-id>"
    assert item.available is True


def test_an_enrolment_as_last_read() -> None:
    """The enrolment keeps its status and the card's network and last four, never the PAN."""
    enrolment = Enrolment.from_reap(
        WireEnrollment.model_validate(load(ENROLLMENT_RESPONSE)), read_at=START
    )
    assert enrolment.status == EnrollmentStatus.ACTIVE
    assert enrolment.is_active
    assert (enrolment.network, enrolment.last4) == ("<network>", "4242")
    assert not enrolment.model_copy(update={"status": EnrollmentStatus.REVOKED}).is_active

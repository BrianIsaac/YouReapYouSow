"""Builders for test records, with the demo's figures as defaults."""

from datetime import datetime, timedelta
from decimal import Decimal

from tests.conftest import START
from youreapyousow.authority.grants import AuthorityGrant
from youreapyousow.domain import (
    Availability,
    BillingGranularity,
    CatalogueVariant,
    Comparator,
    Constraint,
    Enrolment,
    LandedQuote,
    Objective,
    ObjectiveKind,
    Offer,
    Price,
    Quote,
    QuoteBreakdown,
    ReapBinding,
    ShippingChoice,
    Source,
)
from youreapyousow.reap.models import EnrollmentStatus

MERCHANT = "Northwind Parts"
OTHER_MERCHANT = "Contoso Hardware"
VARIANT_ID = "var_nvme_1tb"


def make_offer(
    *,
    provider: str = "vast",
    offer_id: str = "52727526",
    price: str = "0.73",
    vram_gb: int = 24,
    gpu_name: str = "RTX 4090",
    region: str | None = "California, US",
    availability: Availability = Availability.AVAILABLE,
    source: Source = Source.MOCK,
) -> Offer:
    """Build an offer.

    Returns:
        The offer.
    """
    return Offer(
        provider=provider,
        offer_id=offer_id,
        gpu_name=gpu_name,
        gpu_count=1,
        vram_gb=vram_gb,
        price_usd_per_hour=Decimal(price),
        billing_granularity=BillingGranularity.PER_SECOND,
        region=region,
        availability=availability,
        source=source,
        fetched_at=START,
        latency_ms=None,
        raw_ref="sha256:test#0",
    )


def make_enrolment(
    *, enrolment_id: str = "enr_1", status: EnrollmentStatus = EnrollmentStatus.ACTIVE
) -> Enrolment:
    """Build an agentic enrolment holding a published test card.

    Returns:
        The enrolment.
    """
    return Enrolment(id=enrolment_id, status=status, network="VISA", last4="4242", read_at=START)


def make_objective(
    *,
    objective_id: str = "obj_1",
    budget: str = "25",
    card_id: str | None = "card_1",
    enrolment: Enrolment | None = None,
) -> Objective:
    """Build the service-fleet objective: p95 under 500 ms on a $25 budget.

    Returns:
        The objective.
    """
    return Objective(
        id=objective_id,
        kind=ObjectiveKind.SERVICE_FLEET,
        statement="Maintain p95 latency below 500 ms",
        constraints=[
            Constraint(metric="p95_latency_ms", comparator=Comparator.LT, threshold=Decimal(500))
        ],
        budget_usd=Decimal(budget),
        created_at=START,
        reap=ReapBinding(user_id="user_1", account_id="acct_1", card_id=card_id)
        if card_id
        else None,
        enrolment=enrolment,
    )


def make_part_objective(*, objective_id: str = "obj_1", budget: str = "500") -> Objective:
    """Build shape (b)'s objective: keep the node in service on a $500 repair budget.

    Returns:
        The objective, bound to an active enrolment and no card.
    """
    return make_objective(
        objective_id=objective_id, budget=budget, card_id=None, enrolment=make_enrolment()
    )


def make_grant(
    *,
    objective_id: str = "obj_1",
    grant_id: str = "grt_1",
    providers: tuple[str, ...] = ("vast", "runpod", "shadeform"),
    per_tx: str = "5",
    daily: str = "20",
    max_hourly: str | None = "1.00",
    approval: str | None = None,
    issued_at: datetime = START,
    ttl: timedelta = timedelta(hours=4),
    merchants: tuple[str, ...] = (),
    attempts: int = 3,
    margin_s: int = 15,
) -> AuthorityGrant:
    """Build a grant.

    Returns:
        The grant.
    """
    return AuthorityGrant(
        id=grant_id,
        objective_id=objective_id,
        allowed_providers=list(providers),
        allowed_merchants=list(merchants),
        attempts_per_need=attempts,
        quote_margin_s=margin_s,
        per_transaction_cap_usd=Decimal(per_tx),
        daily_cap_usd=Decimal(daily),
        max_price_usd_per_hour=Decimal(max_hourly) if max_hourly else None,
        approval_threshold_usd=Decimal(approval) if approval else None,
        issued_at=issued_at,
        expires_at=issued_at + ttl,
    )


def make_quote(
    *,
    quote_id: str = "quo_1",
    objective_id: str = "obj_1",
    offer: Offer | None = None,
    hours: str = "2",
    created_at: datetime = START,
    ttl: timedelta = timedelta(seconds=60),
) -> Quote:
    """Build a quote whose amount is the offer's price times the hours.

    Returns:
        The quote.
    """
    offer = offer or make_offer()
    return Quote(
        id=quote_id,
        objective_id=objective_id,
        offer=offer,
        hours=Decimal(hours),
        amount_usd=(offer.price_usd_per_hour * Decimal(hours)).quantize(Decimal("0.01")),
        created_at=created_at,
        expires_at=created_at + ttl,
    )


def make_part_grant(
    *,
    grant_id: str = "grt_1",
    merchants: tuple[str, ...] = (MERCHANT, OTHER_MERCHANT),
    per_tx: str = "150",
    daily: str = "300",
    approval: str | None = None,
    attempts: int = 3,
    margin_s: int = 15,
    issued_at: datetime = START,
) -> AuthorityGrant:
    """Build shape (b)'s grant, from the ``configs/purchase-part.yaml`` figures.

    Returns:
        The grant, scoped to merchants and to no compute provider.
    """
    return make_grant(
        grant_id=grant_id,
        providers=(),
        per_tx=per_tx,
        daily=daily,
        max_hourly=None,
        approval=approval,
        merchants=merchants,
        attempts=attempts,
        margin_s=margin_s,
        issued_at=issued_at,
    )


def make_variant(
    *,
    variant_id: str = VARIANT_ID,
    merchant: str = MERCHANT,
    options: dict[str, str] | None = None,
    price: str = "119.00",
) -> CatalogueVariant:
    """Build a resolved catalogue variant: a 1 TB NVMe drive.

    Returns:
        The variant.
    """
    return CatalogueVariant(
        id=variant_id,
        product_id="prod_nvme",
        merchant=merchant,
        name="1 TB",
        options=options or {"Capacity": "1 TB", "Interface": "NVMe"},
        price=Price(amount=Decimal(price), currency="USD"),
        available=True,
        requires_shipping=True,
    )


def make_landed_quote(
    *,
    quote_id: str = "lq_1",
    objective_id: str = "obj_1",
    need_id: str = "need_1",
    attempt: int = 1,
    variant: CatalogueVariant | None = None,
    quantity: int = 1,
    final: str = "142.00",
    currency: str = "USD",
    created_at: datetime = START,
    ttl: timedelta = timedelta(seconds=120),
) -> LandedQuote:
    """Build a landed quote: subtotal, shipping and tax, with Reap's final amount as given.

    Returns:
        The landed quote.
    """
    variant = variant or make_variant()

    def price(amount: str) -> Price:
        return Price(amount=Decimal(amount), currency=currency)

    return LandedQuote(
        id=quote_id,
        reap_quote_id="5f0c2a4e-8d1b-4f6a-9c3e-2b7d1e0a9f41",
        objective_id=objective_id,
        need_id=need_id,
        attempt=attempt,
        variant=variant,
        quantity=quantity,
        breakdown=QuoteBreakdown(
            items_subtotal=price("119.00"),
            shipping=price("13.00"),
            tax=price("10.00"),
            tax_included_in_prices=False,
            final_amount=price(final),
        ),
        shipping=ShippingChoice(id="ship_express", name="Express", price=price("13.00")),
        idempotency_key=f"quo:{need_id}:{variant.id}:{attempt}",
        created_at=created_at,
        expires_at=created_at + ttl,
    )

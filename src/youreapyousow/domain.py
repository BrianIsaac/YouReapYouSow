"""The minimal data model shared across the control plane.

Money is ``Decimal`` in US dollars throughout; nothing on the authority path uses
floating point. Reap's agentic amounts carry their own currency (``Price``); the gate
refuses any whose currency is not the budget's, so every budget figure stays in USD.

The agentic purchase adds the catalogue as Reap returns it (``CatalogueItem``,
``CatalogueVariant``), the quote whose landed ``finalAmount`` the gate decides on
(``LandedQuote``), the checkout that follows (``Order``) and the objective's agentic
enrolment (``Enrolment``). Each ``from_reap`` reads the wire models and copies Reap's
figures as sent: nothing is recomputed, so a breakdown whose parts do not sum to its
``finalAmount`` (as in Reap's own guide examples) keeps Reap's total.

A choice made here: an objective's enrolment sits beside its dormant card binding
(``Objective.enrolment``) rather than inside ``ReapBinding``, so the card fields stay
required for the card path.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from youreapyousow.reap import models as wire

CENT = Decimal("0.01")

BUDGET_CURRENCY = "USD"
"""The currency of every budget, cap and runway figure."""


def usd(value: Decimal | int | str) -> Decimal:
    """Round an amount to whole cents, half up.

    Args:
        value: An amount in US dollars.

    Returns:
        The amount quantised to cents.
    """
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class ObjectiveKind(StrEnum):
    """The two scenarios the one control plane serves."""

    SERVICE_FLEET = "service_fleet"
    ML_SERVICE = "ml_service"


class ObjectiveStatus(StrEnum):
    """Whether an objective is still being pursued."""

    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Comparator(StrEnum):
    """How a constraint compares an observed value with its threshold."""

    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="


class Constraint(BaseModel):
    """One operational constraint, such as ``p95_latency_ms < 500``.

    Attributes:
        metric: The observed metric's name.
        comparator: How the value must relate to the threshold.
        threshold: The bound.
        unit: Unit of the metric, for display.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    comparator: Comparator
    threshold: Decimal
    unit: str = ""

    def is_met(self, value: Decimal) -> bool:
        """Check an observed value against the constraint.

        Args:
            value: The observed value of ``metric``.

        Returns:
            True when the constraint holds.
        """
        match self.comparator:
            case Comparator.LT:
                return value < self.threshold
            case Comparator.LE:
                return value <= self.threshold
            case Comparator.GT:
                return value > self.threshold
            case Comparator.GE:
                return value >= self.threshold


class ReapBinding(BaseModel):
    """The Reap objects that carry an objective's financial authority.

    Attributes:
        user_id: The KYC-approved cardholder of record (the human operator).
        account_id: The account holding the spending balance.
        card_id: The objective's virtual card; one card per objective.
        policy_ids: The floor policies attached to the card.
    """

    user_id: str
    account_id: str
    card_id: str
    policy_ids: list[str] = Field(default_factory=list[str])


def _aware(timestamp: str) -> datetime:
    """Read one of Reap's ISO 8601 timestamps, refusing any without a time zone.

    Args:
        timestamp: For example ``2030-01-01T00:00:00Z``.

    Returns:
        The aware moment.

    Raises:
        ValueError: If the text is not ISO 8601 or names no time zone.
    """
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp {timestamp!r} has no time zone")
    return moment


class Price(BaseModel):
    """An amount in a named currency, as Reap's agentic operations quote it.

    Attributes:
        amount: The amount, exactly as sent.
        currency: The three-letter currency code.
    """

    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)

    @classmethod
    def from_reap(cls, money: wire.Money) -> "Price":
        """Copy one of Reap's amounts.

        Args:
            money: The wire amount.

        Returns:
            The same amount and currency.
        """
        return cls(amount=money.amount, currency=money.currency)


class NamedPrice(BaseModel):
    """A discount or an additional charge on a quote.

    Attributes:
        name: What it is.
        price: How much.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    price: Price


class Enrolment(BaseModel):
    """The objective's agentic enrolment as last read from Reap.

    The card behind it is shown by network and last four only; the agent never sees more.

    Attributes:
        id: Reap's enrolment id.
        status: Its status when last read.
        network: The card network, once a card is captured.
        last4: The card's last four digits, once a card is captured.
        read_at: When it was last read.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    status: wire.EnrollmentStatus
    network: str | None = None
    last4: str | None = None
    read_at: datetime

    @property
    def is_active(self) -> bool:
        """Whether purchases may be charged to it.

        Returns:
            True only when Reap last reported it ``ACTIVE``.
        """
        return self.status == wire.EnrollmentStatus.ACTIVE

    @classmethod
    def from_reap(cls, enrollment: wire.Enrollment, *, read_at: datetime) -> "Enrolment":
        """Record an enrolment read with ``GET /agentic/enrollments/{id}``.

        Args:
            enrollment: The wire enrolment.
            read_at: When it was read.

        Returns:
            The enrolment.
        """
        card = enrollment.payment_method
        return cls(
            id=enrollment.id,
            status=enrollment.status,
            network=card.network if card else None,
            last4=card.last4 if card else None,
            read_at=read_at,
        )


class Objective(BaseModel):
    """What a service is asked to achieve, and with how much money.

    Attributes:
        id: Identifier.
        kind: Which scenario it belongs to.
        statement: Human-readable objective.
        constraints: Operational constraints that define success.
        budget_usd: Total money the objective may commit across all purchases.
        status: Whether it is still active.
        created_at: When it was created.
        reap: The Reap card and account behind it, once set up (card path, dormant).
        enrolment: The agentic enrolment purchases are charged to, as last read.
    """

    id: str
    kind: ObjectiveKind
    statement: str
    constraints: list[Constraint]
    budget_usd: Decimal
    status: ObjectiveStatus = ObjectiveStatus.ACTIVE
    created_at: datetime
    reap: ReapBinding | None = None
    enrolment: Enrolment | None = None


class ResourceKind(StrEnum):
    """Kinds of resource the market offers."""

    GPU_COMPUTE = "gpu_compute"
    MODEL_API = "model_api"


class Availability(StrEnum):
    """Normalised stock level of an offer."""

    AVAILABLE = "available"
    LOW = "low"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class Source(StrEnum):
    """Where a piece of market data came from, shown as a badge on every quote."""

    LIVE = "live"
    CACHED = "cached"
    MOCK = "mock"


class BillingGranularity(StrEnum):
    """How finely a provider meters a lease."""

    PER_SECOND = "per_second"
    PER_MINUTE = "per_minute"
    PER_HOUR = "per_hour"


class Offer(BaseModel):
    """A rentable resource on the market, in one shape whatever the provider.

    Attributes:
        provider: Provider name, such as ``vast``.
        offer_id: The provider's own identifier for the offer or instance type.
        kind: The resource kind.
        gpu_name: Normalised GPU model name.
        gpu_count: GPUs in the offer.
        vram_gb: Memory per GPU in GB.
        price_usd_per_hour: Price for the whole offer per hour, in USD.
        billing_granularity: How the provider meters usage.
        region: Location, when the provider states one.
        availability: Normalised stock level.
        source: Live, cached or mock.
        fetched_at: When the underlying response was fetched.
        latency_ms: How long the fetch took, when it was live.
        raw_ref: ``sha256:<digest>#<index>`` of the raw response, for provenance.
        boot_seconds: Expected time to boot, when the provider says.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    offer_id: str
    kind: ResourceKind = ResourceKind.GPU_COMPUTE
    gpu_name: str
    gpu_count: int
    vram_gb: int
    price_usd_per_hour: Decimal
    billing_granularity: BillingGranularity
    region: str | None
    availability: Availability
    source: Source
    fetched_at: datetime
    latency_ms: int | None
    raw_ref: str
    boot_seconds: int | None = None

    @property
    def key(self) -> str:
        """Return the offer's globally unique key.

        Returns:
            ``<provider>:<offer_id>``.
        """
        return f"{self.provider}:{self.offer_id}"


class ResourceSpec(BaseModel):
    """What the service needs; the argument to ``discover_resources``.

    Attributes:
        kind: Resource kind wanted.
        gpu_count: Exact number of GPUs.
        min_vram_gb: Minimum memory per GPU.
        max_price_usd_per_hour: Price ceiling, if any.
        regions: Acceptable regions (substring match); empty means any.
        gpu_names: Acceptable GPU models (case-insensitive); empty means any.
    """

    model_config = ConfigDict(frozen=True)

    kind: ResourceKind = ResourceKind.GPU_COMPUTE
    gpu_count: int = 1
    min_vram_gb: int = 0
    max_price_usd_per_hour: Decimal | None = None
    regions: tuple[str, ...] = ()
    gpu_names: tuple[str, ...] = ()

    def matches(self, offer: Offer) -> bool:
        """Decide whether an offer satisfies the spec.

        Args:
            offer: A normalised offer from any source.

        Returns:
            True when the offer fits every requirement and is not out of stock.
        """
        if offer.kind != self.kind or offer.gpu_count != self.gpu_count:
            return False
        if offer.vram_gb < self.min_vram_gb or offer.availability == Availability.UNAVAILABLE:
            return False
        if (
            self.max_price_usd_per_hour is not None
            and offer.price_usd_per_hour > self.max_price_usd_per_hour
        ):
            return False
        if self.gpu_names and offer.gpu_name.lower() not in {n.lower() for n in self.gpu_names}:
            return False
        region = (offer.region or "").lower()
        return not self.regions or any(r.lower() in region for r in self.regions)


class MarketReference(BaseModel):
    """A reference price from a market index; never a purchasable offer.

    Attributes:
        source_name: Which index, such as ``akash`` or ``ornn``.
        gpu: GPU model the figure describes.
        statistic: What the figure is, such as ``median`` or ``daily_index``.
        usd_per_gpu_hour: The figure.
        as_of: Timestamp of the figure.
        source: Live, cached or mock.
    """

    model_config = ConfigDict(frozen=True)

    source_name: str
    gpu: str
    statistic: str
    usd_per_gpu_hour: Decimal
    as_of: datetime
    source: Source


class ModelOffer(BaseModel):
    """A model API offer for the ML-service scenario.

    Attributes:
        provider: Where the model is sold, such as ``openrouter``.
        model_id: The provider's model identifier.
        name: Display name.
        prompt_usd_per_mtok: Input price per million tokens.
        completion_usd_per_mtok: Output price per million tokens.
        context_length: Maximum context, when stated.
        source: Live, cached or mock.
        fetched_at: When the list was fetched.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model_id: str
    name: str
    prompt_usd_per_mtok: Decimal
    completion_usd_per_mtok: Decimal
    context_length: int | None
    source: Source
    fetched_at: datetime


class PurchaseTerms(BaseModel):
    """What a quote commits to, in the one shape every authority rule reads.

    Both a compute lease (``Quote``) and a Reap catalogue quote (``LandedQuote``)
    provide it, so the rules that guard money do not care which was bought.

    Attributes:
        quote_id: The quote's identifier.
        objective_id: The objective it serves.
        merchant: Who is paid: the provider of a lease, Reap's merchant name otherwise.
        item: What is bought: the provider's offer id, or Reap's variant id.
        quantity: How many.
        amount: The amount to be charged: a lease's price times hours, or a catalogue
            quote's landed ``finalAmount``.
        currency: The amount's currency.
        expires_at: After this the quote may not be bought.
    """

    model_config = ConfigDict(frozen=True)

    quote_id: str
    objective_id: str
    merchant: str
    item: str
    quantity: int
    amount: Decimal
    currency: str
    expires_at: datetime


class Quote(BaseModel):
    """A committed price for leasing one offer for a set time.

    Attributes:
        id: Identifier.
        objective_id: The objective the quote serves.
        offer: A frozen copy of the offer as it was when quoted.
        hours: Lease length.
        amount_usd: Price times hours, in whole cents; the amount to authorise.
        created_at: When it was quoted.
        expires_at: After this the quote may not be purchased.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    objective_id: str
    offer: Offer
    hours: Decimal
    amount_usd: Decimal
    created_at: datetime
    expires_at: datetime

    @property
    def terms(self) -> PurchaseTerms:
        """Return the lease as the terms the rules read.

        Returns:
            One lease of the offer from its provider, in the budget's currency.
        """
        return PurchaseTerms(
            quote_id=self.id,
            objective_id=self.objective_id,
            merchant=self.offer.provider,
            item=self.offer.offer_id,
            quantity=1,
            amount=self.amount_usd,
            currency=BUDGET_CURRENCY,
            expires_at=self.expires_at,
        )


class CatalogueItem(BaseModel):
    """One product a Reap catalogue search returned.

    Attributes:
        product_id: Reap's product id.
        search_id: The search that returned it.
        merchant: The merchant's name.
        name: The product's name.
        price_min: Its cheapest variant's price.
        price_max: Its dearest variant's price.
        available: Whether it is in stock, when stated.
        preview_variant_id: The variant the search previewed, if any.
        image_url: Its image, if any.
    """

    model_config = ConfigDict(frozen=True)

    product_id: str
    search_id: str
    merchant: str
    name: str
    price_min: Price
    price_max: Price
    available: bool | None = None
    preview_variant_id: str | None = None
    image_url: str | None = None

    @classmethod
    def from_reap(cls, product: wire.ProductSummary, *, search_id: str) -> "CatalogueItem":
        """Record one result of ``POST /agentic/products/search``.

        Args:
            product: The wire search result.
            search_id: Reap's id for the search.

        Returns:
            The catalogue item.
        """
        return cls(
            product_id=product.id,
            search_id=search_id,
            merchant=product.merchant.name,
            name=product.name,
            price_min=Price.from_reap(product.price_range.min),
            price_max=Price.from_reap(product.price_range.max),
            available=product.available,
            preview_variant_id=product.preview_variant.id if product.preview_variant else None,
            image_url=product.image_url,
        )


class CatalogueVariant(BaseModel):
    """A purchasable variant, the only id Reap accepts on a quote.

    Attributes:
        id: Reap's variant id.
        product_id: The product it belongs to.
        merchant: The merchant's name.
        name: The variant's name, when given.
        options: Its options by name, such as ``{"Capacity": "1 TB"}``; what a need's
            attributes are checked against.
        price: Its unit price before shipping and tax; never what the gate decides on.
        available: Whether it is in stock, when stated.
        requires_shipping: Whether a quote for it needs a shipping address, when stated.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    product_id: str
    merchant: str
    name: str | None = None
    options: dict[str, str] = Field(default_factory=dict[str, str])
    price: Price
    available: bool | None = None
    requires_shipping: bool | None = None

    @property
    def key(self) -> str:
        """Return the variant's key across merchants.

        Returns:
            ``<merchant>:<variant id>``.
        """
        return f"{self.merchant}:{self.id}"

    @classmethod
    def from_reap(
        cls, variant: wire.Variant, *, product_id: str, merchant: str
    ) -> "CatalogueVariant":
        """Record a variant from ``POST /agentic/products/variant`` or a default variant.

        Args:
            variant: The wire variant.
            product_id: The product it was resolved from.
            merchant: The product's merchant.

        Returns:
            The variant.
        """
        return cls(
            id=variant.id,
            product_id=product_id,
            merchant=merchant,
            name=variant.name,
            options={o.name: o.value for o in variant.options or []},
            price=Price.from_reap(variant.price),
            available=variant.available,
            requires_shipping=variant.requires_shipping,
        )


class QuoteBreakdown(BaseModel):
    """Reap's itemised total, kept as sent.

    Attributes:
        items_subtotal: The items before shipping, tax, discounts and charges.
        shipping: Shipping, when charged.
        tax: Tax, when stated.
        tax_included_in_prices: Whether the item prices already include the tax.
        discounts: Discounts applied.
        additional_charges: Other charges.
        final_amount: The landed total: what the gate decides on and the checkout charges.
    """

    model_config = ConfigDict(frozen=True)

    items_subtotal: Price
    shipping: Price | None = None
    tax: Price | None = None
    tax_included_in_prices: bool | None = None
    discounts: tuple[NamedPrice, ...] = ()
    additional_charges: tuple[NamedPrice, ...] = ()
    final_amount: Price

    @classmethod
    def from_reap(cls, breakdown: wire.AmountBreakdown) -> "QuoteBreakdown":
        """Copy a quote's ``amountBreakdown``.

        Args:
            breakdown: The wire breakdown.

        Returns:
            The breakdown, figure for figure.
        """
        return cls(
            items_subtotal=Price.from_reap(breakdown.items_subtotal),
            shipping=Price.from_reap(breakdown.shipping) if breakdown.shipping else None,
            tax=Price.from_reap(breakdown.tax.amount) if breakdown.tax else None,
            tax_included_in_prices=breakdown.tax.included_in_prices if breakdown.tax else None,
            discounts=tuple(
                NamedPrice(name=d.name, price=Price.from_reap(d.amount))
                for d in breakdown.discounts or []
            ),
            additional_charges=tuple(
                NamedPrice(name=c.name, price=Price.from_reap(c.amount))
                for c in breakdown.additional_charges or []
            ),
            final_amount=Price.from_reap(breakdown.final_amount),
        )


class ShippingChoice(BaseModel):
    """The shipping option selected on a quote.

    Attributes:
        id: Reap's shipping option id.
        name: Its name, such as ``Standard``.
        price: Its price.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    price: Price


class LandedQuote(BaseModel):
    """A Reap quote for one catalogue variant: the landed price the gate decides on.

    A re-priced quote (a different shipping option) is a new ``LandedQuote`` with the
    same ``reap_quote_id``, so an intent proposed on the old price no longer matches.

    Attributes:
        id: Our identifier for this priced reading of the quote.
        reap_quote_id: Reap's quote id, sent as ``quoteId`` on the checkout.
        objective_id: The objective it serves.
        need_id: The need it would fill.
        attempt: The attempt at the need this quote belongs to, counting from one.
        variant: The variant quoted.
        quantity: How many.
        breakdown: Reap's itemised total.
        shipping: The selected shipping option, if the quote offers any.
        idempotency_key: The key the quote was created under.
        created_at: When it landed.
        expires_at: Reap's ``expiresAt``.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    reap_quote_id: str
    objective_id: str
    need_id: str
    attempt: int = Field(ge=1)
    variant: CatalogueVariant
    quantity: int = Field(gt=0)
    breakdown: QuoteBreakdown
    shipping: ShippingChoice | None = None
    idempotency_key: str
    created_at: datetime
    expires_at: datetime

    @property
    def merchant(self) -> str:
        """Return the merchant the quote is from.

        Returns:
            The variant's merchant name.
        """
        return self.variant.merchant

    @property
    def final_amount(self) -> Price:
        """Return the landed total.

        Returns:
            ``amountBreakdown.finalAmount`` as Reap sent it.
        """
        return self.breakdown.final_amount

    @property
    def terms(self) -> PurchaseTerms:
        """Return the quote as the terms the rules read.

        Returns:
            The variant from its merchant at the landed ``finalAmount``.
        """
        return PurchaseTerms(
            quote_id=self.id,
            objective_id=self.objective_id,
            merchant=self.merchant,
            item=self.variant.id,
            quantity=self.quantity,
            amount=self.final_amount.amount,
            currency=self.final_amount.currency,
            expires_at=self.expires_at,
        )

    @classmethod
    def from_reap(
        cls,
        quote: wire.Quote,
        *,
        quote_id: str,
        objective_id: str,
        need_id: str,
        attempt: int,
        variant: CatalogueVariant,
        quantity: int,
        idempotency_key: str,
        created_at: datetime,
    ) -> "LandedQuote":
        """Record a quote from ``POST /agentic/quotes`` or its shipping-option change.

        Args:
            quote: The wire quote.
            quote_id: Our identifier for this reading.
            objective_id: The objective it serves.
            need_id: The need it would fill.
            attempt: The attempt at the need, from one.
            variant: The variant that was quoted.
            quantity: How many were quoted.
            idempotency_key: The key the quote was created under.
            created_at: When it landed.

        Returns:
            The landed quote.

        Raises:
            ValueError: If ``expiresAt`` names no time zone.
        """
        selected = next((o for o in quote.shipping_options if o.selected), None)
        return cls(
            id=quote_id,
            reap_quote_id=quote.id,
            objective_id=objective_id,
            need_id=need_id,
            attempt=attempt,
            variant=variant,
            quantity=quantity,
            breakdown=QuoteBreakdown.from_reap(quote.amount_breakdown),
            shipping=ShippingChoice(
                id=selected.id, name=selected.name, price=Price.from_reap(selected.price)
            )
            if selected
            else None,
            idempotency_key=idempotency_key,
            created_at=created_at,
            expires_at=_aware(quote.expires_at),
        )


class IntentState(StrEnum):
    """Lifecycle of a purchase intent through the gate.

    The agentic path runs ``executing`` (claimed; the checkout is created and then
    ``PROCESSING``), ``awaiting_approval`` (Reap's ``REQUIRES_ACTION``), and one of
    ``completed``, ``failed``, ``expired`` or ``outcome_unknown``. ``authorised``,
    ``declined`` and ``settled`` belong to the dormant card path.
    """

    PROPOSED = "proposed"
    ALLOWED = "allowed"
    ESCALATED = "escalated"
    REFUSED = "refused"
    EXECUTING = "executing"
    AUTHORISED = "authorised"
    DECLINED = "declined"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SETTLED = "settled"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    EXPIRED = "expired"


COMMITTED_STATES = frozenset(
    {
        IntentState.EXECUTING,
        IntentState.AWAITING_APPROVAL,
        IntentState.COMPLETED,
        IntentState.OUTCOME_UNKNOWN,
        IntentState.AUTHORISED,
        IntentState.SETTLED,
    }
)
"""States in which money may have moved, so the intent counts against the budget."""


def committed_amount(
    state: IntentState,
    amount: Decimal,
    *,
    settled: Decimal | None = None,
    final: Decimal | None = None,
) -> Decimal:
    """Return how much of the budget an intent consumes: the one rule every figure uses.

    Args:
        state: The intent's state.
        amount: The amount it was claimed at (a quote's landed ``finalAmount``).
        settled: What cleared, for a settled card purchase.
        final: The completed checkout's ``finalAmount``, when it is in the budget's
            currency.

    Returns:
        The completed or settled figure once known, the claimed amount while committed,
        else 0.
    """
    if state == IntentState.COMPLETED and final is not None:
        return final
    if state == IntentState.SETTLED and settled is not None:
        return settled
    if state in COMMITTED_STATES:
        return amount
    return Decimal(0)


class PurchaseIntent(BaseModel):
    """A purchase the agent wants to make, held until the gate decides.

    The same intent serves both paths. On the agentic path ``provider`` is the
    merchant's name, ``offer_id`` Reap's variant id and ``amount_usd`` the quote's
    landed ``finalAmount``; ``need_id`` is set (from the quote, never the agent) and
    marks the intent as agentic.

    Attributes:
        id: Identifier.
        objective_id: The objective it serves.
        quote_id: The quote it would buy.
        provider: Provider or merchant named by the agent; must match the quote exactly.
        offer_id: Offer or variant named by the agent; must match the quote exactly.
        amount_usd: Amount named by the agent; must match the quote exactly.
        rationale: Why the agent chose this option.
        options_considered: Keys of every offer the agent compared.
        state: Where it is in the gate.
        created_at: When it was proposed.
        decision_id: The latest policy decision about it.
        approved_by: The operator who approved an escalated intent.
        claimed_at: When it was claimed for execution; its day counts for daily caps.
        claim_event_id: The ledger event that claimed it for execution.
        idempotency_key: Key sent to Reap; derived from ids the caller cannot choose.
        reap_transaction_id: Reap's transaction id once sent.
        decline_code: Reap's decline code, if declined.
        executed_at: When the Reap call completed.
        settled_usd: Amount cleared, once settled.
        quantity: How many the agent named; must match the quote exactly.
        currency: The currency the agent named; must match the quote exactly.
        need_id: The need a catalogue purchase fills; None on the card path.
        attempt: The attempt at the need, copied from the landed quote.
        checkout_id: Reap's checkout id, once created.
        order_id: The merchant's order id, once the checkout completed.
        final_amount_usd: The completed checkout's ``finalAmount``, which replaces the
            quoted amount in every budget figure.
    """

    id: str
    objective_id: str
    quote_id: str
    provider: str
    offer_id: str
    amount_usd: Decimal
    rationale: str
    options_considered: list[str] = Field(default_factory=list[str])
    state: IntentState = IntentState.PROPOSED
    created_at: datetime
    decision_id: str | None = None
    approved_by: str | None = None
    claimed_at: datetime | None = None
    claim_event_id: str | None = None
    idempotency_key: str | None = None
    reap_transaction_id: str | None = None
    decline_code: str | None = None
    executed_at: datetime | None = None
    settled_usd: Decimal | None = None
    quantity: int = Field(default=1, gt=0)
    currency: str = BUDGET_CURRENCY
    need_id: str | None = None
    attempt: int | None = None
    checkout_id: str | None = None
    order_id: str | None = None
    final_amount_usd: Decimal | None = None

    @property
    def is_agentic(self) -> bool:
        """Whether the intent buys from Reap's catalogue rather than leasing on a card.

        Returns:
            True when it fills a need.
        """
        return self.need_id is not None

    @property
    def committed_usd(self) -> Decimal:
        """Return how much of the budget this intent consumes.

        Returns:
            The final or settled amount once known, the full amount while committed,
            else 0.
        """
        return committed_amount(
            self.state, self.amount_usd, settled=self.settled_usd, final=self.final_amount_usd
        )


class Order(BaseModel):
    """A Reap checkout and the merchant order it becomes, as last recorded.

    Attributes:
        checkout_id: Reap's checkout id.
        intent_id: The intent it executes.
        objective_id: The objective it serves.
        quote_id: The landed quote it buys (our id).
        status: The checkout's status as last read.
        quoted: The landed ``finalAmount`` the gate allowed.
        amount: The ``amount`` on the create response, when sent.
        order_id: The merchant's order reference, once completed.
        final_amount: The amount actually charged, once completed.
        approval_host: The host of Reap's approval page, when one was required.
        approval_expires_at: When that approval lapses, as Reap sent it.
        simulated: Whether the sandbox's ``X-Simulate-Checkout`` header was sent; None
            when the checkout was first seen on reconciliation and nobody can say.
        created_at: When the checkout was first recorded.
        updated_at: When it was last recorded.
    """

    checkout_id: str
    intent_id: str
    objective_id: str
    quote_id: str
    status: wire.CheckoutStatus
    quoted: Price
    amount: Price | None = None
    order_id: str | None = None
    final_amount: Price | None = None
    approval_host: str | None = None
    approval_expires_at: str | None = None
    simulated: bool | None = None
    created_at: datetime
    updated_at: datetime


class Disposition(StrEnum):
    """The gate's verdict: act, wait for a human, or never."""

    ALLOW = "allow"
    ESCALATE = "escalate"
    REFUSE = "refuse"


class DecisionPhase(StrEnum):
    """When the rules ran: on proposal, or again just before money moves."""

    PROPOSAL = "proposal"
    APPLY = "apply"


class RuleCheck(BaseModel):
    """The outcome of one rule.

    Attributes:
        rule: Stable rule identifier, such as ``amount.daily_cap``.
        passed: Whether the rule was satisfied.
        detail: The figures the rule compared.
    """

    model_config = ConfigDict(frozen=True)

    rule: str
    passed: bool
    detail: str


class PolicyDecision(BaseModel):
    """A deterministic verdict naming the rule that fired.

    Attributes:
        id: Identifier.
        intent_id: The intent decided.
        objective_id: Its objective.
        phase: Proposal or apply time.
        disposition: Allow, escalate or refuse.
        rule: The rule that decided it; ``all_rules_passed`` for a plain allow.
        reason: Human-readable explanation.
        checks: Every rule evaluated, in order, up to and including the deciding one.
        runway_before_usd: Budget remaining before this purchase.
        runway_after_usd: Budget remaining if it goes ahead.
        decided_at: When the rules ran.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    intent_id: str
    objective_id: str
    phase: DecisionPhase
    disposition: Disposition
    rule: str
    reason: str
    checks: list[RuleCheck]
    runway_before_usd: Decimal
    runway_after_usd: Decimal
    decided_at: datetime


class DeploymentStatus(StrEnum):
    """Whether a deployment is still consuming money."""

    RUNNING = "running"
    RELEASED = "released"


class Deployment(BaseModel):
    """Capacity acquired (or pre-existing) in service of an objective.

    Attributes:
        id: Identifier.
        objective_id: The objective it serves.
        intent_id: The purchase that paid for it; None for baseline capacity.
        provider: Where it runs.
        offer: The offer it was provisioned from, if purchased.
        status: Running or released.
        started_at: When it started.
        released_at: When it was released.
        protected: Baseline capacity the agent may never terminate.
        mode: ``mock`` or ``live`` provisioning.
        simulation_notice: What would really happen at this provider with a sandbox
            card, written into every mock deployment.
        release_reason: Why it was released.
    """

    id: str
    objective_id: str
    intent_id: str | None
    provider: str
    offer: Offer | None
    status: DeploymentStatus = DeploymentStatus.RUNNING
    started_at: datetime
    released_at: datetime | None = None
    protected: bool = False
    mode: str = "mock"
    simulation_notice: str = ""
    release_reason: str | None = None


class Observation(BaseModel):
    """One measured value of a metric.

    Attributes:
        id: Identifier.
        objective_id: The objective it informs.
        deployment_id: The deployment it concerns, if any.
        metric: Metric name, such as ``p95_latency_ms``.
        value: The measured value.
        unit: Unit, for display.
        at: When it was measured.
        source: ``simulated`` or ``live``.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    objective_id: str
    deployment_id: str | None = None
    metric: str
    value: Decimal
    unit: str = ""
    at: datetime
    source: str = "simulated"

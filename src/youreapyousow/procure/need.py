"""The need: what an objective wants bought, and how a candidate is judged to fill it.

A need is raised from the configuration's ``purchase:`` block when a breach (shape (a),
compute) or a fault (shape (b), a part) calls for a purchase. It carries everything the
control plane needs to buy it without reading the configuration again: how to search
Reap's catalogue (or the cart URL of the third route), how a variant is judged to match,
how many, and where the receipt and the parcel go.

The judgement is the gate's rule 10, ``item.matches_need``, and the same pure function
filters candidates before they are quoted, so nothing the agent proposes is judged by
the agent's say-so:

* ``AttributeMatch`` (shape (b)): every named option of the variant is one of the
  accepted values, the bill of materials' part or an approved equivalent.
* ``ValueMatch`` (shape (a)): the item's value, read from its unit price or from one of
  its options (``Amount: USD 100``) and multiplied by the quantity, covers the estimated
  lease and is no more than the configured multiple of it.
* The checkout-URL route: the quote is for the cart the operator configured, from the
  merchant the operator named.

Choices made here:

* Option names and values are compared with case and white space ignored, so ``1TB``
  is ``1 TB``; nothing else is normalised.
* A value read from an option must name exactly one number (thousands separated by
  commas allowed) and at most one three-letter currency code; anything else fails
  closed rather than being guessed. Without a code the variant's price currency is used.
  A value is compared only in the budget's currency, since nothing here converts.
* A cart bought through the checkout-URL route is judged by its URL and merchant only:
  what the cart holds is the operator's own vetting, since Reap's quote does not list it.
"""

import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from youreapyousow.authority.rules import NeedJudge, RuleResult
from youreapyousow.domain import BUDGET_CURRENCY, CatalogueVariant, LandedQuote, Price
from youreapyousow.reap.models import (
    Email,
    HttpsUrl,
    MerchantPreference,
    PriceFilter,
    ProductSearchRequest,
    SearchContext,
    SearchFilters,
    SearchPagination,
    ShippingAddress,
)


class Shape(StrEnum):
    """What kind of thing is bought, which decides what follows the order."""

    COMPUTE = "compute"
    """Compute, credits or SaaS: capacity is provisioned against the order (shape (a))."""
    PART = "part"
    """A physical part for the machine's own fix: a technician's ticket follows (shape (b))."""


class Route(StrEnum):
    """How the item reaches a quote."""

    CATALOGUE = "catalogue"
    """Search, details and variant, then a quote of the variant."""
    CHECKOUT_URL = "checkout_url"
    """A merchant cart URL quoted as ``externalCheckout``."""


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SearchPreference(_Config):
    """Reap's ``merchantPreference``: favour or restrict to one merchant.

    Attributes:
        mode: ``PREFER`` ranks the merchant first; ``ONLY`` returns nothing else.
        merchant_name: The merchant, as Reap names it.
    """

    mode: Literal["PREFER", "ONLY"]
    merchant_name: str = Field(min_length=1)


class SearchPlace(_Config):
    """Reap's search ``context``.

    Attributes:
        country: Where the buyer is, as a country code.
        currency: The currency results should be priced in.
    """

    country: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class PriceBand(_Config):
    """Reap's price filter; strings on the wire.

    Attributes:
        min: The lowest price, if bounded.
        max: The highest price, if bounded.
    """

    min: Decimal | None = None
    max: Decimal | None = None


class CatalogueSearch(_Config):
    """How the need is searched for in Reap's catalogue.

    Attributes:
        query: Free text, such as ``1TB NVMe M.2 2280 SSD``.
        merchant_preference: A merchant to prefer or require, if any.
        context: The buyer's country and currency, if stated.
        price: A price band, if any.
        availability: ``AVAILABLE_ONLY`` to leave out what is out of stock.
        limit: Results per page, 1 to 50 (Reap's bounds).
    """

    query: str = Field(min_length=1)
    merchant_preference: SearchPreference | None = None
    context: SearchPlace | None = None
    price: PriceBand | None = None
    availability: Literal["AVAILABLE_ONLY"] | None = None
    limit: int = Field(default=20, ge=1, le=50)

    def request(self) -> ProductSearchRequest:
        """Build ``POST /agentic/products/search``.

        Returns:
            The request, with only what was configured.
        """
        preference = self.merchant_preference
        filters = (
            SearchFilters(
                price=PriceFilter(min=self.price.min, max=self.price.max) if self.price else None,
                availability=self.availability,
            )
            if self.price or self.availability
            else None
        )
        return ProductSearchRequest(
            query=self.query,
            merchant_preference=MerchantPreference(
                mode=preference.mode, merchant_name=preference.merchant_name
            )
            if preference
            else None,
            context=SearchContext(country=self.context.country, currency=self.context.currency)
            if self.context
            else None,
            filters=filters,
            pagination=SearchPagination(limit=self.limit),
        )


class CheckoutUrl(_Config):
    """The cart the third route quotes: Reap's ``externalCheckout``.

    Attributes:
        merchant: The merchant's name, as the grant's merchant scope names it.
        merchant_domain: The merchant's domain, which Reap must have allowlisted.
        checkout_url: The cart URL.
    """

    merchant: str = Field(min_length=1)
    merchant_domain: str = Field(min_length=1, max_length=253)
    checkout_url: HttpsUrl = Field(max_length=8192)


class AttributeMatch(_Config):
    """Shape (b): the part, or an approved equivalent, by its options.

    Attributes:
        attributes: Each option name and the values accepted for it, such as
            ``{"Capacity": ("1 TB", "2 TB")}``.
    """

    attributes: dict[str, tuple[str, ...]] = Field(min_length=1)

    @model_validator(mode="after")
    def _values_named(self) -> Self:
        empty = [name for name, values in self.attributes.items() if not values]
        if empty:
            raise ValueError(f"attributes {empty} accept no value")
        return self

    def accepts(self, name: str, value: str) -> bool:
        """Whether a value of one option is acceptable; an option not named accepts any.

        Args:
            name: The option's name, such as ``Capacity``.
            value: The option's value, such as ``1 TB``.

        Returns:
            True when the option is not constrained or the value is accepted.
        """
        for named, accepted in self.attributes.items():
            if _norm(named) == _norm(name):
                return _norm(value) in {_norm(a) for a in accepted}
        return True


class ValueOption(_Config):
    """Read the item's value from one of its options.

    Attributes:
        option: The option's name, such as ``Amount``.
    """

    option: str = Field(min_length=1)


class ValueMatch(_Config):
    """Shape (a): the item's value against the estimated lease.

    Attributes:
        value_from: ``price`` (the variant's unit price) or an option naming the value.
        covers_estimate: Whether the value must be at least the estimate.
        max_multiple: The most the value may be, as a multiple of the estimate; None for
            no ceiling.
    """

    value_from: Literal["price"] | ValueOption
    covers_estimate: bool = True
    max_multiple: Decimal | None = Field(default=None, ge=1)

    @property
    def needs_estimate(self) -> bool:
        """Whether judging needs the estimate.

        Returns:
            True when the value is compared with it.
        """
        return self.covers_estimate or self.max_multiple is not None


class NeedSpec(_Config):
    """What to buy and how, read from the ``purchase:`` block before any need exists.

    Attributes:
        shape: Compute or part, which decides the follow-through.
        route: Catalogue search, or a configured cart URL.
        search: The catalogue search; required on the catalogue route.
        external_checkout: The cart; required on the checkout-URL route.
        match: How a variant is judged; required on the catalogue route.
        quantity: How many of the variant (the catalogue route).
        email: Where Reap sends the order's receipt.
        shipping_address: Where the item ships; required when it ships and on every
            checkout-URL quote.
    """

    shape: Shape
    route: Route = Route.CATALOGUE
    search: CatalogueSearch | None = None
    external_checkout: CheckoutUrl | None = None
    match: AttributeMatch | ValueMatch | None = None
    quantity: int = Field(default=1, gt=0)
    email: Email
    shipping_address: ShippingAddress | None = None

    @model_validator(mode="after")
    def _route_complete(self) -> Self:
        if self.route == Route.CATALOGUE:
            if self.search is None:
                raise ValueError("the catalogue route needs a search")
            if self.match is None:
                raise ValueError("the catalogue route needs a match")
        else:
            if self.external_checkout is None:
                raise ValueError("the checkout_url route needs external_checkout")
            if self.shipping_address is None:
                raise ValueError(
                    "the checkout_url route needs a shipping_address: Reap requires one on "
                    "every external checkout quote"
                )
        return self


class Need(BaseModel):
    """A raised need: one thing to buy, for one reason, with its attempts counted.

    Attributes:
        id: Identifier.
        objective_id: The objective it serves.
        spec: What to buy and how.
        reason: Why, in words: the breach or the fault.
        estimate_usd: Shape (a): the lease the estimate says restores the objective.
        raised_at: When it was raised.
        quote_attempts: Quote requests sent for it so far; each mints a new key.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    objective_id: str
    spec: NeedSpec
    reason: str
    estimate_usd: Decimal | None = Field(default=None, gt=0)
    raised_at: datetime
    quote_attempts: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _estimate_when_judged_by_value(self) -> Self:
        match = self.spec.match
        if isinstance(match, ValueMatch) and match.needs_estimate and self.estimate_usd is None:
            raise ValueError("a need judged by value needs estimate_usd")
        return self

    @property
    def what(self) -> str:
        """Return the need in words.

        Returns:
            The search query, or the configured cart URL.
        """
        if self.spec.search is not None:
            return self.spec.search.query
        if self.spec.external_checkout is not None:
            return self.spec.external_checkout.checkout_url
        return self.spec.shape.value


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_CURRENCY = re.compile(r"\b[A-Z]{3}\b")


def _option_value(label: str, fallback_currency: str) -> Price | str:
    """Read a value such as ``USD 100`` from an option's label.

    Args:
        label: The option's value as the merchant states it.
        fallback_currency: The currency when the label names none.

    Returns:
        The value, or why it cannot be read.
    """
    numbers = _NUMBER.findall(label)
    codes = _CURRENCY.findall(label)
    if len(numbers) != 1 or len(codes) > 1:
        return f"{label!r} does not name exactly one value"
    try:
        amount = Decimal(numbers[0].replace(",", ""))
    except InvalidOperation:
        return f"{label!r} does not name exactly one value"
    return Price(amount=amount, currency=codes[0] if codes else fallback_currency)


def _judge_attributes(match: AttributeMatch, variant: CatalogueVariant) -> RuleResult:
    options = {_norm(name): value for name, value in variant.options.items()}
    matched: list[str] = []
    for name, accepted in match.attributes.items():
        value = options.get(_norm(name))
        if value is None:
            return False, f"{variant.key} states no {name}"
        if not match.accepts(name, value):
            return False, f"{variant.key} {name} {value} is not one of {list(accepted)}"
        matched.append(f"{name} {value}")
    return True, f"{variant.key} matches the need: {', '.join(matched)}"


def _judge_value(
    match: ValueMatch, variant: CatalogueVariant, quantity: int, estimate: Decimal | None
) -> RuleResult:
    if isinstance(match.value_from, ValueOption):
        name = match.value_from.option
        label = next((v for k, v in variant.options.items() if _norm(k) == _norm(name)), None)
        if label is None:
            return False, f"{variant.key} states no {name}"
        unit = _option_value(label, variant.price.currency)
        if isinstance(unit, str):
            return False, f"{variant.key} {name}: {unit}"
    else:
        unit = variant.price
    if unit.currency != BUDGET_CURRENCY:
        return False, f"{variant.key} is valued in {unit.currency}, the budget in {BUDGET_CURRENCY}"
    value = unit.amount * quantity
    if estimate is None:
        return True, f"{variant.key} is worth {value} {unit.currency}"
    if match.covers_estimate and value < estimate:
        return False, f"{variant.key} worth {value} does not cover the estimate {estimate}"
    if match.max_multiple is not None and value > estimate * match.max_multiple:
        return False, (
            f"{variant.key} worth {value} is more than {match.max_multiple} times the "
            f"estimate {estimate} (the configured multiple)"
        )
    return True, f"{variant.key} worth {value} covers the estimate {estimate}"


def _judge_cart(target: CheckoutUrl, variant: CatalogueVariant) -> RuleResult:
    if variant.id != target.checkout_url:
        return False, f"{variant.id} is not the configured cart {target.checkout_url}"
    if _norm(variant.merchant) != _norm(target.merchant):
        return False, f"the cart is from {variant.merchant}, not {target.merchant}"
    return True, f"the configured cart from {target.merchant}"


def judge_variant(need: Need, variant: CatalogueVariant, *, quantity: int) -> RuleResult:
    """Judge whether a variant, bought in a quantity, fills the need.

    Args:
        need: The need.
        variant: The candidate, as Reap resolved it.
        quantity: How many would be bought.

    Returns:
        Whether it fills the need, and the detail recorded on the decision.
    """
    spec = need.spec
    if spec.route == Route.CHECKOUT_URL:
        if spec.external_checkout is None:
            return False, f"need {need.id} names no cart"
        return _judge_cart(spec.external_checkout, variant)
    match spec.match:
        case AttributeMatch():
            return _judge_attributes(spec.match, variant)
        case ValueMatch():
            return _judge_value(spec.match, variant, quantity, need.estimate_usd)
        case None:
            return False, f"need {need.id} names no match"


def judge_quote(need: Need, quote: LandedQuote) -> RuleResult:
    """Judge a landed quote against the need it was raised for: rule 10.

    Args:
        need: The need.
        quote: The landed quote.

    Returns:
        Whether its item fills the need, and the detail.
    """
    if quote.need_id != need.id:
        return False, f"quote {quote.id} is for {quote.need_id}, not {need.id}"
    return judge_variant(need, quote.variant, quantity=quote.quantity)


def need_judge(lookup: Callable[[str], Need | None]) -> NeedJudge:
    """Build the gate's rule 10 over the raised needs.

    Args:
        lookup: Finds a need by id, or None.

    Returns:
        The judge the gate takes; an unknown need fails closed.
    """

    def judge(quote: LandedQuote) -> RuleResult:
        need = lookup(quote.need_id)
        if need is None:
            return False, f"no need {quote.need_id} has been raised"
        return judge_quote(need, quote)

    return judge

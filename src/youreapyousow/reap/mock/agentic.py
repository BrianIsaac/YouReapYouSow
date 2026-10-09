"""The agentic mock: Reap's Agentic Payments module, in memory, seeded from a catalogue.

The engine answers the twelve agentic operations with the client's wire models
(``reap/models.py``); ``reap/mock/server.py`` serves it on Reap's paths, so the real
HTTP client runs against it. The catalogue is a YAML file under
``reap/mock/catalogue/``: merchants, their products, option groups and variants with
unit prices and availability, shipping options with prices, additional charges, offer
codes, and per country a flat tax rate and the address rules a merchant applies. Three
ship with the package (``compute``, ``parts`` and ``headphones``); any other file loads
the same way.

Sources: https://docs.reap.global/api-reference/openapi.json (the ``/agentic/*``
operations),
https://docs.reap.global/agentic-payments/{setup,one-time-purchases,lifecycle,how-it-works}.md,
https://docs.reap.global/api-reference/{errors,idempotency,rate-limiting}.md and
https://docs.reap.global/changelog.md.

Documented, implemented as written:

- Enrollment: ``EXTERNAL`` returns ``REQUIRES_ACTION`` with a redirect ``nextAction`` to a
  hosted card entry page and becomes ``ACTIVE`` once the card is entered; ``EXPIRED`` when
  the step is not finished before ``expiresAt``; only ``ACTIVE`` is charged or revoked;
  "Revoking is final". The three published test cards, with their CVC and expiry, and the
  one-time password ``456789``. A list is scoped to one owner and pages by ``nextCursor``.
- Search with ``merchantPreference`` (``PREFER``, ``ONLY``) and ``MERCHANT_NOT_RESOLVED``,
  the price band and ``AVAILABLE_ONLY`` filters, ``pagination`` and ``warnings``. Details
  with option groups whose values carry ``optionId`` and ``available``, a purchasable
  ``defaultVariant``, and an id that does not resolve reported in ``errors`` "rather than
  stopping the call". "The returned variant id is the only id accepted by POST
  /agentic/quotes"; ``VARIANT_RESOLUTION_FAILED``.
- Quote: exactly one of ``items`` and ``externalCheckout``; ``shippingAddress`` required
  "when any item requires shipping"; the ``amountBreakdown`` fields and ``expiresAt``;
  ``VARIANT_UNAVAILABLE`` for a sold-out item; ``CARD_PAYMENT_UNAVAILABLE``;
  ``OFFER_CODE_INVALID`` and ``OFFER_CODE_EXPIRED``; ``QUOTE_UNFULFILLABLE`` with the
  OpenAPI's ``detail.reason`` (``ITEMS_UNSHIPPABLE``, ``STATE_OR_PROVINCE_REQUIRED``,
  ``ADDRESS_LINE_2_REQUIRED``, ``INVALID_PHONE``) and each reason's fixed message;
  ``CHECKOUT_URL_INVALID`` with ``detail.reason``, a checkout URL accepted only on an
  allowlisted merchant domain. Choosing a shipping option re-prices the quote;
  ``SHIPPING_OPTION_INVALID``, ``QUOTE_EXPIRED``, ``QUOTE_NOT_MUTABLE`` and
  ``QUOTE_REPLACEMENT_REQUIRED``.
- Checkout: the lifecycle page's state machine (``REQUIRES_ACTION`` to ``PROCESSING`` on
  approval or ``EXPIRED`` past ``expiresAt``; ``PROCESSING`` to ``COMPLETED`` or
  ``FAILED``; the three final statuses never change); ``nextAction`` only while approval
  is outstanding; ``X-Simulate-Checkout: COMPLETED`` completing in the sandbox and
  rejected in production; ``orderId`` and ``finalAmount`` on a completed checkout;
  ``QUOTE_NOT_FOUND``, ``ENROLLMENT_NOT_FOUND``, ``ENROLLMENT_NOT_ACTIVE`` (with
  ``detail.reason`` ``CARD_NOT_CAPTURED``) and ``QUOTE_EXPIRED``.
- Every operation's error codes and statuses (``DOCUMENTED_ERRORS``), each sent with the
  OpenAPI's description as its message; ``Retry-After`` on
  ``QUOTE_TEMPORARILY_UNAVAILABLE``, ``CHECKOUT_TEMPORARILY_UNAVAILABLE`` and
  ``RATE_LIMIT_EXCEEDED`` only; ``403 AGENTIC_PAYMENTS_NOT_ENABLED`` when the project is
  not enabled.

Assumed where the docs are silent, each one a named choice:

- Times (``AgenticMockConfig``): a quote lives 120 s ("Quotes are short-lived"), the
  hosted card entry page 30 minutes, the hosted approval page 15 minutes, and an approved
  checkout stays ``PROCESSING`` for 1 s.
- Money: tax is a flat rate per country on the items after discounts, never on shipping
  or charges; when prices include it, the breakdown shows the included part.
  ``finalAmount`` is the full sum (items less discounts, plus shipping, tax not included
  in prices and additional charges), although Reap's own examples leave the tax out. A
  completed checkout's ``finalAmount`` equals the quote's unless a test overrides it.
  Amounts are rounded half up to the cent. Nothing is converted between currencies.
- The order id is ``MOCK-ORDER-`` and six digits. Resource ids are ``uuid4``; product,
  variant and shipping option ids are the catalogue's; an option id is derived from its
  product, group and value.
- Under the sandbox header the create response is ``COMPLETED`` (the changelog) or, when
  configured, ``REQUIRES_ACTION`` with the first read ``COMPLETED`` (the guide's example)
  (``SimulatedCreate``).
- Hosted pages are local, at ``<hosted_base_url>/hosted/enrollments/<id>`` and
  ``<hosted_base_url>/hosted/checkouts/<id>``. A wrong card or password leaves the
  enrollment ``REQUIRES_ACTION`` for another try (never ``FAILED``). The stored card's
  network reads ``VISA`` (the docs show ``<network>``; the test cards are in a Visa
  range). Approval has no decline: the lifecycle names no declined status.
- ``REAP_CARD`` and ``BIN_SPONSOR`` enrollments ("coming soon") are rejected with
  ``AGENTIC_REQUEST_REJECTED``, so ``AGENTIC_CARD_NOT_FOUND`` is reachable only by
  injection; listing by ``REAP_USER`` finds nothing.
- Search: a product matches when at least half of the query's words (and at least one)
  are in its name or keywords, ignoring case, inner dots and hyphens, a plural ``s`` and
  whether neighbouring words are written apart or together (``120 mm``, ``120mm``);
  matches rank by words shared, then by preview price; ``PREFER`` ranks the merchant's
  products first. ``MERCHANT_NOT_RESOLVED`` in both modes. The default page size is 20.
  ``context.country`` is not used. A product priced in another currency than
  ``context.currency`` is left out and a warning says so. ``priceRange`` spans every
  variant, as the guide's example does with Silver sold out; ``previewVariant`` is the
  default variant if it passes the filters, else the cheapest one that does. A cursor this
  mock did not issue is ``422 VALIDATION_FAILED`` ("decoding a pagination cursor").
- Details: an unknown product is an ``errors`` entry with code
  ``AGENTIC_RESOURCE_NOT_FOUND``; ``media`` is empty. Variant resolution needs exactly
  one value in every group; an unknown product is ``404 AGENTIC_RESOURCE_NOT_FOUND``; a
  sold-out variant resolves with ``available`` false ("Stop here when available reads
  false").
- Quote: an id that is not a variant, or variants from two merchants, is
  ``AGENTIC_REQUEST_REJECTED`` with ``detail.path``; a repeated variant's quantities add
  up; any positive quantity is accepted. A missing address that an item needs is ``422
  VALIDATION_FAILED`` on ``shippingAddress``. Address rules run in this order: a country
  the merchant does not ship to, a missing region, a missing second line, a phone whose
  calling code is not the country's. Shipping options are listed only when an item ships,
  with the catalogue's preselection. An offer code takes a percentage or an amount off the
  items (``SAVE10`` is ten percent). A checkout URL reads its cart from
  ``https://<merchantDomain>/cart/<variant id>:<quantity>[,...]``, the guide's example's
  shape; ``EXPIRED`` is reachable only by injection.
- Shipping option: ``expiresAt`` does not move; ``QUOTE_NOT_MUTABLE`` once a checkout was
  opened; ``QUOTE_REPLACEMENT_REQUIRED`` once an item sold out; the offer code is checked
  again; the OpenAPI's ``SHIPPING_OPTION_INVALID`` reasons are reachable by injection.
  ``GET /agentic/quotes/{id}`` returns the quote as last priced, even once expired.
- Checkout: the header in production is ``AGENTIC_REQUEST_REJECTED`` with
  ``detail.header``, checked once the body validates and before the engine's own checks.
  A quote takes one checkout; a second is ``AGENTIC_REQUEST_REJECTED`` with
  ``detail.field`` ``quoteId`` (the OpenAPI's checkout rejection shape), since "FAILED
  and EXPIRED ... need a fresh quote and a fresh checkout". The order is placed when
  processing ends, and fails (``FAILED``) if an item sold out by then. ``amount`` on
  create is the quote's ``finalAmount``; ``finalAmount`` on a read appears only once
  ``COMPLETED``, and ``updatedAt`` is when the order settled. ``ENROLLMENT_NOT_ACTIVE``
  carries ``CARD_NOT_CAPTURED`` only while the enrollment awaits its card.
- Matching: merchant names (``merchantPreference``) and merchant domains ignore case;
  offer codes match exactly. Enrollments list oldest first. Every expiry is set on a
  whole second, rounded down, so the ``expiresAt`` shown is the instant enforced.
- The hosted card page accepts the test cards in either environment.

Fault injection, for tests only: ``inject_error`` (any code the operation documents or
any endpoint returns, answered before the operation runs, with ``Retry-After`` where
Reap sends one; its ``detail`` is sent as given, unchecked against the OpenAPI),
``drop_next_response`` (the server loses the response after the operation ran),
``hold_next`` (a call held in flight, for ``IDEMPOTENCY_REQUEST_IN_PROGRESS``),
``set_variant_available``, ``fail_order``, ``override_final_amount`` and ``enabled``.
"""

import asyncio
import base64
import binascii
import functools
import itertools
import math
import re
import uuid
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from youreapyousow.clock import Clock, utc_now
from youreapyousow.reap.models import (
    AgenticErrorCode,
    AmountBreakdown,
    Checkout,
    CheckoutCreated,
    CheckoutStatus,
    ClientReferenceOwner,
    CreateCheckoutRequest,
    CreateEnrollmentRequest,
    CreateExternalCheckoutQuoteRequest,
    CreateExternalEnrollmentRequest,
    CreateQuoteRequest,
    Enrollment,
    EnrollmentCreated,
    EnrollmentOwner,
    EnrollmentStatus,
    ExternalEnrollmentCreated,
    MerchantName,
    Money,
    NamedAmount,
    NextAction,
    OptionValue,
    Page,
    PaymentMethod,
    PreviewVariant,
    PriceRange,
    ProductDetail,
    ProductDetailsRequest,
    ProductDetailsResponse,
    ProductError,
    ProductOption,
    ProductSearchRequest,
    ProductSearchResponse,
    ProductSummary,
    Quote,
    ResolveVariantRequest,
    SearchPage,
    SelectShippingOptionRequest,
    ShippingAddress,
    ShippingOption,
    ShippingOptionDetail,
    Tax,
    Variant,
    VariantOption,
)

BUNDLED_CATALOGUES = ("compute", "parts", "headphones", "prizes")
"""The catalogues shipped in ``reap/mock/catalogue/``, in the order they load by default."""

type _Text = Annotated[str, Field(min_length=1)]
type _Country = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]


# Catalogue files


class _CatalogueModel(BaseModel):
    """Base for the catalogue file: snake_case YAML, unknown keys refused, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CountryRules(_CatalogueModel):
    """What a merchant applies to an address in one country.

    Attributes:
        tax_rate: The flat tax rate on items after discounts, such as ``0.09``.
        tax_included: Whether item prices already include the tax.
        calling_code: The country's telephone calling code, without the plus sign.
        region_required: Whether an address there needs a state or province.
        address_line2_required: Whether an address there needs a second line.
    """

    tax_rate: Decimal = Field(ge=0, lt=1)
    tax_included: bool = False
    calling_code: str = Field(pattern=r"^[1-9]\d{0,3}$")
    region_required: bool = False
    address_line2_required: bool = False


class ShippingRate(_CatalogueModel):
    """A shipping option a merchant offers on every quote that needs shipping.

    Attributes:
        id: The option id a quote lists and ``shippingOptionId`` names.
        name: Its display name.
        price: Its price in the merchant's currency.
        selected: Whether a new quote arrives with it preselected.
        details: Free-form key and value pairs, as Reap's ``details`` carries them.
    """

    id: _Text
    name: _Text
    price: Decimal = Field(ge=0)
    selected: bool = False
    details: dict[str, str] = Field(default_factory=dict[str, str])


class OfferCode(_CatalogueModel):
    """An offer code a merchant accepts, taking a percentage or an amount off the items.

    Attributes:
        code: The code as the buyer types it (matched exactly).
        percent_off: Percentage off the items subtotal.
        amount_off: Amount off the items subtotal, at most the subtotal.
        expires_at: When it stops being accepted; None for never.
    """

    code: _Text
    percent_off: Decimal | None = Field(default=None, gt=0, le=100)
    amount_off: Decimal | None = Field(default=None, gt=0)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        if (self.percent_off is None) == (self.amount_off is None):
            raise ValueError(f"offer code {self.code} needs exactly one of percent_off, amount_off")
        return self


class Charge(_CatalogueModel):
    """An additional charge a merchant adds to every quote, such as a service fee.

    Attributes:
        name: The charge's name on the quote.
        amount: Its amount in the merchant's currency.
    """

    name: _Text
    amount: Decimal = Field(ge=0)


class CatalogueVariant(_CatalogueModel):
    """One purchasable variant.

    Attributes:
        id: The variant id a quote accepts.
        name: Its display name.
        options: Its value in every option group of its product, by group name.
        price: Its unit price in the merchant's currency.
        available: Whether it is in stock.
        requires_shipping: Whether a quote for it needs a shipping address.
    """

    id: _Text
    name: str | None = None
    options: dict[str, str] = Field(default_factory=dict[str, str])
    price: Decimal = Field(gt=0)
    available: bool = True
    requires_shipping: bool = True


class CatalogueProduct(_CatalogueModel):
    """A product: its variants share option groups and differ in their values.

    Attributes:
        id: The product id search and details return.
        name: Its name, matched against search queries.
        description: Its description on the details response.
        keywords: Further words a search query matches.
        default_variant: The default variant's id; the first variant when None.
        variants: The variants, at least one.
    """

    id: _Text
    name: _Text
    description: str | None = None
    keywords: tuple[str, ...] = ()
    default_variant: str | None = None
    variants: tuple[CatalogueVariant, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        groups = list(self.variants[0].options)
        seen: set[tuple[tuple[str, str], ...]] = set()
        for variant in self.variants:
            if set(variant.options) != set(groups):
                differ = sorted(set(variant.options) ^ set(groups))
                raise ValueError(
                    f"variant {variant.id} of {self.id} has option groups {differ} "
                    "that its siblings do not share"
                )
            combination = tuple(sorted(variant.options.items()))
            if combination in seen:
                raise ValueError(f"variant {variant.id} of {self.id} repeats the same options")
            seen.add(combination)
        if len(set(self.option_ids.values())) != len(self.option_ids):
            raise ValueError(f"two option values of {self.id} share an option id")
        if self.default_variant is not None and self.default_variant not in {
            v.id for v in self.variants
        }:
            raise ValueError(
                f"default_variant {self.default_variant} is not a variant of {self.id}"
            )
        return self

    @property
    def default(self) -> CatalogueVariant:
        """Return the default variant.

        Returns:
            The named default, or the first variant.
        """
        return next((v for v in self.variants if v.id == self.default_variant), self.variants[0])

    @property
    def option_groups(self) -> dict[str, tuple[str, ...]]:
        """Return each option group's values, in the order the variants list them.

        Returns:
            Group name to its distinct values.
        """
        groups: dict[str, list[str]] = {name: [] for name in self.variants[0].options}
        for variant in self.variants:
            for name, value in variant.options.items():
                if value not in groups[name]:
                    groups[name].append(value)
        return {name: tuple(values) for name, values in groups.items()}

    @property
    def option_ids(self) -> dict[tuple[str, str], str]:
        """Return the option id of every value of every group.

        Returns:
            ``(group, value)`` to an id derived from the product id, group and value.
        """
        return {
            (group, value): f"opt-{self.id}-{_slug(group)}-{_slug(value)}"
            for group, values in self.option_groups.items()
            for value in values
        }


def _slug(text: str) -> str:
    """Reduce text to lowercase letters, digits and single hyphens.

    Args:
        text: The text.

    Returns:
        The slug.
    """
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


class CatalogueMerchant(_CatalogueModel):
    """A merchant: what it sells, where it ships, and what it adds to a quote.

    Attributes:
        name: The merchant name search results and ``merchantPreference`` use.
        domain: Its web domain, which an external checkout URL must be on.
        card_payment: Whether it takes card payment; quotes fail
            ``CARD_PAYMENT_UNAVAILABLE`` when it does not.
        external_checkout: Whether its domain is allowlisted for checkout URL quotes.
        ships_to: The countries it ships to.
        shipping_options: Its shipping options, at most one preselected.
        additional_charges: Charges added to every quote.
        offer_codes: The offer codes it accepts.
        products: What it sells.
    """

    name: _Text
    domain: str | None = None
    card_payment: bool = True
    external_checkout: bool = False
    ships_to: tuple[_Country, ...] = Field(min_length=1)
    shipping_options: tuple[ShippingRate, ...] = ()
    additional_charges: tuple[Charge, ...] = ()
    offer_codes: tuple[OfferCode, ...] = ()
    products: tuple[CatalogueProduct, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if sum(option.selected for option in self.shipping_options) > 1:
            raise ValueError(f"{self.name} has more than one selected shipping option")
        if len({option.id for option in self.shipping_options}) != len(self.shipping_options):
            raise ValueError(f"{self.name} repeats a shipping option id")
        ships = any(v.requires_shipping for p in self.products for v in p.variants)
        if ships and not self.shipping_options:
            raise ValueError(
                f"{self.name} sells items that require shipping but has no shipping option"
            )
        if self.external_checkout and not self.domain:
            raise ValueError(f"{self.name} allows external checkout but has no domain")
        return self


class CatalogueFile(_CatalogueModel):
    """One catalogue file.

    Attributes:
        name: The catalogue's name.
        description: What it holds.
        currency: The currency of every price in it.
        default_country: The country whose tax applies when a quote has no address.
        countries: The rules per country its merchants ship to.
        merchants: Its merchants.
    """

    name: _Text
    description: str = ""
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    default_country: _Country
    countries: dict[_Country, CountryRules] = Field(min_length=1)
    merchants: tuple[CatalogueMerchant, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _countries_known(self) -> Self:
        if self.default_country not in self.countries:
            raise ValueError(f"default_country {self.default_country} has no country rules")
        for merchant in self.merchants:
            unknown = sorted(set(merchant.ships_to) - set(self.countries))
            if unknown:
                raise ValueError(f"{merchant.name} ships to {unknown} with no country rules")
        return self


# The loaded catalogue


@dataclass(frozen=True)
class Merchant:
    """A merchant as loaded, carrying the currency and country rules of its file.

    Attributes:
        name: The merchant name.
        domain: Its web domain, if any.
        card_payment: Whether it takes card payment.
        external_checkout: Whether checkout URL quotes are allowed on its domain.
        ships_to: The countries it ships to.
        shipping_options: Its shipping options.
        additional_charges: Charges added to every quote.
        offer_codes: The offer codes it accepts.
        products: What it sells.
        currency: The currency of its prices.
        countries: Tax and address rules per country.
        default_country: The country whose tax applies without an address.
        catalogue: The name of the file it came from.
    """

    name: str
    domain: str | None
    card_payment: bool
    external_checkout: bool
    ships_to: tuple[str, ...]
    shipping_options: tuple[ShippingRate, ...]
    additional_charges: tuple[Charge, ...]
    offer_codes: tuple[OfferCode, ...]
    products: tuple[CatalogueProduct, ...]
    currency: str
    countries: Mapping[str, CountryRules]
    default_country: str
    catalogue: str

    @property
    def preselected_shipping(self) -> ShippingRate | None:
        """Return the shipping option a new quote arrives with.

        Returns:
            The option marked selected, else the first, else None.
        """
        return next(
            (o for o in self.shipping_options if o.selected),
            self.shipping_options[0] if self.shipping_options else None,
        )


@dataclass(frozen=True)
class ProductEntry:
    """A product with the merchant that sells it.

    Attributes:
        merchant: The merchant.
        product: The product.
    """

    merchant: Merchant
    product: CatalogueProduct


@dataclass(frozen=True)
class VariantEntry:
    """A variant with its product and merchant.

    Attributes:
        merchant: The merchant.
        product: The product.
        variant: The variant.
    """

    merchant: Merchant
    product: CatalogueProduct
    variant: CatalogueVariant


class Catalogue:
    """One or more catalogue files merged, every id naming exactly one thing."""

    def __init__(self, files: Sequence[CatalogueFile]) -> None:
        """Merge catalogue files.

        Args:
            files: The files, in order; their merchants keep that order.

        Raises:
            ValueError: If two files share a merchant name or domain, a product id or a
                variant id.
        """
        self.names = tuple(f.name for f in files)
        merchants: list[Merchant] = []
        self._products: dict[str, ProductEntry] = {}
        self._variants: dict[str, VariantEntry] = {}
        self._by_name: dict[str, Merchant] = {}
        self._by_domain: dict[str, Merchant] = {}
        for file in files:
            for spec in file.merchants:
                merchant = Merchant(
                    name=spec.name,
                    domain=spec.domain,
                    card_payment=spec.card_payment,
                    external_checkout=spec.external_checkout,
                    ships_to=spec.ships_to,
                    shipping_options=spec.shipping_options,
                    additional_charges=spec.additional_charges,
                    offer_codes=spec.offer_codes,
                    products=spec.products,
                    currency=file.currency,
                    countries=dict(file.countries),
                    default_country=file.default_country,
                    catalogue=file.name,
                )
                _claim(self._by_name, merchant.name.casefold(), merchant, "merchant")
                if merchant.domain:
                    _claim(self._by_domain, merchant.domain.casefold(), merchant, "domain")
                merchants.append(merchant)
                for product in spec.products:
                    _claim(self._products, product.id, ProductEntry(merchant, product), "product")
                    for variant in product.variants:
                        entry = VariantEntry(merchant, product, variant)
                        _claim(self._variants, variant.id, entry, "variant")
        self.merchants = tuple(merchants)

    def merchant(self, name: str) -> Merchant | None:
        """Find a merchant by name, ignoring case.

        Args:
            name: The merchant name.

        Returns:
            The merchant, or None.
        """
        return self._by_name.get(name.casefold())

    def merchant_by_domain(self, domain: str) -> Merchant | None:
        """Find a merchant by its web domain, ignoring case.

        Args:
            domain: The domain.

        Returns:
            The merchant, or None.
        """
        return self._by_domain.get(domain.casefold())

    def find_product(self, product_id: str) -> ProductEntry | None:
        """Find a product.

        Args:
            product_id: The product id.

        Returns:
            The product and its merchant, or None.
        """
        return self._products.get(product_id)

    def product(self, product_id: str) -> ProductEntry:
        """Return a product that must exist.

        Args:
            product_id: The product id.

        Returns:
            The product and its merchant.

        Raises:
            KeyError: If there is no such product.
        """
        return self._products[product_id]

    def products(self) -> Iterator[ProductEntry]:
        """Iterate over every product, in catalogue order.

        Yields:
            Each product with its merchant.
        """
        yield from self._products.values()

    def find_variant(self, variant_id: str) -> VariantEntry | None:
        """Find a variant.

        Args:
            variant_id: The variant id.

        Returns:
            The variant with its product and merchant, or None.
        """
        return self._variants.get(variant_id)

    def variant(self, variant_id: str) -> VariantEntry:
        """Return a variant that must exist.

        Args:
            variant_id: The variant id.

        Returns:
            The variant with its product and merchant.

        Raises:
            KeyError: If there is no such variant.
        """
        return self._variants[variant_id]

    def variants(self) -> Iterator[VariantEntry]:
        """Iterate over every variant, in catalogue order.

        Yields:
            Each variant with its product and merchant.
        """
        yield from self._variants.values()


def _claim[V](index: dict[str, V], key: str, value: V, kind: str) -> None:
    """Add to an index, refusing a key that is already taken.

    Args:
        index: The index.
        key: The key.
        value: The value.
        kind: What the key names, for the error.

    Raises:
        ValueError: If the key is already in the index.
    """
    if key in index:
        raise ValueError(f"duplicate {kind} {key!r} across catalogue files")
    index[key] = value


def _parse(text: str) -> CatalogueFile:
    """Parse and validate one catalogue file's YAML.

    Args:
        text: The YAML text.

    Returns:
        The validated file.
    """
    data: Any = yaml.safe_load(text)
    return CatalogueFile.model_validate(data)


@functools.cache
def _bundled_file(name: str) -> CatalogueFile:
    """Read a bundled catalogue file once per process.

    Args:
        name: One of ``BUNDLED_CATALOGUES``.

    Returns:
        The validated file.
    """
    text = resources.files(__package__).joinpath("catalogue", f"{name}.yaml").read_text("utf-8")
    return _parse(text)


def bundled_catalogue(*names: str) -> Catalogue:
    """Load catalogues shipped with the package.

    Args:
        *names: Which of ``BUNDLED_CATALOGUES``; all three when none is given.

    Returns:
        The merged catalogue.

    Raises:
        ValueError: If a name is not a bundled catalogue.
    """
    unknown = [name for name in names if name not in BUNDLED_CATALOGUES]
    if unknown:
        raise ValueError(f"no bundled catalogue {unknown}; choose from {BUNDLED_CATALOGUES}")
    return Catalogue([_bundled_file(name) for name in names or BUNDLED_CATALOGUES])


def load_catalogue(*paths: Path) -> Catalogue:
    """Load catalogue files from disk.

    Args:
        *paths: The YAML files, merged in order.

    Returns:
        The merged catalogue.
    """
    return Catalogue([_parse(path.read_text(encoding="utf-8")) for path in paths])


# Operations, their documented errors, and the configuration

HOSTED_BASE_URL = "http://reap-mock.local"
"""Where the mock's hosted pages live by default: the in-process client's base URL."""

TEST_CARDS: Mapping[str, tuple[str, str]] = {
    "4622 9431 2313 7797": ("640", "12/27"),
    "4622 9431 2313 7805": ("304", "12/27"),
    "4622 9431 2313 7847": ("698", "12/27"),
}
"""The sandbox test cards, number to CVC and expiry (setup page, "Test cards")."""

TEST_OTP = "456789"
"""The sandbox one-time password (setup page, "One-time passwords")."""

_TEST_CARD_NETWORK = "VISA"
_ORDER_PREFIX = "MOCK-ORDER-"
_CENT = Decimal("0.01")


class Operation(StrEnum):
    """An agentic operation the mock serves, named by its method and path."""

    LIST_ENROLLMENTS = "GET /agentic/enrollments"
    CREATE_ENROLLMENT = "POST /agentic/enrollments"
    GET_ENROLLMENT = "GET /agentic/enrollments/{id}"
    REVOKE_ENROLLMENT = "POST /agentic/enrollments/{id}/revoke"
    SEARCH = "POST /agentic/products/search"
    DETAILS = "POST /agentic/products/details"
    VARIANT = "POST /agentic/products/variant"
    CREATE_QUOTE = "POST /agentic/quotes"
    GET_QUOTE = "GET /agentic/quotes/{id}"
    SELECT_SHIPPING_OPTION = "POST /agentic/quotes/{id}/shipping-option"
    CREATE_CHECKOUT = "POST /agentic/checkouts"
    GET_CHECKOUT = "GET /agentic/checkouts/{id}"


_E = AgenticErrorCode

# The four every agentic operation lists in the OpenAPI.
_COMMON: dict[str, int] = {
    _E.AGENTIC_REQUEST_REJECTED: 400,
    _E.AGENTIC_PAYMENTS_NOT_ENABLED: 403,
    _E.AGENTIC_RESOURCE_NOT_FOUND: 404,
    _E.AGENTIC_SERVICE_UNAVAILABLE: 503,
}

ANY_ENDPOINT_ERRORS: Mapping[str, int] = {
    "API_VERSION_HEADER_MISSING": 400,
    "API_VERSION_INVALID": 400,
    "PARSE_ERROR": 400,
    _E.IDEMPOTENT_PARAMETER_MISMATCH: 400,
    "API_KEY_REQUIRED": 401,
    "INVALID_AUTH_HEADER": 401,
    "INVALID_API_KEY": 401,
    "API_KEY_IP_NOT_ALLOWED": 403,
    "ROUTE_NOT_FOUND": 404,
    _E.IDEMPOTENCY_REQUEST_IN_PROGRESS: 409,
    _E.VALIDATION_FAILED: 422,
    _E.RATE_LIMIT_EXCEEDED: 429,
    "INTERNAL_SERVER_ERROR": 500,
}
"""Codes any endpoint can return (https://docs.reap.global/api-reference/errors.md)."""

DOCUMENTED_ERRORS: Mapping[Operation, Mapping[str, int]] = {
    Operation.LIST_ENROLLMENTS: _COMMON,
    Operation.CREATE_ENROLLMENT: _COMMON | {_E.AGENTIC_CARD_NOT_FOUND: 404},
    Operation.GET_ENROLLMENT: _COMMON | {_E.ENROLLMENT_NOT_FOUND: 404},
    Operation.REVOKE_ENROLLMENT: _COMMON
    | {_E.ENROLLMENT_NOT_FOUND: 404, _E.ENROLLMENT_NOT_ACTIVE: 409},
    Operation.SEARCH: _COMMON | {_E.MERCHANT_NOT_RESOLVED: 400},
    Operation.DETAILS: _COMMON,
    Operation.VARIANT: _COMMON | {_E.VARIANT_RESOLUTION_FAILED: 400},
    Operation.CREATE_QUOTE: _COMMON
    | {
        _E.CHECKOUT_URL_INVALID: 400,
        _E.CARD_PAYMENT_UNAVAILABLE: 400,
        _E.OFFER_CODE_INVALID: 400,
        _E.OFFER_CODE_EXPIRED: 400,
        _E.QUOTE_UNFULFILLABLE: 400,
        _E.QUOTE_EXPIRED: 409,
        _E.VARIANT_UNAVAILABLE: 409,
        _E.QUOTE_TEMPORARILY_UNAVAILABLE: 503,
    },
    Operation.GET_QUOTE: _COMMON | {_E.QUOTE_NOT_FOUND: 404},
    Operation.SELECT_SHIPPING_OPTION: _COMMON
    | {
        _E.OFFER_CODE_INVALID: 400,
        _E.OFFER_CODE_EXPIRED: 400,
        _E.SHIPPING_OPTION_INVALID: 400,
        _E.QUOTE_NOT_FOUND: 404,
        _E.QUOTE_EXPIRED: 409,
        _E.QUOTE_REPLACEMENT_REQUIRED: 409,
        _E.QUOTE_NOT_MUTABLE: 409,
    },
    Operation.CREATE_CHECKOUT: _COMMON
    | {
        _E.ENROLLMENT_NOT_FOUND: 404,
        _E.QUOTE_NOT_FOUND: 404,
        _E.ENROLLMENT_NOT_ACTIVE: 409,
        _E.QUOTE_EXPIRED: 409,
        _E.CHECKOUT_TEMPORARILY_UNAVAILABLE: 503,
    },
    Operation.GET_CHECKOUT: _COMMON | {_E.CHECKOUT_NOT_FOUND: 404},
}
"""Each operation's own error codes and statuses, from the OpenAPI."""

RETRY_AFTER_CODES = frozenset(
    {
        _E.QUOTE_TEMPORARILY_UNAVAILABLE,
        _E.CHECKOUT_TEMPORARILY_UNAVAILABLE,
        _E.RATE_LIMIT_EXCEEDED,
    }
)
"""Codes answered with a ``Retry-After`` header (OpenAPI, rate-limiting page)."""

# The OpenAPI's description of each error response, sent as the message; for the codes any
# endpoint returns, the errors page's "Cause" and the rate-limiting page's example.
MESSAGES: Mapping[str, str] = {
    _E.AGENTIC_REQUEST_REJECTED: "The request was rejected",
    _E.AGENTIC_PAYMENTS_NOT_ENABLED: "Agentic Payments is not enabled for this project",
    _E.AGENTIC_RESOURCE_NOT_FOUND: "Resource not found",
    _E.AGENTIC_SERVICE_UNAVAILABLE: "Agentic Payments is temporarily unavailable",
    _E.AGENTIC_CARD_NOT_FOUND: "Card not found",
    _E.ENROLLMENT_NOT_FOUND: "Enrollment not found.",
    _E.ENROLLMENT_NOT_ACTIVE: "Enrollment is not active.",
    _E.MERCHANT_NOT_RESOLVED: (
        "Merchant preference could not be resolved. Correct the merchant name or remove the "
        "restriction."
    ),
    _E.VARIANT_RESOLUTION_FAILED: "Product variant could not be resolved.",
    _E.CARD_PAYMENT_UNAVAILABLE: "Card payment is unavailable for this checkout.",
    _E.CHECKOUT_URL_INVALID: "The checkout URL cannot be used.",
    _E.OFFER_CODE_INVALID: "The offer code is invalid.",
    _E.OFFER_CODE_EXPIRED: "The offer code has expired.",
    _E.QUOTE_UNFULFILLABLE: (
        "The merchant cannot fulfill this quote as requested (for example, it cannot ship to "
        "the address)."
    ),
    _E.SHIPPING_OPTION_INVALID: "Shipping option is not available.",
    _E.QUOTE_NOT_FOUND: "Quote not found.",
    _E.QUOTE_EXPIRED: "The quote has expired.",
    _E.QUOTE_NOT_MUTABLE: "The quote can no longer be modified.",
    _E.QUOTE_REPLACEMENT_REQUIRED: "Create a new quote.",
    _E.VARIANT_UNAVAILABLE: "The selected item is sold out. Choose another variant or product.",
    _E.QUOTE_TEMPORARILY_UNAVAILABLE: (
        "We couldn't complete this quote right now. Please try again in a moment."
    ),
    _E.CHECKOUT_NOT_FOUND: "Checkout not found.",
    _E.CHECKOUT_TEMPORARILY_UNAVAILABLE: (
        "We couldn't complete this checkout right now. Please try again in a moment."
    ),
    "API_VERSION_HEADER_MISSING": "The Reap-Version header is missing.",
    "API_VERSION_INVALID": "The Reap-Version header is not a supported version.",
    "PARSE_ERROR": "The request body could not be parsed.",
    _E.IDEMPOTENT_PARAMETER_MISMATCH: "A POST reused an Idempotency-Key with a different body.",
    "API_KEY_REQUIRED": "The Authorization header is missing.",
    "INVALID_AUTH_HEADER": "The Authorization header is not a Bearer token.",
    "INVALID_API_KEY": (
        "The API key is unknown or revoked, or it belongs to another environment or region."
    ),
    "API_KEY_IP_NOT_ALLOWED": ("The request came from an IP address outside the key's allowlist."),
    "ROUTE_NOT_FOUND": "No endpoint matches the method and path.",
    _E.IDEMPOTENCY_REQUEST_IN_PROGRESS: (
        "Another POST with the same Idempotency-Key is still running."
    ),
    _E.VALIDATION_FAILED: "Validation failed",
    _E.RATE_LIMIT_EXCEEDED: "Rate limit exceeded. See the Retry-After header for when to retry.",
    "INTERNAL_SERVER_ERROR": "The request failed on our side.",
}

# The fixed messages the OpenAPI gives each ``detail.reason`` of QUOTE_UNFULFILLABLE and
# SHIPPING_OPTION_INVALID.
REASON_MESSAGES: Mapping[str, str] = {
    "INVALID_PHONE": "Phone is invalid",
    "STATE_OR_PROVINCE_REQUIRED": "Select a state / province",
    "ITEMS_UNSHIPPABLE": (
        "Your cart has been updated and the items you added can't be shipped to your address. "
        "Remove the items to complete your order."
    ),
    "ADDRESS_LINE_2_REQUIRED": "Address line 2 is required.",
}


class Environment(StrEnum):
    """Which Reap environment the mock plays."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"


class SimulatedCreate(StrEnum):
    """How a checkout created under ``X-Simulate-Checkout: COMPLETED`` first answers.

    Reap's changelog says the header "returns the checkout as
    ``COMPLETED``"; the one-time-purchases guide sends the header and shows the create
    response ``REQUIRES_ACTION``. Both readings are servable.
    """

    COMPLETED = "COMPLETED"
    """The create response is ``COMPLETED`` (the changelog)."""
    REQUIRES_ACTION = "REQUIRES_ACTION"
    """The create response is ``REQUIRES_ACTION`` and the first read ``COMPLETED`` (the guide)."""


@dataclass(frozen=True)
class AgenticMockConfig:
    """The mock's settings, each a documented unknown chosen here.

    Attributes:
        environment: Sandbox accepts ``X-Simulate-Checkout``; production rejects it.
        simulated_create: How a checkout under the sandbox header first answers.
        quote_ttl_s: How long a quote lives (Reap says only "Quotes are short-lived").
        enrollment_ttl_s: How long the hosted card entry page stays open.
        approval_ttl_s: How long the hosted approval page stays open.
        processing_s: How long an approved checkout stays ``PROCESSING``.
        hosted_base_url: The base of every ``nextAction.url``.
        api_key: The only bearer key accepted; any key when None.
    """

    environment: Environment = Environment.SANDBOX
    simulated_create: SimulatedCreate = SimulatedCreate.COMPLETED
    quote_ttl_s: float = 120.0
    enrollment_ttl_s: float = 1800.0
    approval_ttl_s: float = 900.0
    processing_s: float = 1.0
    hosted_base_url: str = HOSTED_BASE_URL
    api_key: str | None = None


class AgenticMockError(Exception):
    """An error the mock answers as ``{"error": {"code", "message", "detail"}}``."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        """Create the error.

        Args:
            status: HTTP status.
            code: Reap's error code.
            message: The message; the documented one for the code when None.
            detail: The ``detail`` object, or None for ``null``.
            retry_after_s: Sent as ``Retry-After`` when set.
        """
        self.status = status
        self.code = str(code)
        self.message = message if message is not None else MESSAGES.get(code, code)
        self.detail = detail
        self.retry_after_s = retry_after_s
        super().__init__(f"{status} {self.code}: {self.message}")


class HostedStepError(Exception):
    """A hosted page refused what the user did; the message is shown on the page."""


def _fail(code: str, *, detail: dict[str, Any] | None = None) -> AgenticMockError:
    """Build an operation's documented error from its code alone.

    Args:
        code: A code in ``DOCUMENTED_ERRORS`` or ``ANY_ENDPOINT_ERRORS``.
        detail: The ``detail`` object.

    Returns:
        The error, with the code's status and message.
    """
    status = ANY_ENDPOINT_ERRORS.get(code) or next(
        table[code] for table in DOCUMENTED_ERRORS.values() if code in table
    )
    return AgenticMockError(status, code, detail=detail)


def _reason(code: str, reason: str) -> AgenticMockError:
    """Build QUOTE_UNFULFILLABLE or SHIPPING_OPTION_INVALID with a reason and its message.

    Args:
        code: The error code.
        reason: The ``detail.reason``.

    Returns:
        The error.
    """
    return _fail(code, detail={"reason": reason, "message": REASON_MESSAGES[reason]})


def _invalid(on: str, path: str, message: str) -> AgenticMockError:
    """Build a ``422 VALIDATION_FAILED`` in the errors page's shape.

    Args:
        on: ``body``, ``query``, ``params`` or ``headers``.
        path: The failing field, dot-separated.
        message: What is wrong with it.

    Returns:
        The error.
    """
    return _fail(
        _E.VALIDATION_FAILED, detail={"on": on, "errors": [{"path": path, "message": message}]}
    )


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _deadline(now: datetime, seconds: float) -> datetime:
    """Set an expiry on a whole second, so the ``expiresAt`` shown is the one enforced.

    Args:
        now: The time it starts from.
        seconds: Its lifetime.

    Returns:
        The expiry, rounded down to the second.
    """
    return (now + timedelta(seconds=seconds)).replace(microsecond=0)


def _cents(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode()


def _decode_cursor(cursor: str | None, on: str) -> int:
    """Read an offset cursor; a cursor that does not decode is ``VALIDATION_FAILED``.

    Args:
        cursor: The cursor, or None for the first page.
        on: Where the cursor came from, for the error.

    Returns:
        The offset.

    Raises:
        AgenticMockError: If the cursor is not one this mock issued.
    """
    if cursor is None:
        return 0
    try:
        text = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        text = ""
    prefix, _, number = text.partition(":")
    if prefix != "offset" or not number.isdigit():
        raise AgenticMockError(
            422, _E.VALIDATION_FAILED, detail={"on": on, "message": "cursor is invalid"}
        )
    return int(number)


def _words(text: str) -> list[str]:
    """Normalise text into search words: lowercase, inner dots and hyphens dropped.

    Args:
        text: A query, a product name or a keyword.

    Returns:
        The words in order, each with a plural ``s`` taken off.
    """
    joined = re.sub(r"(?<=[A-Za-z0-9])[.\-](?=[A-Za-z0-9])", "", text.casefold())
    return [
        w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
        for w in re.findall(r"[a-z0-9]+", joined)
    ]


def _joined(words: list[str]) -> set[str]:
    """Return the words and every pair of neighbours written together.

    Args:
        words: Words in order.

    Returns:
        The words, plus ``"1tb"`` for ``["1", "tb"]``, so spacing does not matter.
    """
    return set(words) | {a + b for a, b in itertools.pairwise(words)}


def _matched(query: list[str], phrases: list[list[str]]) -> int:
    """Count the query's words found in a product's name or keywords.

    A query word counts when it is a product word, or when it and a neighbour written
    together are (``"120 mm"`` finds ``"120mm"``), or when it is two product words written
    together (``"wh1000xm5"`` finds ``"WH 1000XM5"``).

    Args:
        query: The query's words.
        phrases: The product's name and keywords, as words.

    Returns:
        How many of the query's words matched.
    """
    known = set[str]().union(*map(_joined, phrases))
    count = 0
    for index, word in enumerate(query):
        before = query[index - 1] + word if index > 0 else ""
        after = word + query[index + 1] if index + 1 < len(query) else ""
        count += word in known or before in known or after in known
    return count


# State


@dataclass
class _Enrollment:
    id: str
    owner: ClientReferenceOwner
    return_url: str
    status: EnrollmentStatus
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    payment_method: PaymentMethod | None = None


@dataclass
class _Line:
    entry: VariantEntry
    quantity: int


@dataclass
class _Quote:
    id: str
    merchant: Merchant
    lines: list[_Line]
    address: ShippingAddress | None
    offer: OfferCode | None
    shipping_id: str | None
    expires_at: datetime
    breakdown: AmountBreakdown
    checkout_id: str | None = None

    @property
    def ships(self) -> bool:
        return any(line.entry.variant.requires_shipping for line in self.lines)


@dataclass
class _Checkout:
    id: str
    quote: _Quote
    enrollment_id: str
    return_url: str
    status: CheckoutStatus
    amount: Money
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    simulated: bool = False
    approved_at: datetime | None = None
    order_id: str | None = None
    final_amount: Money | None = None


@dataclass(frozen=True)
class HostedEnrollment:
    """What the hosted card entry page shows.

    Attributes:
        id: The enrollment id.
        status: Its status now.
        email: The owner's email.
        return_url: Where the page sends the user afterwards.
        expires_at: When the page stops accepting a card.
    """

    id: str
    status: EnrollmentStatus
    email: str
    return_url: str
    expires_at: datetime


@dataclass(frozen=True)
class HostedLine:
    """One line of the order the hosted approval page shows.

    Attributes:
        name: The product and variant.
        quantity: How many.
        price: The unit price.
    """

    name: str
    quantity: int
    price: Money


@dataclass(frozen=True)
class HostedCheckout:
    """What the hosted approval page shows.

    Attributes:
        id: The checkout id.
        status: Its status now.
        merchant: The merchant's name.
        lines: The order's lines.
        breakdown: The quote's amount breakdown.
        return_url: Where the page sends the user afterwards.
        expires_at: When the page stops accepting an approval.
    """

    id: str
    status: CheckoutStatus
    merchant: str
    lines: tuple[HostedLine, ...]
    breakdown: AmountBreakdown
    return_url: str
    expires_at: datetime


@dataclass
class _Faults:
    errors: dict[Operation, deque[AgenticMockError]] = field(
        default_factory=dict[Operation, deque[AgenticMockError]]
    )
    drops: dict[Operation, int] = field(default_factory=dict[Operation, int])
    holds: dict[Operation, asyncio.Event] = field(default_factory=dict[Operation, asyncio.Event])
    failing_orders: set[str] = field(default_factory=set[str])
    final_amounts: dict[str, Decimal] = field(default_factory=dict[str, Decimal])


# The engine


class AgenticMockEngine:
    """Reap's agentic operations over a catalogue, with Reap's statuses and error codes."""

    def __init__(
        self,
        catalogue: Catalogue | None = None,
        *,
        clock: Clock = utc_now,
        config: AgenticMockConfig | None = None,
    ) -> None:
        """Create the engine.

        Args:
            catalogue: What the merchants sell; all three bundled catalogues when None.
            clock: Time source for expiry, processing and timestamps.
            config: The mock's settings; the defaults when None.
        """
        self.catalogue = catalogue if catalogue is not None else bundled_catalogue()
        self.config = config or AgenticMockConfig()
        self.enabled = True
        self._clock = clock
        self._enrollments: dict[str, _Enrollment] = {}
        self._quotes: dict[str, _Quote] = {}
        self._checkouts: dict[str, _Checkout] = {}
        self._availability: dict[str, bool] = {}
        self._orders = itertools.count(1)
        self._faults = _Faults()

    # Test hooks

    def inject_error(
        self,
        operation: Operation,
        code: str,
        *,
        detail: dict[str, Any] | None = None,
        message: str | None = None,
        retry_after_s: float | None = None,
        times: int = 1,
    ) -> None:
        """Answer the next calls of an operation with one of its documented errors.

        The error is answered before the operation runs, so nothing changes state.

        Args:
            operation: The operation.
            code: A code the operation documents, or one any endpoint can return.
            detail: The ``detail`` object; None sends ``null``.
            message: The message; the documented one when None.
            retry_after_s: The ``Retry-After`` seconds; 1 for a code that carries one.
            times: How many consecutive calls answer with it.

        Raises:
            ValueError: If the code is not documented for the operation, or ``times`` < 1.
        """
        status = DOCUMENTED_ERRORS[operation].get(code) or ANY_ENDPOINT_ERRORS.get(code)
        if status is None:
            raise ValueError(f"{operation} does not document {code}")
        if times < 1:
            raise ValueError("times must be at least 1")
        if retry_after_s is None and code in RETRY_AFTER_CODES:
            retry_after_s = 1.0
        error = AgenticMockError(status, code, message, detail=detail, retry_after_s=retry_after_s)
        queue = self._faults.errors.setdefault(operation, deque())
        queue.extend(error for _ in range(times))

    def drop_next_response(self, operation: Operation, *, times: int = 1) -> None:
        """Lose the response of the next calls after they have run, as a dropped connection.

        Args:
            operation: The operation.
            times: How many consecutive calls lose their response.
        """
        self._faults.drops[operation] = self._faults.drops.get(operation, 0) + times

    def take_drop(self, operation: Operation) -> bool:
        """Say whether this call's response is to be dropped, using up one drop.

        Args:
            operation: The operation being answered.

        Returns:
            True if the response must not reach the client.
        """
        left = self._faults.drops.get(operation, 0)
        if left:
            self._faults.drops[operation] = left - 1
        return left > 0

    def hold_next(self, operation: Operation) -> asyncio.Event:
        """Hold the next call of an operation in flight until the returned event is set.

        Args:
            operation: The operation.

        Returns:
            The event that releases the held call.
        """
        gate = asyncio.Event()
        self._faults.holds[operation] = gate
        return gate

    def take_hold(self, operation: Operation) -> asyncio.Event | None:
        """Return, once, the event a call of this operation must wait for.

        Args:
            operation: The operation being answered.

        Returns:
            The event, or None when the call runs straight through.
        """
        return self._faults.holds.pop(operation, None)

    def set_variant_available(self, variant_id: str, *, available: bool) -> None:
        """Mark a variant in or out of stock, overriding the catalogue.

        Args:
            variant_id: The variant.
            available: Whether it is in stock.
        """
        self.catalogue.variant(variant_id)
        self._availability[variant_id] = available

    def fail_order(self, quote_id: str) -> None:
        """Make the merchant order behind this quote's checkout fail: ``FAILED``.

        Args:
            quote_id: The quote.
        """
        self._quote(quote_id)
        self._faults.failing_orders.add(quote_id)

    def override_final_amount(self, quote_id: str, amount: Decimal) -> None:
        """Charge a different final amount than the quote's when its checkout completes.

        Args:
            quote_id: The quote.
            amount: The amount the completed checkout reports.
        """
        self._quote(quote_id)
        self._faults.final_amounts[quote_id] = amount

    def _quote(self, quote_id: str) -> _Quote:
        quote = self._quotes.get(quote_id)
        if quote is None:
            raise KeyError(quote_id)
        return quote

    def _enter(self, operation: Operation) -> None:
        """Run the checks every operation starts with.

        Args:
            operation: The operation starting.

        Raises:
            AgenticMockError: The project is not enabled, or an error was injected.
        """
        if not self.enabled:
            raise _fail(_E.AGENTIC_PAYMENTS_NOT_ENABLED)
        queue = self._faults.errors.get(operation)
        if queue:
            raise queue.popleft()

    def _available(self, variant: CatalogueVariant) -> bool:
        return self._availability.get(variant.id, variant.available)

    def _hosted(self, kind: str, resource_id: str) -> str:
        return f"{self.config.hosted_base_url.rstrip('/')}/hosted/{kind}/{resource_id}"

    # Enrollments

    def create_enrollment(self, req: CreateEnrollmentRequest) -> EnrollmentCreated:
        """``POST /agentic/enrollments``: store a card behind a hosted card entry step.

        Args:
            req: The request.

        Returns:
            The enrollment, ``REQUIRES_ACTION`` with the hosted page in ``nextAction``.

        Raises:
            AgenticMockError: A source other than ``EXTERNAL``, which Reap calls "coming soon".
        """
        self._enter(Operation.CREATE_ENROLLMENT)
        if not isinstance(req, CreateExternalEnrollmentRequest):
            raise AgenticMockError(
                400,
                _E.AGENTIC_REQUEST_REJECTED,
                f"{req.source} enrollment is coming soon",
                detail={"field": "source"},
            )
        now = self._clock()
        enrollment = _Enrollment(
            id=str(uuid.uuid4()),
            owner=req.owner,
            return_url=req.presentation.return_url,
            status=EnrollmentStatus.REQUIRES_ACTION,
            expires_at=_deadline(now, self.config.enrollment_ttl_s),
            created_at=now,
            updated_at=now,
        )
        self._enrollments[enrollment.id] = enrollment
        return ExternalEnrollmentCreated(
            id=enrollment.id,
            status=enrollment.status,
            source="EXTERNAL",
            owner=enrollment.owner,
            next_action=self._enrollment_action(enrollment),
        )

    def _enrollment(self, enrollment_id: str) -> _Enrollment | None:
        enrollment = self._enrollments.get(enrollment_id)
        if enrollment is not None:
            self._expire(enrollment)
        return enrollment

    def _expire(self, enrollment: _Enrollment) -> None:
        """Expire an enrollment whose hosted step was not finished in time.

        Args:
            enrollment: The enrollment.
        """
        if (
            enrollment.status == EnrollmentStatus.REQUIRES_ACTION
            and self._clock() >= enrollment.expires_at
        ):
            enrollment.status = EnrollmentStatus.EXPIRED
            enrollment.updated_at = enrollment.expires_at

    def _enrollment_action(self, enrollment: _Enrollment) -> NextAction | None:
        if enrollment.status != EnrollmentStatus.REQUIRES_ACTION:
            return None
        return NextAction(
            type="REDIRECT",
            url=self._hosted("enrollments", enrollment.id),
            expires_at=_iso(enrollment.expires_at),
        )

    def _enrollment_wire(self, enrollment: _Enrollment) -> Enrollment:
        return Enrollment(
            id=enrollment.id,
            status=enrollment.status,
            owner=EnrollmentOwner(
                type="CLIENT_REFERENCE", id=enrollment.owner.id, email=enrollment.owner.email
            ),
            payment_method=enrollment.payment_method,
            next_action=self._enrollment_action(enrollment),
            created_at=_iso(enrollment.created_at),
            updated_at=_iso(enrollment.updated_at),
        )

    def get_enrollment(self, enrollment_id: str) -> Enrollment:
        """``GET /agentic/enrollments/{id}``.

        Args:
            enrollment_id: The enrollment.

        Returns:
            The enrollment as it stands.

        Raises:
            AgenticMockError: ``ENROLLMENT_NOT_FOUND``.
        """
        self._enter(Operation.GET_ENROLLMENT)
        enrollment = self._enrollment(enrollment_id)
        if enrollment is None:
            raise _fail(_E.ENROLLMENT_NOT_FOUND)
        return self._enrollment_wire(enrollment)

    def list_enrollments(
        self,
        *,
        owner_id: str,
        owner_type: Literal["REAP_USER", "CLIENT_REFERENCE"] = "CLIENT_REFERENCE",
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[Enrollment]:
        """``GET /agentic/enrollments``: one owner's enrollments, oldest first.

        Args:
            owner_id: The owner.
            owner_type: Reap's default is ``CLIENT_REFERENCE``.
            limit: Page size, 1 to 100.
            cursor: ``nextCursor`` of the previous page.

        Returns:
            One page.
        """
        self._enter(Operation.LIST_ENROLLMENTS)
        offset = _decode_cursor(cursor, "query")
        owned = [
            e
            for e in self._enrollments.values()
            if owner_type == "CLIENT_REFERENCE" and e.owner.id == owner_id
        ]
        page = owned[offset : offset + limit]
        more = offset + limit < len(owned)
        for enrollment in page:
            self._expire(enrollment)
        return Page[Enrollment](
            items=[self._enrollment_wire(e) for e in page],
            next_cursor=_encode_cursor(offset + limit) if more else None,
        )

    def revoke_enrollment(self, enrollment_id: str) -> Enrollment:
        """``POST /agentic/enrollments/{id}/revoke``: "Revoking is final".

        Args:
            enrollment_id: The enrollment.

        Returns:
            The enrollment, ``REVOKED``.

        Raises:
            AgenticMockError: ``ENROLLMENT_NOT_FOUND``, or ``ENROLLMENT_NOT_ACTIVE``.
        """
        self._enter(Operation.REVOKE_ENROLLMENT)
        enrollment = self._enrollment(enrollment_id)
        if enrollment is None:
            raise _fail(_E.ENROLLMENT_NOT_FOUND)
        self._require_active(enrollment)
        enrollment.status = EnrollmentStatus.REVOKED
        enrollment.updated_at = self._clock()
        return self._enrollment_wire(enrollment)

    @staticmethod
    def _require_active(enrollment: _Enrollment) -> None:
        if enrollment.status != EnrollmentStatus.ACTIVE:
            waiting = enrollment.status == EnrollmentStatus.REQUIRES_ACTION
            raise _fail(
                _E.ENROLLMENT_NOT_ACTIVE,
                detail={"reason": "CARD_NOT_CAPTURED"} if waiting else None,
            )

    def enrollment_page(self, enrollment_id: str) -> HostedEnrollment:
        """What the hosted card entry page shows.

        Args:
            enrollment_id: The enrollment.

        Returns:
            The page's content.

        Raises:
            HostedStepError: There is no such enrollment.
        """
        enrollment = self._enrollment(enrollment_id)
        if enrollment is None:
            raise HostedStepError(f"There is no enrollment {enrollment_id}.")
        return HostedEnrollment(
            id=enrollment.id,
            status=enrollment.status,
            email=enrollment.owner.email,
            return_url=enrollment.return_url,
            expires_at=enrollment.expires_at,
        )

    def submit_card(
        self, enrollment_id: str, *, number: str, cvc: str, expiry: str, otp: str
    ) -> str:
        """The hosted card entry step: a published test card and the one-time password.

        Args:
            enrollment_id: The enrollment.
            number: The card number, spaces allowed.
            cvc: The CVC.
            expiry: The expiry as ``MM/YY``.
            otp: The one-time password.

        Returns:
            The return URL the page sends the user to.

        Raises:
            HostedStepError: The page is closed, or the card or password is wrong.
        """
        page = self.enrollment_page(enrollment_id)
        enrollment = self._enrollments[page.id]
        if enrollment.status != EnrollmentStatus.REQUIRES_ACTION:
            closed = "has expired" if enrollment.status == EnrollmentStatus.EXPIRED else "is closed"
            raise HostedStepError(f"This card entry page {closed} ({enrollment.status}).")
        digits = re.sub(r"[\s-]", "", number)
        card = next((known for known in TEST_CARDS if known.replace(" ", "") == digits), None)
        if card is None or TEST_CARDS[card] != (cvc.strip(), expiry.strip()):
            raise HostedStepError(
                "Card declined: enter one of the published test cards with its CVC and expiry."
            )
        if otp.strip() != TEST_OTP:
            raise HostedStepError("The one-time password is wrong.")
        month, year = TEST_CARDS[card][1].split("/")
        enrollment.status = EnrollmentStatus.ACTIVE
        enrollment.payment_method = PaymentMethod(
            type="CARD",
            network=_TEST_CARD_NETWORK,
            last4=digits[-4:],
            expiry_month=int(month),
            expiry_year=2000 + int(year),
        )
        enrollment.updated_at = self._clock()
        return enrollment.return_url

    # Products

    def search_products(self, req: ProductSearchRequest) -> ProductSearchResponse:
        """``POST /agentic/products/search``.

        A product matches when at least half the query's words (and at least one) are in
        its name or keywords; matches rank by how many words they share, then by preview
        price. ``PREFER`` ranks the merchant's products first; ``ONLY`` keeps only them.

        Args:
            req: The query, merchant preference, context, filters and paging.

        Returns:
            One page of products.

        Raises:
            AgenticMockError: ``MERCHANT_NOT_RESOLVED``, or a cursor that does not decode.
        """
        self._enter(Operation.SEARCH)
        preferred: Merchant | None = None
        if req.merchant_preference is not None:
            preferred = self.catalogue.merchant(req.merchant_preference.merchant_name)
            if preferred is None:
                raise _fail(_E.MERCHANT_NOT_RESOLVED)
        only = req.merchant_preference is not None and req.merchant_preference.mode == "ONLY"
        offset = _decode_cursor(req.pagination.cursor if req.pagination else None, "body")
        limit = req.pagination.limit if req.pagination and req.pagination.limit else 20
        currency = req.context.currency if req.context else None
        query = _words(req.query)
        needed = max(1, math.ceil(len(query) / 2))
        ranked: list[tuple[bool, int, Decimal, int, ProductSummary]] = []
        other_currency = 0
        for position, entry in enumerate(self.catalogue.products()):
            if only and entry.merchant is not preferred:
                continue
            score = _matched(
                query, [_words(entry.product.name), *map(_words, entry.product.keywords)]
            )
            if score < needed:
                continue
            if currency is not None and currency.upper() != entry.merchant.currency:
                other_currency += 1
                continue
            summary = self._summary(entry, req)
            if summary is None:
                continue
            price = summary.preview_variant.price.amount if summary.preview_variant else 0
            ranked.append(
                (entry.merchant is not preferred, -score, Decimal(price), position, summary)
            )
        ranked.sort(key=lambda row: row[:4])
        products = [row[4] for row in ranked]
        page = products[offset : offset + limit]
        more = offset + limit < len(products)
        warnings = (
            [
                f"{other_currency} matching products are priced in another currency than "
                f"{currency}; the mock converts no currency and left them out."
            ]
            if other_currency
            else []
        )
        return ProductSearchResponse(
            id=str(uuid.uuid4()),
            products=page,
            pagination=SearchPage(
                next_cursor=_encode_cursor(offset + limit) if more else None,
                has_next_page=more,
                returned_count=len(page),
            ),
            warnings=warnings,
        )

    def _summary(self, entry: ProductEntry, req: ProductSearchRequest) -> ProductSummary | None:
        """Summarise a matching product, or drop it when no variant passes the filters.

        Args:
            entry: The product.
            req: The search, for its filters.

        Returns:
            The search result, or None.
        """
        filters = req.filters
        band = filters.price if filters else None
        available_only = filters is not None and filters.availability == "AVAILABLE_ONLY"
        variants = entry.product.variants

        def passes(variant: CatalogueVariant) -> bool:
            if available_only and not self._available(variant):
                return False
            if band is not None and band.min is not None and variant.price < band.min:
                return False
            return not (band is not None and band.max is not None and variant.price > band.max)

        passing = [v for v in variants if passes(v)]
        if not passing:
            return None
        default = entry.product.default
        preview = default if default in passing else min(passing, key=lambda v: v.price)
        currency = entry.merchant.currency
        return ProductSummary(
            id=entry.product.id,
            merchant=MerchantName(name=entry.merchant.name),
            name=entry.product.name,
            price_range=PriceRange(
                min=Money(amount=min(v.price for v in variants), currency=currency),
                max=Money(amount=max(v.price for v in variants), currency=currency),
            ),
            available=any(self._available(v) for v in variants),
            preview_variant=PreviewVariant(
                id=preview.id,
                name=preview.name,
                price=Money(amount=preview.price, currency=currency),
                available=self._available(preview),
            ),
        )

    def _variant_wire(self, merchant: Merchant, variant: CatalogueVariant) -> Variant:
        return Variant(
            id=variant.id,
            name=variant.name,
            options=[VariantOption(name=k, value=v) for k, v in variant.options.items()],
            price=Money(amount=variant.price, currency=merchant.currency),
            available=self._available(variant),
            requires_shipping=variant.requires_shipping,
            media=[],
        )

    def product_details(self, req: ProductDetailsRequest) -> ProductDetailsResponse:
        """``POST /agentic/products/details``; an unknown id is an entry in ``errors``.

        Args:
            req: 1 to 10 product ids.

        Returns:
            The products, and an error per id that did not resolve.
        """
        self._enter(Operation.DETAILS)
        products: list[ProductDetail] = []
        errors: list[ProductError] = []
        for product_id in req.product_ids:
            entry = self.catalogue.find_product(product_id)
            if entry is None:
                errors.append(
                    ProductError(
                        product_id=product_id,
                        code=_E.AGENTIC_RESOURCE_NOT_FOUND,
                        message=MESSAGES[_E.AGENTIC_RESOURCE_NOT_FOUND],
                    )
                )
                continue
            product = entry.product
            ids = product.option_ids
            options = [
                ProductOption(
                    name=group,
                    values=[
                        OptionValue(
                            option_id=ids[group, value],
                            label=value,
                            available=any(
                                self._available(v)
                                for v in product.variants
                                if v.options[group] == value
                            ),
                        )
                        for value in values
                    ],
                )
                for group, values in product.option_groups.items()
            ]
            products.append(
                ProductDetail(
                    id=product.id,
                    merchant=MerchantName(name=entry.merchant.name),
                    name=product.name,
                    description=product.description,
                    media=[],
                    options=options,
                    default_variant=self._variant_wire(entry.merchant, product.default),
                )
            )
        return ProductDetailsResponse(products=products, errors=errors)

    def resolve_variant(self, req: ResolveVariantRequest) -> Variant:
        """``POST /agentic/products/variant``: exactly one value in every option group.

        A sold-out variant resolves, with ``available`` false, as the guide expects ("Stop
        here when ``available`` reads ``false``").

        Args:
            req: The product and the chosen option ids.

        Returns:
            The variant, the only id a quote accepts.

        Raises:
            AgenticMockError: ``AGENTIC_RESOURCE_NOT_FOUND`` for an unknown product,
                ``VARIANT_RESOLUTION_FAILED`` when the options name no single variant.
        """
        self._enter(Operation.VARIANT)
        entry = self.catalogue.find_product(req.product_id)
        if entry is None:
            raise _fail(_E.AGENTIC_RESOURCE_NOT_FOUND)
        by_id = {option_id: key for key, option_id in entry.product.option_ids.items()}
        chosen: dict[str, str] = {}
        for option_id in req.option_ids:
            key = by_id.get(option_id)
            if key is None or key[0] in chosen:
                raise _fail(_E.VARIANT_RESOLUTION_FAILED)
            chosen[key[0]] = key[1]
        variant = next((v for v in entry.product.variants if v.options == chosen), None)
        if variant is None:
            raise _fail(_E.VARIANT_RESOLUTION_FAILED)
        return self._variant_wire(entry.merchant, variant)

    # Quotes

    def create_quote(self, req: CreateQuoteRequest) -> Quote:
        """``POST /agentic/quotes``: price a cart of variants or a checkout URL.

        Args:
            req: Items or an external checkout, with email, address and offer code.

        Returns:
            The quote, with the preselected shipping option and ``expiresAt``.

        Raises:
            AgenticMockError: Any of the operation's documented refusals.
        """
        self._enter(Operation.CREATE_QUOTE)
        if isinstance(req, CreateExternalCheckoutQuoteRequest):
            merchant, lines = self._cart_from_url(
                req.external_checkout.merchant_domain, req.external_checkout.checkout_url
            )
        else:
            merchant, lines = self._cart_from_items([(i.variant_id, i.quantity) for i in req.items])
        if not merchant.card_payment:
            raise _fail(_E.CARD_PAYMENT_UNAVAILABLE)
        if any(not self._available(line.entry.variant) for line in lines):
            raise _fail(_E.VARIANT_UNAVAILABLE)
        offer = self._offer(merchant, req.offer_code)
        ships = any(line.entry.variant.requires_shipping for line in lines)
        if ships:
            if req.shipping_address is None:
                raise _invalid(
                    "body", "shippingAddress", "Required when any item requires shipping"
                )
            _check_address(merchant, req.shipping_address)
        preselected = merchant.preselected_shipping if ships else None
        now = self._clock()
        quote = _Quote(
            id=str(uuid.uuid4()),
            merchant=merchant,
            lines=lines,
            address=req.shipping_address,
            offer=offer,
            shipping_id=preselected.id if preselected else None,
            expires_at=_deadline(now, self.config.quote_ttl_s),
            breakdown=AmountBreakdown(
                items_subtotal=Money(amount=Decimal(0), currency=merchant.currency),
                final_amount=Money(amount=Decimal(0), currency=merchant.currency),
            ),
        )
        quote.breakdown = self._price(quote)
        self._quotes[quote.id] = quote
        return self._quote_wire(quote)

    def _cart_from_items(self, items: list[tuple[str, int]]) -> tuple[Merchant, list[_Line]]:
        """Resolve item lines to variants of one merchant, merging repeated variants.

        Args:
            items: Variant id and quantity per line.

        Returns:
            The merchant and the lines.

        Raises:
            AgenticMockError: An id that is not a variant, or variants of two merchants.
        """
        lines: dict[str, _Line] = {}
        for index, (variant_id, quantity) in enumerate(items):
            entry = self.catalogue.find_variant(variant_id)
            if entry is None:
                raise AgenticMockError(
                    400,
                    _E.AGENTIC_REQUEST_REJECTED,
                    f"{variant_id} is not a variant id",
                    detail={"path": f"items.{index}.variantId"},
                )
            if variant_id in lines:
                lines[variant_id].quantity += quantity
            else:
                lines[variant_id] = _Line(entry, quantity)
        merchants = {line.entry.merchant.name for line in lines.values()}
        if len(merchants) > 1:
            raise AgenticMockError(
                400,
                _E.AGENTIC_REQUEST_REJECTED,
                "A quote's items must come from one merchant",
                detail={"path": "items"},
            )
        first = next(iter(lines.values()))
        return first.entry.merchant, list(lines.values())

    def _cart_from_url(self, domain: str, url: str) -> tuple[Merchant, list[_Line]]:
        """Read a cart from a checkout URL of the form ``https://<domain>/cart/<id>:<qty>,...``.

        That is the shape of the guide's example
        (``https://merchant.example/cart/variant-1:1?attributes[...]=...``).

        Args:
            domain: ``externalCheckout.merchantDomain``.
            url: ``externalCheckout.checkoutUrl``.

        Returns:
            The merchant and the lines.

        Raises:
            AgenticMockError: ``CHECKOUT_URL_INVALID`` with the reason.
        """
        merchant = self.catalogue.merchant_by_domain(domain)
        if merchant is None or not merchant.external_checkout:
            raise _fail(_E.CHECKOUT_URL_INVALID, detail={"reason": "MERCHANT_CONTEXT_UNVERIFIED"})
        parts = urlsplit(url)
        cart = re.fullmatch(r"/cart/([^/]+)", parts.path)
        on_domain = parts.scheme == "https" and (parts.hostname or "") == domain.casefold()
        items: list[tuple[str, int]] = []
        for piece in cart.group(1).split(",") if cart and on_domain else []:
            variant_id, _, quantity = piece.rpartition(":")
            if not variant_id or not quantity.isdigit() or int(quantity) < 1:
                items = []
                break
            items.append((variant_id, int(quantity)))
        if not items:
            raise _fail(_E.CHECKOUT_URL_INVALID, detail={"reason": "INVALID"})
        lines: list[_Line] = []
        for variant_id, quantity in items:
            entry = self.catalogue.find_variant(variant_id)
            if entry is None or entry.merchant is not merchant:
                raise _fail(_E.CHECKOUT_URL_INVALID, detail={"reason": "NOT_FOUND"})
            lines.append(_Line(entry, quantity))
        return merchant, lines

    def _offer(self, merchant: Merchant, code: str | None) -> OfferCode | None:
        """Find an offer code the merchant accepts now.

        Args:
            merchant: The merchant.
            code: The code, or None.

        Returns:
            The offer, or None when no code was sent.

        Raises:
            AgenticMockError: ``OFFER_CODE_INVALID`` or ``OFFER_CODE_EXPIRED``.
        """
        if code is None:
            return None
        offer = next((o for o in merchant.offer_codes if o.code == code), None)
        if offer is None:
            raise _fail(_E.OFFER_CODE_INVALID)
        self._check_offer(offer)
        return offer

    def _check_offer(self, offer: OfferCode | None) -> None:
        if offer is not None and offer.expires_at is not None and self._clock() >= offer.expires_at:
            raise _fail(_E.OFFER_CODE_EXPIRED)

    def _price(self, quote: _Quote) -> AmountBreakdown:
        """Price a quote: items, less discounts, plus shipping, tax and charges.

        Args:
            quote: The quote.

        Returns:
            The amount breakdown; ``finalAmount`` is the full sum of the parts.
        """
        merchant = quote.merchant

        def money(amount: Decimal) -> Money:
            return Money(amount=_cents(amount), currency=merchant.currency)

        subtotal = sum(
            (line.entry.variant.price * line.quantity for line in quote.lines), Decimal(0)
        )
        discounts: list[NamedAmount] = []
        if quote.offer is not None:
            off = (
                subtotal * quote.offer.percent_off / 100
                if quote.offer.percent_off is not None
                else min(quote.offer.amount_off or Decimal(0), subtotal)
            )
            discounts.append(NamedAmount(name=f"Offer code {quote.offer.code}", amount=money(off)))
        taxable = subtotal - sum((d.amount.amount for d in discounts), Decimal(0))
        country = (
            quote.address.country
            if quote.address is not None and quote.address.country in merchant.countries
            else merchant.default_country
        )
        rules = merchant.countries[country]
        tax = _cents(
            taxable - taxable / (1 + rules.tax_rate)
            if rules.tax_included
            else taxable * rules.tax_rate
        )
        rate = next((o for o in merchant.shipping_options if o.id == quote.shipping_id), None)
        shipping = rate.price if rate is not None else None
        charges = [
            NamedAmount(name=c.name, amount=money(c.amount)) for c in merchant.additional_charges
        ]
        final = (
            taxable
            + (shipping or Decimal(0))
            + (Decimal(0) if rules.tax_included else tax)
            + sum((c.amount.amount for c in charges), Decimal(0))
        )
        return AmountBreakdown(
            items_subtotal=money(subtotal),
            shipping=money(shipping) if shipping is not None else None,
            tax=Tax(amount=money(tax), included_in_prices=rules.tax_included),
            discounts=discounts,
            additional_charges=charges,
            final_amount=money(final),
        )

    def _quote_wire(self, quote: _Quote) -> Quote:
        options = (
            [
                ShippingOption(
                    id=o.id,
                    name=o.name,
                    selected=o.id == quote.shipping_id,
                    price=Money(amount=_cents(o.price), currency=quote.merchant.currency),
                    details=[ShippingOptionDetail(key=k, value=v) for k, v in o.details.items()],
                )
                for o in quote.merchant.shipping_options
            ]
            if quote.ships
            else []
        )
        return Quote(
            id=quote.id,
            shipping_options=options,
            amount_breakdown=quote.breakdown,
            expires_at=_iso(quote.expires_at),
        )

    def get_quote(self, quote_id: str) -> Quote:
        """``GET /agentic/quotes/{id}``: the quote as last priced.

        Args:
            quote_id: The quote.

        Returns:
            The quote.

        Raises:
            AgenticMockError: ``QUOTE_NOT_FOUND``.
        """
        self._enter(Operation.GET_QUOTE)
        quote = self._quotes.get(quote_id)
        if quote is None:
            raise _fail(_E.QUOTE_NOT_FOUND)
        return self._quote_wire(quote)

    def select_shipping_option(self, quote_id: str, req: SelectShippingOptionRequest) -> Quote:
        """``POST /agentic/quotes/{id}/shipping-option``: choose and re-price.

        ``expiresAt`` does not move.

        Args:
            quote_id: The quote.
            req: The shipping option.

        Returns:
            The quote with a fresh amount breakdown.

        Raises:
            AgenticMockError: ``QUOTE_NOT_FOUND``, ``QUOTE_EXPIRED``, ``QUOTE_NOT_MUTABLE``
                once a checkout was opened, ``QUOTE_REPLACEMENT_REQUIRED`` once an item sold
                out, ``SHIPPING_OPTION_INVALID``, or ``OFFER_CODE_EXPIRED``.
        """
        self._enter(Operation.SELECT_SHIPPING_OPTION)
        quote = self._quotes.get(quote_id)
        if quote is None:
            raise _fail(_E.QUOTE_NOT_FOUND)
        if self._clock() >= quote.expires_at:
            raise _fail(_E.QUOTE_EXPIRED)
        if quote.checkout_id is not None:
            raise _fail(_E.QUOTE_NOT_MUTABLE)
        if any(not self._available(line.entry.variant) for line in quote.lines):
            raise _fail(_E.QUOTE_REPLACEMENT_REQUIRED)
        known = {o.id for o in quote.merchant.shipping_options} if quote.ships else set[str]()
        if req.shipping_option_id not in known:
            raise _fail(_E.SHIPPING_OPTION_INVALID)
        self._check_offer(quote.offer)
        quote.shipping_id = req.shipping_option_id
        quote.breakdown = self._price(quote)
        return self._quote_wire(quote)

    # Checkouts

    def create_checkout(
        self, req: CreateCheckoutRequest, *, simulate: str | None = None
    ) -> CheckoutCreated:
        """``POST /agentic/checkouts``: open the payment for an unexpired quote.

        Args:
            req: The quote, the enrollment and the return URL.
            simulate: The ``X-Simulate-Checkout`` header's value, if sent.

        Returns:
            The checkout: ``REQUIRES_ACTION`` with the hosted approval page, or under the
            sandbox header ``COMPLETED`` (or as ``SimulatedCreate`` says).

        Raises:
            AgenticMockError: The header in production, ``QUOTE_NOT_FOUND``,
                ``ENROLLMENT_NOT_FOUND``, ``ENROLLMENT_NOT_ACTIVE``, ``QUOTE_EXPIRED``, or a
                quote that already has a checkout.
        """
        self._enter(Operation.CREATE_CHECKOUT)
        if simulate is not None and self.config.environment == Environment.PRODUCTION:
            raise AgenticMockError(
                400,
                _E.AGENTIC_REQUEST_REJECTED,
                "X-Simulate-Checkout is rejected in production",
                detail={"header": "X-Simulate-Checkout"},
            )
        quote = self._quotes.get(req.quote_id)
        if quote is None:
            raise _fail(_E.QUOTE_NOT_FOUND)
        enrollment = self._enrollment(req.enrollment_id)
        if enrollment is None:
            raise _fail(_E.ENROLLMENT_NOT_FOUND)
        self._require_active(enrollment)
        now = self._clock()
        if now >= quote.expires_at:
            raise _fail(_E.QUOTE_EXPIRED)
        if quote.checkout_id is not None:
            raise AgenticMockError(
                400,
                _E.AGENTIC_REQUEST_REJECTED,
                "This quote already has a checkout; create a new quote",
                detail={"field": "quoteId"},
            )
        checkout = _Checkout(
            id=str(uuid.uuid4()),
            quote=quote,
            enrollment_id=enrollment.id,
            return_url=req.presentation.return_url,
            status=CheckoutStatus.REQUIRES_ACTION,
            amount=quote.breakdown.final_amount,
            expires_at=_deadline(now, self.config.approval_ttl_s),
            created_at=now,
            updated_at=now,
            simulated=simulate is not None,
        )
        quote.checkout_id = checkout.id
        self._checkouts[checkout.id] = checkout
        if checkout.simulated and self.config.simulated_create == SimulatedCreate.COMPLETED:
            self._place_order(checkout)
        return CheckoutCreated(
            id=checkout.id,
            status=checkout.status,
            quote_id=quote.id,
            enrollment_id=enrollment.id,
            amount=checkout.amount,
            next_action=self._checkout_action(checkout),
        )

    def _place_order(self, checkout: _Checkout, at: datetime | None = None) -> None:
        """Settle a checkout: the merchant places the order, or it fails.

        Args:
            checkout: The checkout, approved or simulated.
            at: When it settled; now when None.
        """
        quote = checkout.quote
        sold_out = any(not self._available(line.entry.variant) for line in quote.lines)
        checkout.updated_at = at or self._clock()
        if sold_out or quote.id in self._faults.failing_orders:
            checkout.status = CheckoutStatus.FAILED
            return
        override = self._faults.final_amounts.get(quote.id)
        checkout.status = CheckoutStatus.COMPLETED
        checkout.order_id = f"{_ORDER_PREFIX}{next(self._orders):06d}"
        checkout.final_amount = (
            Money(amount=_cents(override), currency=checkout.amount.currency)
            if override is not None
            else checkout.amount
        )

    def _checkout(self, checkout_id: str) -> _Checkout | None:
        """Find a checkout and move it along the clock.

        Args:
            checkout_id: The checkout.

        Returns:
            The checkout, or None.
        """
        checkout = self._checkouts.get(checkout_id)
        if checkout is None:
            return None
        now = self._clock()
        if checkout.status == CheckoutStatus.REQUIRES_ACTION:
            if checkout.simulated:
                self._place_order(checkout)
            elif now >= checkout.expires_at:
                checkout.status = CheckoutStatus.EXPIRED
                checkout.updated_at = checkout.expires_at
        elif checkout.status == CheckoutStatus.PROCESSING and checkout.approved_at is not None:
            settles = checkout.approved_at + timedelta(seconds=self.config.processing_s)
            if now >= settles:
                self._place_order(checkout, settles)
        return checkout

    def _checkout_action(self, checkout: _Checkout) -> NextAction | None:
        if checkout.status != CheckoutStatus.REQUIRES_ACTION:
            return None
        return NextAction(
            type="REDIRECT",
            url=self._hosted("checkouts", checkout.id),
            expires_at=_iso(checkout.expires_at),
        )

    def get_checkout(self, checkout_id: str) -> Checkout:
        """``GET /agentic/checkouts/{id}``.

        Args:
            checkout_id: The checkout.

        Returns:
            The checkout; when ``COMPLETED`` it carries ``orderId`` and ``finalAmount``.

        Raises:
            AgenticMockError: ``CHECKOUT_NOT_FOUND``.
        """
        self._enter(Operation.GET_CHECKOUT)
        checkout = self._checkout(checkout_id)
        if checkout is None:
            raise _fail(_E.CHECKOUT_NOT_FOUND)
        return Checkout(
            id=checkout.id,
            status=checkout.status,
            quote_id=checkout.quote.id,
            enrollment_id=checkout.enrollment_id,
            order_id=checkout.order_id,
            final_amount=checkout.final_amount,
            next_action=self._checkout_action(checkout),
            created_at=_iso(checkout.created_at),
            updated_at=_iso(checkout.updated_at),
        )

    def checkout_page(self, checkout_id: str) -> HostedCheckout:
        """What the hosted approval page shows.

        Args:
            checkout_id: The checkout.

        Returns:
            The page's content.

        Raises:
            HostedStepError: There is no such checkout.
        """
        checkout = self._checkout(checkout_id)
        if checkout is None:
            raise HostedStepError(f"There is no checkout {checkout_id}.")
        quote = checkout.quote
        return HostedCheckout(
            id=checkout.id,
            status=checkout.status,
            merchant=quote.merchant.name,
            lines=tuple(
                HostedLine(
                    name=", ".join(
                        filter(None, [line.entry.product.name, line.entry.variant.name])
                    ),
                    quantity=line.quantity,
                    price=Money(
                        amount=_cents(line.entry.variant.price), currency=quote.merchant.currency
                    ),
                )
                for line in quote.lines
            ),
            breakdown=quote.breakdown,
            return_url=checkout.return_url,
            expires_at=checkout.expires_at,
        )

    def approve_checkout(self, checkout_id: str) -> str:
        """The hosted approval step: the user approves the charge; ``PROCESSING`` follows.

        Args:
            checkout_id: The checkout.

        Returns:
            The return URL the page sends the user to.

        Raises:
            HostedStepError: There is no such checkout, or it no longer awaits approval.
        """
        page = self.checkout_page(checkout_id)
        if page.status != CheckoutStatus.REQUIRES_ACTION:
            raise HostedStepError(f"This checkout is {page.status} and cannot be approved.")
        checkout = self._checkouts[page.id]
        checkout.status = CheckoutStatus.PROCESSING
        checkout.approved_at = checkout.updated_at = self._clock()
        return checkout.return_url


def _check_address(merchant: Merchant, address: ShippingAddress) -> None:
    """Apply the merchant's rules for the address's country.

    Args:
        merchant: The merchant.
        address: The shipping address.

    Raises:
        AgenticMockError: ``QUOTE_UNFULFILLABLE`` with the first rule that fails.
    """
    rules = merchant.countries.get(address.country)
    if rules is None or address.country not in merchant.ships_to:
        raise _reason(_E.QUOTE_UNFULFILLABLE, "ITEMS_UNSHIPPABLE")
    if rules.region_required and not address.region:
        raise _reason(_E.QUOTE_UNFULFILLABLE, "STATE_OR_PROVINCE_REQUIRED")
    if rules.address_line2_required and not address.address_line2:
        raise _reason(_E.QUOTE_UNFULFILLABLE, "ADDRESS_LINE_2_REQUIRED")
    if not address.phone.startswith(f"+{rules.calling_code}"):
        raise _reason(_E.QUOTE_UNFULFILLABLE, "INVALID_PHONE")

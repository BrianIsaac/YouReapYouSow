"""Kwal participant contract wire models, transcribed from the Kwal skill's own helpers.

Source: https://github.com/payward/kwal-skill at commit
``be5f52c90edea8d2dbb07e38395af2388ae10ce1``, the "published Kwal participant v1
contract" (``skills/agent-payment/scripts/session.py:22``): the routes under
``/kwal/participant/v1/`` and the fields its helpers read in ``catalog.py``,
``quotes.py``, ``checkout.py``, ``vault.py``, ``amounts.py`` and ``transport.py``. Each
model names the helper it follows. How the control plane uses them:
``docs/kwal-backend.md``.

Field names are snake_case in Python and camelCase on the wire. proto3 JSON omits a zero,
a false and an empty list, so an absent amount reads as zero, an absent flag as false and
an absent list as empty, as the skill reads them (``_fields.py``, ``amounts.py``). An
enum is stated by its full name on the wire (``PARTICIPANT_PAYMENT_STATE_COMPLETED``).
The checks the skill makes on a response that decides what happens next are made here
too, so a body it would refuse is refused (a quote without a total, a waiting payment
without an https link, a ready setup without its card and deposit).
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

ERROR_TYPE_PREFIX = "tag:kraken.com,2025:"
"""Every typed participant error is ``tag:kraken.com,2025:<Tag>`` (``transport.py:52``)."""

USDC = "USDC"
USDC_DECIMALS = 6
"""Funding amounts are "USDC with 6 decimals" (``vault.py:426-428``)."""

_MAX_INT64 = 2**63 - 1


def _whole_units(value: str) -> str:
    """Accept minor units only as ASCII decimal digits, as ``_fields._whole_number`` does.

    Args:
        value: The minor units as sent.

    Returns:
        The value, unchanged.

    Raises:
        ValueError: If it is not a whole number in ASCII digits.
    """
    if not value.isascii() or not value.isdecimal():
        raise ValueError("minor units must be a whole number in ASCII digits")
    return value


def _instant(value: object) -> object:
    """Read a proto3 int64 instant, which arrives as a decimal string or a number.

    Args:
        value: The raw value.

    Returns:
        An int when the text is decimal digits, else the value for pydantic to refuse.
    """
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return value


def _https(value: str | None) -> str | None:
    """Accept a link a person can open safely: https, a host, no userinfo.

    Args:
        value: The link.

    Returns:
        The link, unchanged.

    Raises:
        ValueError: If it is not such a link (``checkout.py:64-81``).
    """
    if value is None:
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        raise ValueError("an approval link must be an https URL without userinfo")
    return value


type MinorUnits = Annotated[str, AfterValidator(_whole_units)]
type Instant = Annotated[int, BeforeValidator(_instant), Field(gt=0, le=_MAX_INT64)]
type HttpsLink = Annotated[str | None, AfterValidator(_https)]


class KwalWire(BaseModel):
    """Base for every participant payload: camelCase aliases, unknown fields tolerated."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    def to_wire(self) -> dict[str, Any]:
        """Serialise as the participant contract states it.

        Returns:
            A JSON-ready dict with camelCase keys and without the fields that are None.
        """
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class KwalAmount(KwalWire):
    """``{"minorUnits": "<digits>", "currency": ..., "decimals": n}`` (``amounts.py``).

    Attributes:
        minor_units: Whole minor units, as text so any precision stays exact.
        currency: The currency or token symbol; None when the service sent none.
        decimals: How many of the digits are after the point.
    """

    minor_units: MinorUnits = "0"
    currency: str | None = None
    decimals: int = Field(default=0, ge=0, le=36)

    @property
    def value(self) -> Decimal:
        """Return the amount in whole units, exactly.

        Returns:
            The minor units shifted by the decimals.
        """
        return Decimal(int(self.minor_units)).scaleb(-self.decimals)

    @classmethod
    def of(cls, amount: Decimal, currency: str, decimals: int) -> Self:
        """State an amount as the gateway does.

        Args:
            amount: Whole units.
            currency: The currency or token symbol.
            decimals: The precision to state it at.

        Returns:
            The amount in minor units.

        Raises:
            ValueError: If the amount is negative or not exact at that precision.
        """
        units = amount.scaleb(decimals)
        if units < 0 or units != units.to_integral_value():
            raise ValueError(f"{amount} is not a whole number of minor units at {decimals}")
        return cls(minor_units=str(int(units)), currency=currency, decimals=decimals)


# Products: GET /products?query=&limit=, GET /products/{id}, POST /products/{id}/variant


class KwalProduct(KwalWire):
    """One product (``catalog.py:87-95``); merchant and price may be unreported."""

    product_id: str
    title: str
    merchant: str | None = None
    price: KwalAmount | None = None


class KwalProducts(KwalWire):
    """A search's answer; an absent list is an empty result (``catalog.py:98-107``)."""

    products: list[KwalProduct] = []


class KwalOptionValue(KwalWire):
    """One selectable value of an option, by its published id."""

    option_id: str
    label: str


class KwalOption(KwalWire):
    """An option that decides the variant; one without values is refused."""

    name: str
    values: list[KwalOptionValue] = Field(min_length=1)


class KwalProductDetail(KwalWire):
    """One product read with its options (``catalog.py:110-137``)."""

    product: KwalProduct
    options: list[KwalOption] = []


class VariantRequest(KwalWire):
    """``POST /products/{id}/variant``: one option id for every option, or none."""

    option_ids: list[str]


class KwalVariant(KwalWire):
    """A resolved variant (``catalog.py:140-155``); only a purchasable one is quoted."""

    variant_id: str
    title: str | None = None
    price: KwalAmount | None = None
    purchasable: bool = False

    @model_validator(mode="after")
    def _priced_when_purchasable(self) -> Self:
        if self.purchasable and (self.price is None or not int(self.price.minor_units)):
            raise ValueError("a purchasable variant must carry its price")
        return self


# Quotes: POST /quotes, GET /quotes/{id}, POST /quotes/{id}/shipping


class QuoteLine(KwalWire):
    """One line of a quote."""

    variant_id: str
    quantity: int = Field(gt=0)


class KwalShippingAddress(KwalWire):
    """The delivery address a quote prices shipping from (``quotes.py:43-53``)."""

    first_name: str
    last_name: str
    phone: str
    line1: str
    line2: str | None = None
    city: str
    region: str | None = None
    postal_code: str | None = None
    country: str


class QuoteRequest(KwalWire):
    """``POST /quotes`` (``quotes.py:183-208``)."""

    email: str
    lines: list[QuoteLine] = Field(min_length=1)
    shipping_address: KwalShippingAddress | None = None


class ShippingRequest(KwalWire):
    """``POST /quotes/{id}/shipping``."""

    shipping_option_id: str


class KwalShippingOption(KwalWire):
    """One offered shipping option; an absent price is zero."""

    shipping_option_id: str
    label: str
    price: KwalAmount | None = None


class KwalQuote(KwalWire):
    """A priced quote (``quotes.py:88-180``).

    ``payment_id`` names a payment that already holds this quote.
    """

    quote_id: str
    lines: list[QuoteLine] = Field(min_length=1)
    shipping_options: list[KwalShippingOption] = []
    selected_shipping_option_id: str | None = None
    subtotal: KwalAmount | None = None
    shipping: KwalAmount | None = None
    tax: KwalAmount | None = None
    total: KwalAmount
    expires_at_unix_seconds: Instant
    payment_id: str | None = None

    @model_validator(mode="after")
    def _reviewable(self) -> Self:
        if not int(self.total.minor_units) or self.total.currency is None:
            raise ValueError("a quote must carry a total")
        offered = {o.shipping_option_id for o in self.shipping_options}
        if self.selected_shipping_option_id not in (None, *offered):
            raise ValueError("a quote cannot select a shipping option it does not offer")
        return self


# Payments: POST /payments, GET /payments/{id}


class PaymentRequest(KwalWire):
    """``POST /payments``: the caller's payment id, minted before sending, and the quote."""

    payment_id: str
    quote_id: str


class PaymentState(StrEnum):
    """A payment's state (``checkout.py:37-39``)."""

    REQUIRES_ACTION = "PARTICIPANT_PAYMENT_STATE_REQUIRES_ACTION"
    PENDING = "PARTICIPANT_PAYMENT_STATE_PENDING"
    COMPLETED = "PARTICIPANT_PAYMENT_STATE_COMPLETED"
    DECLINED = "PARTICIPANT_PAYMENT_STATE_DECLINED"
    ERROR = "PARTICIPANT_PAYMENT_STATE_ERROR"


SIMULATED_SPEND_STEPS = frozenset({"card_authorization", "card_clearing"})
"""The steps of a sandbox payment made by a simulated card spend (``checkout.py:42``)."""

QUOTE_ALREADY_USED = "quote_already_used"
"""The step of a payment refused because another payment holds its quote (``checkout.py:46``)."""


class KwalPayment(KwalWire):
    """A payment as the service reports it (``checkout.py:84-152``)."""

    payment_id: str
    state: PaymentState
    step: str | None = None
    approval_url: HttpsLink = None
    approval_expires_at_unix_seconds: Instant | None = None
    checkout_id: str | None = None
    order_id: str | None = None
    charged: KwalAmount | None = None
    reason: str | None = None
    amount: KwalAmount | None = None
    checkout_status: str | None = None
    quote_id: str | None = None
    card_transaction_id: str | None = None
    held: KwalAmount | None = None

    @model_validator(mode="after")
    def _linked_when_waiting(self) -> Self:
        if self.state == PaymentState.REQUIRES_ACTION and self.approval_url is None:
            raise ValueError("a payment that requires action must carry its approval link")
        return self


# Setup and funding: GET /status, GET /funding


class SetupState(StrEnum):
    """The vault and card setup's state (``vault.py:64``)."""

    NOT_STARTED = "PARTICIPANT_SETUP_STATE_NOT_STARTED"
    PENDING = "PARTICIPANT_SETUP_STATE_PENDING"
    READY = "PARTICIPANT_SETUP_STATE_READY"
    NEEDS_OPERATOR = "PARTICIPANT_SETUP_STATE_NEEDS_OPERATOR"


_ENROLMENT_PREFIX = "PARTICIPANT_ENROLLMENT_STATUS_"
_ENROLMENT_STATUSES = frozenset({"REQUIRES_ACTION", "ACTIVE", "FAILED", "EXPIRED", "REVOKED"})


def _enrolment_status(value: object) -> object:
    """Read the card enrolment's status by its short name, as ``vault.py:104-110`` does.

    Args:
        value: The full enum name, or its unspecified value.

    Returns:
        The short name, None when unspecified, else the value for pydantic to refuse.
    """
    if value is None or value == f"{_ENROLMENT_PREFIX}UNSPECIFIED":
        return None
    if isinstance(value, str) and value.startswith(_ENROLMENT_PREFIX):
        short = value.removeprefix(_ENROLMENT_PREFIX)
        if short in _ENROLMENT_STATUSES:
            return short
    raise ValueError("unknown enrolment status")


def _enrolment_wire(value: str) -> str:
    """State the card enrolment's status by its full wire name.

    Args:
        value: The short name.

    Returns:
        ``PARTICIPANT_ENROLLMENT_STATUS_<name>``.
    """
    return f"{_ENROLMENT_PREFIX}{value}"


class KwalSetup(KwalWire):
    """The participant's vault and card setup (``vault.py:81-153``).

    The addresses are kept for the operator's own command line (the skill prints them);
    the control plane never records or shows them.
    """

    state: SetupState
    step: str | None = None
    vault_address: str | None = None
    chain: str | None = None
    owner_address: str | None = None
    card_status: str | None = None
    enrollment_id: str | None = None
    enrollment_status: Annotated[
        str | None,
        BeforeValidator(_enrolment_status),
        PlainSerializer(_enrolment_wire, when_used="unless-none"),
    ] = None
    deployment_tx_id: str | None = None
    deposit_observed: bool = False

    @model_validator(mode="after")
    def _evidenced(self) -> Self:
        if (self.enrollment_id is None) != (self.enrollment_status is None):
            raise ValueError("an enrolment needs both its id and its status")
        if self.state == SetupState.READY and (
            self.card_status != "ACTIVE"
            or not self.deposit_observed
            or self.enrollment_status not in (None, "ACTIVE")
            or self.vault_address is None
        ):
            raise ValueError("ready needs a vault, an active card and an observed deposit")
        return self


class FundingState(StrEnum):
    """Funding readiness (``vault.py:264``)."""

    SETUP_NEEDED = "PARTICIPANT_FUNDING_STATE_SETUP_NEEDED"
    FUNDS_NEEDED = "PARTICIPANT_FUNDING_STATE_FUNDS_NEEDED"
    PROCESSING = "PARTICIPANT_FUNDING_STATE_PROCESSING"
    READY = "PARTICIPANT_FUNDING_STATE_READY"
    ERROR = "PARTICIPANT_FUNDING_STATE_ERROR"


class KwalFunding(KwalWire):
    """Funding readiness: what the card can spend from the vault (``vault.py:383-448``)."""

    state: FundingState
    vault_address: str | None = None
    chain: str | None = None
    available: KwalAmount | None = None
    shortfall: KwalAmount | None = None
    required: KwalAmount | None = None

    @model_validator(mode="after")
    def _usdc(self) -> Self:
        for value in (self.available, self.shortfall, self.required):
            if value is not None and (value.currency, value.decimals) != (USDC, USDC_DECIMALS):
                raise ValueError("funding amounts must be USDC with 6 decimals")
        return self


class KwalErrorBody(KwalWire):
    """An error body; only its stable type tag is read (``transport.py:39-52``)."""

    type: str | None = None
    data: dict[str, Any] | None = None

    @property
    def tag(self) -> str | None:
        """Return the participant error tag, if the type is one.

        Returns:
            The tag after ``tag:kraken.com,2025:``, or None.
        """
        if self.type is None or not self.type.startswith(ERROR_TYPE_PREFIX):
            return None
        return self.type.removeprefix(ERROR_TYPE_PREFIX) or None

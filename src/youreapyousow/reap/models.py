"""Reap wire models, transcribed from Reap's published OpenAPI.

Sources: ``https://docs.reap.global/api-reference/openapi.json`` (API version
``2025-02-14``) and ``https://docs.reap.global/webhooks/openapi.json``.

Field names are snake_case in Python and camelCase on the wire. Fields the spec marks
required are required here even when nullable, so a response built by the mock carries
exactly the keys Reap's does; optional fields default to None and are left out of the
JSON when None. Money is ``Decimal`` in Python and a JSON number on the wire.
Timestamps stay as the strings Reap sends.

The agentic models (section "Agentic payments" below) are transcribed from the same
OpenAPI file's sixteen ``/agentic/*`` operations, the guides under
``https://docs.reap.global/agentic-payments/`` and the reference pages
``https://docs.reap.global/api-reference/{idempotency,errors,rate-limiting}.md``.
Mandates are left out: "the endpoints are not live in sandbox or production". Where
Reap's docs disagree with each other or are silent, these models assume, by name:

- Required, but absent in Reap's own example: the OpenAPI marks these response fields
  required, yet the guide's examples leave them out, so they parse when absent (None) and
  stay absent when serialised: a product's ``merchant`` and ``media`` and a default
  variant's ``options`` and ``media`` (product details example), a variant's ``media``
  (resolve variant example), and ``discounts`` and ``additionalCharges`` in an amount
  breakdown (select shipping option example).
- A checkout create response's ``nextAction`` is nullable, although the OpenAPI marks it
  non-null: the guide says "When it is empty, the charge already ran under terms the user
  approved earlier", and under ``X-Simulate-Checkout`` the changelog returns it ``COMPLETED``.
- An error body's ``detail`` may be absent: the rate-limiting page's 429 example has none.
- Constraints are enforced on requests, which this side sends, exactly as the OpenAPI
  gives them (UUID patterns, the phone and email patterns, lengths and counts, HTTPS return
  URLs from the field descriptions). Responses are parsed for shape only: Reap's own
  examples carry placeholders such as ``<enrollment-id>`` where the schema's pattern asks
  for a UUID, and a client that refused Reap's answer over a format would turn a
  completed purchase into an unknown one.
- The search price filter's ``min`` and ``max`` are strings on the wire (the OpenAPI) and
  hold decimal amounts in Python, as the guide's ``"100"`` and ``"200"`` do.
"""

import re
import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    PlainSerializer,
    Tag,
)
from pydantic.alias_generators import to_camel

REAP_VERSION = "2025-02-14"

type WireMoney = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]


def _absent_optionals(model: BaseModel) -> dict[Any, Any]:
    """Build a nested ``exclude`` spec naming every optional field that is None.

    Args:
        model: The model about to be serialised.

    Returns:
        A pydantic ``exclude`` mapping; required fields are never excluded.
    """
    exclude: dict[Any, Any] = {}
    keep_set_nulls = getattr(type(model), "keeps_explicit_nulls", False)
    for name, info in type(model).model_fields.items():
        value = getattr(model, name)
        if value is None and not info.is_required():
            if not (keep_set_nulls and name in model.model_fields_set):
                exclude[name] = True
        elif isinstance(value, BaseModel):
            if nested := _absent_optionals(value):
                exclude[name] = nested
        elif isinstance(value, list):
            items: dict[Any, Any] = {}
            for index, item in enumerate(cast(list[object], value)):
                if isinstance(item, BaseModel) and (nested := _absent_optionals(item)):
                    items[index] = nested
            if items:
                exclude[name] = items
    return exclude


class Wire(BaseModel):
    """Base for every Reap payload: camelCase aliases, unknown fields tolerated."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    keeps_explicit_nulls: ClassVar[bool] = False
    """When True, an optional field set to None explicitly is sent as null, not left out."""

    def to_wire(self) -> dict[str, Any]:
        """Serialise exactly as Reap would send or expect it.

        Returns:
            A JSON-ready dict with camelCase keys; optional fields that are None are
            omitted, required ones are always present.
        """
        return self.model_dump(mode="json", by_alias=True, exclude=_absent_optionals(self))


class ErrorDetail(Wire):
    """The body of a Reap error.

    ``detail`` is ``null`` in the errors page's example
    (https://docs.reap.global/api-reference/errors.md) and absent from the rate-limiting
    page's 429 example (https://docs.reap.global/api-reference/rate-limiting.md); both
    parse, and each serialises back as it came.
    """

    keeps_explicit_nulls = True

    code: str
    message: str
    detail: dict[str, Any] | None = None


class ErrorResponse(Wire):
    """``{"error": {"code", "message", "detail"}}``, returned with 4xx and 5xx statuses."""

    error: ErrorDetail


class Page[T](Wire):
    """A cursor-paginated list."""

    items: list[T]
    next_cursor: str | None


# Users and KYC


class ApplicationStatus(StrEnum):
    """KYC application status of a user."""

    NOT_STARTED = "NOT_STARTED"
    AWAITING_DOCUMENTS = "AWAITING_DOCUMENTS"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETRY_REQUIRED = "RETRY_REQUIRED"


class UserApplication(Wire):
    """A user's KYC application."""

    status: ApplicationStatus
    rejection_reason: str | None
    documents: list[dict[str, Any]] | None


class CreateUserRequest(Wire):
    """``POST /users/``."""

    email: str
    phone_number: str
    external_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class User(Wire):
    """A cardholder."""

    id: str
    external_id: str | None
    email: str
    phone_number: str
    first_name: str | None
    last_name: str | None
    country: str | None
    date_of_birth: str | None
    company_id: str | None
    application: UserApplication
    created_at: str
    updated_at: str


class SimulateApplicationRequest(Wire):
    """``POST /simulation/users/{userId}/application`` (sandbox only)."""

    status: Literal["APPROVED", "REJECTED", "RETRY_REQUIRED", "IN_REVIEW"]


# Accounts and funding


class CreateAccountRequest(Wire):
    """``POST /accounts/``; requires an ``Idempotency-Key``."""

    owner_id: str
    owner_type: Literal["USER", "COMPANY"] | None = None


class ChainAddress(Wire):
    """A deposit address on one chain."""

    chain_id: str
    address: str


class Account(Wire):
    """An account holding a spending balance."""

    id: str
    status: Literal["ACTIVE", "RESTRICTED"]
    owner_type: Literal["USER", "COMPANY", "PROJECT"]
    owner_id: str | None
    chain_addresses: list[ChainAddress]
    created_at: str
    updated_at: str


class BalanceAssets(Wire):
    """Asset breakdown of a balance."""

    crypto: WireMoney | None
    fiat: WireMoney | None
    virtual: WireMoney | None


class CardDebt(Wire):
    """Card debt breakdown."""

    pending: WireMoney
    cleared: WireMoney
    total: WireMoney


class BalanceLiabilities(Wire):
    """Liability breakdown of a balance."""

    card_debt: CardDebt
    deposit_fees: WireMoney | None


class Balance(Wire):
    """``GET /accounts/{id}/balance``."""

    currency: str
    total_asset_value: WireMoney
    total_liabilities: WireMoney
    available_balance: WireMoney
    assets: BalanceAssets
    liabilities: BalanceLiabilities


class SimulateFiatDepositRequest(Wire):
    """``POST /simulation/fiat-deposits`` (sandbox, Program-Funded projects only)."""

    amount: WireMoney
    currency: str
    sender_name: str | None = None
    reference: str | None = None


class FiatDeposit(Wire):
    """A settled fiat deposit."""

    id: str
    account_id: str
    transaction_id: str
    status: Literal["SETTLED"]
    amount: WireMoney
    currency: str
    original_amount: WireMoney
    original_currency: str
    sender_name: str | None
    reference: str | None
    occurred_at: str
    created_at: str
    updated_at: str


# Cards


class CardStatus(StrEnum):
    """Card status."""

    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"


class CreateCardRequest(Wire):
    """``POST /cards/``; requires an ``Idempotency-Key``."""

    user_id: str
    account_id: str
    type: Literal["VIRTUAL", "PHYSICAL"]
    three_ds_challenge_method: Literal["SMS", "WEBHOOK"] | None = Field(
        default=None,
        validation_alias="3dsChallengeMethod",
        serialization_alias="3dsChallengeMethod",
    )
    card_design_id: str | None = None


class CardBlockReason(Wire):
    """Why a card is blocked."""

    type: str
    message: str


class Card(Wire):
    """A card. Carries no limit, expiry or metadata field: those live in policies."""

    id: str
    account_id: str
    user_id: str
    type: Literal["VIRTUAL", "PHYSICAL"]
    card_design_id: str
    status: CardStatus
    block_reason: CardBlockReason | None
    block_liftable: bool
    frozen: bool
    cardholder_name: str
    last4: str
    three_ds_challenge_method: Literal["SMS", "WEBHOOK"] = Field(
        validation_alias="3dsChallengeMethod", serialization_alias="3dsChallengeMethod"
    )
    physical_card_status: str | None
    created_at: str
    updated_at: str


# Spend policies


class PolicyType(StrEnum):
    """The five policy types Reap enforces at authorisation."""

    MERCHANT_RESTRICTION = "MERCHANT_RESTRICTION"
    CHANNEL_RESTRICTION = "CHANNEL_RESTRICTION"
    TRANSACTION_AMOUNT_LIMIT = "TRANSACTION_AMOUNT_LIMIT"
    SPEND_LIMIT = "SPEND_LIMIT"
    AUTHORIZATION_COUNT_LIMIT = "AUTHORIZATION_COUNT_LIMIT"


class ScopeType(StrEnum):
    """Where a policy attaches."""

    PROJECT = "PROJECT"
    USER = "USER"
    CARD = "CARD"


type Channel = Literal["ATM", "POS", "ECOMMERCE", "VISA_DIRECT"]
type Period = Literal["DAILY", "WEEKLY", "MONTHLY", "YEARLY", "LIFETIME"]


class PolicyScope(Wire):
    """``{"type": "PROJECT"}`` or ``{"type": "USER"|"CARD", "id": ...}``."""

    type: ScopeType
    id: str | None = None


class MccMatch(Wire):
    """Match merchants by category code."""

    dimension: Literal["MCC"]
    codes: list[str]


class CountryMatch(Wire):
    """Match merchants by country."""

    dimension: Literal["MERCHANT_COUNTRY"]
    countries: list[str]


type MerchantMatch = Annotated[MccMatch | CountryMatch, Field(discriminator="dimension")]


class CalendarWindow(Wire):
    """A calendar window; resets at 00:00 UTC boundaries, never rolling."""

    type: Literal["CALENDAR"] = "CALENDAR"
    period: Period


class LimitFilter(Wire):
    """Restricts which transactions count towards a running-total limit."""

    channels: list[Channel] | None = None
    merchant: MerchantMatch | None = None


class MerchantRestrictionConfig(Wire):
    """Blocks transactions whose merchant matches."""

    match: MerchantMatch


class ChannelRestrictionConfig(Wire):
    """Blocks transactions on the listed channels."""

    channels: list[Channel]


class TransactionAmountLimitConfig(Wire):
    """Rejects any single transaction above ``max_amount``; no window."""

    max_amount: WireMoney


class SpendLimitConfig(Wire):
    """Caps total spend within a calendar window."""

    window: CalendarWindow
    max_amount: WireMoney
    filter: LimitFilter | None = None


class AuthorizationCountLimitConfig(Wire):
    """Caps the number of authorisations within a calendar window."""

    window: CalendarWindow
    max_count: int
    filter: LimitFilter | None = None


class _PolicyCreateBase(Wire):
    scope: PolicyScope
    name: str


class MerchantRestrictionCreate(_PolicyCreateBase):
    """Create a merchant restriction."""

    type: Literal["MERCHANT_RESTRICTION"] = "MERCHANT_RESTRICTION"
    config: MerchantRestrictionConfig


class ChannelRestrictionCreate(_PolicyCreateBase):
    """Create a channel restriction."""

    type: Literal["CHANNEL_RESTRICTION"] = "CHANNEL_RESTRICTION"
    config: ChannelRestrictionConfig


class TransactionAmountLimitCreate(_PolicyCreateBase):
    """Create a per-transaction cap."""

    type: Literal["TRANSACTION_AMOUNT_LIMIT"] = "TRANSACTION_AMOUNT_LIMIT"
    config: TransactionAmountLimitConfig


class SpendLimitCreate(_PolicyCreateBase):
    """Create a spend limit."""

    type: Literal["SPEND_LIMIT"] = "SPEND_LIMIT"
    config: SpendLimitConfig


class AuthorizationCountLimitCreate(_PolicyCreateBase):
    """Create an authorisation count limit."""

    type: Literal["AUTHORIZATION_COUNT_LIMIT"] = "AUTHORIZATION_COUNT_LIMIT"
    config: AuthorizationCountLimitConfig


type PolicyCreate = Annotated[
    MerchantRestrictionCreate
    | ChannelRestrictionCreate
    | TransactionAmountLimitCreate
    | SpendLimitCreate
    | AuthorizationCountLimitCreate,
    Field(discriminator="type"),
]


class _PolicyStoredBase(Wire):
    id: str
    scope: PolicyScope
    status: Literal["ACTIVE", "DISABLED"]
    name: str
    created_at: str
    updated_at: str


class MerchantRestrictionPolicy(_PolicyStoredBase):
    """A stored merchant restriction."""

    type: Literal["MERCHANT_RESTRICTION"]
    config: MerchantRestrictionConfig


class ChannelRestrictionPolicy(_PolicyStoredBase):
    """A stored channel restriction."""

    type: Literal["CHANNEL_RESTRICTION"]
    config: ChannelRestrictionConfig


class TransactionAmountLimitPolicy(_PolicyStoredBase):
    """A stored per-transaction cap."""

    type: Literal["TRANSACTION_AMOUNT_LIMIT"]
    currency: str
    config: TransactionAmountLimitConfig


class SpendLimitPolicy(_PolicyStoredBase):
    """A stored spend limit."""

    type: Literal["SPEND_LIMIT"]
    currency: str
    config: SpendLimitConfig


class AuthorizationCountLimitPolicy(_PolicyStoredBase):
    """A stored authorisation count limit."""

    type: Literal["AUTHORIZATION_COUNT_LIMIT"]
    config: AuthorizationCountLimitConfig


type Policy = Annotated[
    MerchantRestrictionPolicy
    | ChannelRestrictionPolicy
    | TransactionAmountLimitPolicy
    | SpendLimitPolicy
    | AuthorizationCountLimitPolicy,
    Field(discriminator="type"),
]


class PolicyLimitUsage(Wire):
    """Usage of a running-total limit: Reap's native "budget remaining" read."""

    limit: WireMoney
    used: WireMoney
    remaining: WireMoney
    resets_at: str | None


class EffectivePolicy(Wire):
    """A policy in force for a scope, with usage when it tracks a running total."""

    policy: Policy
    usage: PolicyLimitUsage | None


class EffectivePolicies(Wire):
    """``GET /policies/effective``."""

    items: list[EffectivePolicy]


# Card transactions


class Fees(Wire):
    """Fees on a transaction."""

    atm: WireMoney
    fx: WireMoney


class SimulatedMerchant(Wire):
    """Merchant on a simulated authorisation; omitted fields are randomised by Reap."""

    name: str | None = None
    city: str | None = None
    post_code: str | None = None
    state: str | None = None
    country: str | None = None
    mcc_code: str | None = None
    mcc_category: str | None = None


class TransactionMerchant(Wire):
    """Merchant on a card transaction."""

    id: str
    card_acceptor_id: str | None
    name: str
    city: str | None
    post_code: str | None
    state: str | None
    country: str | None = None
    mcc_code: str
    mcc_category: str


class TransactionEvent(Wire):
    """One network event in a transaction's life."""

    id: str
    type: Literal["AUTHORIZATION", "CLEARING", "REVERSAL", "REFUND", "DECLINE"]
    original_amount: WireMoney
    original_currency: str
    amount: WireMoney
    fees: Fees
    occurred_at: str
    created_at: str


class AuthorisedAmounts(Wire):
    """Amounts on a pending or void transaction."""

    authorized: WireMoney
    reversed: WireMoney
    current: WireMoney


class ClearedAmounts(Wire):
    """Amounts on a cleared transaction."""

    cleared: WireMoney
    refunded: WireMoney
    current: WireMoney


class DeclineCode(StrEnum):
    """Decline codes, including the policy ones that name the rule that fired."""

    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    INSUFFICIENT_MASTER_BALANCE = "INSUFFICIENT_MASTER_BALANCE"
    CARD_NOT_ACTIVE = "CARD_NOT_ACTIVE"
    CARD_FROZEN = "CARD_FROZEN"
    CARD_BLOCKED = "CARD_BLOCKED"
    MERCHANT_CATEGORY_CODE_NOT_ALLOWED = "MERCHANT_CATEGORY_CODE_NOT_ALLOWED"
    MERCHANT_NOT_ALLOWED = "MERCHANT_NOT_ALLOWED"
    CHANNEL_NOT_ALLOWED = "CHANNEL_NOT_ALLOWED"
    TRANSACTION_AMOUNT_LIMIT_EXCEEDED = "TRANSACTION_AMOUNT_LIMIT_EXCEEDED"
    SPEND_LIMIT_EXCEEDED = "SPEND_LIMIT_EXCEEDED"
    AUTHORIZATION_COUNT_LIMIT_EXCEEDED = "AUTHORIZATION_COUNT_LIMIT_EXCEEDED"
    TRANSACTION_NOT_ALLOWED = "TRANSACTION_NOT_ALLOWED"
    SUSPECTED_FRAUD = "SUSPECTED_FRAUD"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DeclineReason(Wire):
    """Why a transaction was declined. Reap's enum is longer; unknown codes parse."""

    code: str
    message: str


class DeclinedPolicy(Wire):
    """The policy that declined a transaction."""

    id: str
    type: PolicyType
    scope_type: ScopeType
    name: str


class _TransactionBase(Wire):
    id: str
    account_id: str
    card_id: str
    channel: Channel
    digital_wallet: str | None
    original_currency: str
    currency: str
    merchant: TransactionMerchant
    fees: Fees
    events: list[TransactionEvent]
    occurred_at: str
    created_at: str
    updated_at: str


class PendingCardTransaction(_TransactionBase):
    """An authorised, not yet cleared, transaction."""

    status: Literal["PENDING"]
    original_amount: AuthorisedAmounts
    amount: AuthorisedAmounts
    conversion_rate: WireMoney


class ClearedCardTransaction(_TransactionBase):
    """A settled transaction."""

    status: Literal["CLEARED"]
    original_amount: ClearedAmounts
    amount: ClearedAmounts
    conversion_rate: WireMoney


class DeclinedCardTransaction(_TransactionBase):
    """A declined transaction, naming the policy responsible when one was."""

    status: Literal["DECLINED"]
    original_amount: WireMoney
    decline_reason: DeclineReason
    policy: DeclinedPolicy | None


class VoidCardTransaction(_TransactionBase):
    """A fully reversed authorisation."""

    status: Literal["VOID"]
    original_amount: AuthorisedAmounts
    amount: AuthorisedAmounts


type CardTransaction = Annotated[
    PendingCardTransaction | ClearedCardTransaction | DeclinedCardTransaction | VoidCardTransaction,
    Field(discriminator="status"),
]


class SimulateAuthorizationRequest(Wire):
    """``POST /simulation/card-transactions/authorization`` (sandbox only)."""

    card_id: str
    amount: WireMoney
    transaction_id: str | None = None
    original_amount: WireMoney | None = None
    original_currency: str | None = None
    channel: Channel | None = None
    merchant: SimulatedMerchant | None = None
    trigger_fraud_alert: bool | None = None


class SimulateClearingRequest(Wire):
    """``POST /simulation/card-transactions/clearing`` (sandbox only)."""

    card_id: str
    amount: WireMoney
    transaction_id: str | None = None
    original_amount: WireMoney | None = None
    original_currency: str | None = None


class Activity(Wire):
    """One entry of ``GET /activities/``; ``data`` depends on ``type``."""

    type: str
    id: str
    occurred_at: str
    data: dict[str, Any]


# Webhooks


class CreateWebhookRequest(Wire):
    """``POST /webhooks/``."""

    name: str
    url: str
    mode: Literal["NOTIFICATION", "REQUEST"] | None = None


class WebhookEndpoint(Wire):
    """A registered endpoint; ``signing_secret`` is returned exactly once."""

    id: str
    name: str
    url: str
    status: Literal["ACTIVE", "DISABLED"]
    mode: Literal["NOTIFICATION", "REQUEST"]
    last_used_at: str | None
    created_at: str
    updated_at: str
    signing_secret: str


class WebhookEventType(StrEnum):
    """Webhook event types the control plane consumes."""

    CARD_TRANSACTION_CREATED = "CARD_TRANSACTION_CREATED"
    CARD_TRANSACTION_UPDATED = "CARD_TRANSACTION_UPDATED"
    CARD_STATUS_UPDATED = "CARD_STATUS_UPDATED"
    FIAT_DEPOSIT_CREATED = "FIAT_DEPOSIT_CREATED"
    USER_APPLICATION_STATUS_UPDATED = "USER_APPLICATION_STATUS_UPDATED"
    CARD_AUTHORIZATION_REQUEST = "CARD_AUTHORIZATION_REQUEST"


class WebhookEvent(Wire):
    """The envelope of every webhook delivery: ``{id, type, data}``."""

    id: str
    type: str
    data: dict[str, Any]


class AuthorizationMerchant(Wire):
    """Merchant on an external authorisation request."""

    card_acceptor_id: str | None
    name: str
    city: str | None
    post_code: str | None
    state: str | None
    country: str | None = None
    mcc_code: str
    mcc_category: str


class CardAuthorizationRequest(Wire):
    """``data`` of ``CARD_AUTHORIZATION_REQUEST``; must be answered within 1.6 s."""

    transaction_id: str
    event_id: str
    account_id: str
    card_id: str
    channel: Channel
    digital_wallet: str | None
    currency: str
    amount: WireMoney
    original_currency: str
    original_amount: WireMoney
    fees: Fees
    merchant: AuthorizationMerchant
    occurred_at: str


class ExternalAuthDecision(Wire):
    """The reply to an authorisation request; Reap fails closed on anything else."""

    decision: Literal["APPROVE", "DECLINE"]
    reason: Literal["INSUFFICIENT_BALANCE", "TRANSACTION_NOT_ALLOWED"] | None = None


# Agentic payments
#
# Shapes from https://docs.reap.global/api-reference/openapi.json (the /agentic/* paths);
# constraints are the OpenAPI's own. The assumptions where the docs
# are silent or disagree are named in the module docstring.

# The UUID pattern the OpenAPI sets on ``quoteId``, ``enrollmentId`` and enrollment ids.
_UUID_PATTERN = re.compile(
    r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
    r"|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$"
)
# The OpenAPI's email pattern; it uses look-aheads, so it is checked with ``re``.
_EMAIL_PATTERN = re.compile(
    r"^(?!\.)(?!.*\.\.)([A-Za-z0-9_'+\-\.]*)[A-Za-z0-9_+-]@([A-Za-z0-9][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}$"
)
# ``shippingAddress.phone`` in the OpenAPI.
PHONE_PATTERN = r"^\+[1-9]\d{6,14}$"
# ``offerCode`` in the OpenAPI: at least one non-space character.
_OFFER_CODE_PATTERN = r"\S"


def _uuid_pattern(value: str) -> str:
    """Check a value against the OpenAPI's UUID pattern.

    Args:
        value: The candidate id.

    Returns:
        The value, unchanged.

    Raises:
        ValueError: If it does not match.
    """
    if not _UUID_PATTERN.match(value):
        raise ValueError("must be a UUID")
    return value


def _uuid_format(value: str) -> str:
    """Check a value is a UUID in any version (``format: uuid``).

    Args:
        value: The candidate id.

    Returns:
        The value, unchanged.

    Raises:
        ValueError: If it is not a UUID.
    """
    uuid.UUID(value)
    return value


def _email(value: str) -> str:
    """Check a value against the OpenAPI's email pattern.

    Args:
        value: The candidate address.

    Returns:
        The value, unchanged.

    Raises:
        ValueError: If it does not match.
    """
    if not _EMAIL_PATTERN.match(value):
        raise ValueError("must be an email address")
    return value


def _https(value: str) -> str:
    """Check a URL is HTTPS, as the OpenAPI describes every ``returnUrl``.

    Args:
        value: The candidate URL.

    Returns:
        The value, unchanged.

    Raises:
        ValueError: If it is not an https URL with a host.
    """
    scheme, _, rest = value.partition("://")
    if scheme.lower() != "https" or not rest:
        raise ValueError("must be an https URL")
    return value


type UuidId = Annotated[str, AfterValidator(_uuid_pattern)]
type UuidFormat = Annotated[str, AfterValidator(_uuid_format)]
type Email = Annotated[str, AfterValidator(_email)]
type HttpsUrl = Annotated[str, AfterValidator(_https)]
type NonEmpty = Annotated[str, Field(min_length=1)]
type DecimalString = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]


class AgenticErrorCode(StrEnum):
    """Every ``error.code`` the agentic operations document, to branch on (never the status).

    From the OpenAPI's per-operation error lists, the errors page
    (https://docs.reap.global/api-reference/errors.md), the rate-limiting page and the
    changelog. Reap may send others; ``ReapError.code`` stays a string so an unknown
    code still parses.
    """

    AGENTIC_REQUEST_REJECTED = "AGENTIC_REQUEST_REJECTED"
    AGENTIC_PAYMENTS_NOT_ENABLED = "AGENTIC_PAYMENTS_NOT_ENABLED"
    AGENTIC_RESOURCE_NOT_FOUND = "AGENTIC_RESOURCE_NOT_FOUND"
    AGENTIC_SERVICE_UNAVAILABLE = "AGENTIC_SERVICE_UNAVAILABLE"
    AGENTIC_CARD_NOT_FOUND = "AGENTIC_CARD_NOT_FOUND"
    ENROLLMENT_NOT_FOUND = "ENROLLMENT_NOT_FOUND"
    ENROLLMENT_NOT_ACTIVE = "ENROLLMENT_NOT_ACTIVE"
    MERCHANT_NOT_RESOLVED = "MERCHANT_NOT_RESOLVED"
    VARIANT_RESOLUTION_FAILED = "VARIANT_RESOLUTION_FAILED"
    CARD_PAYMENT_UNAVAILABLE = "CARD_PAYMENT_UNAVAILABLE"
    CHECKOUT_URL_INVALID = "CHECKOUT_URL_INVALID"
    OFFER_CODE_INVALID = "OFFER_CODE_INVALID"
    OFFER_CODE_EXPIRED = "OFFER_CODE_EXPIRED"
    QUOTE_UNFULFILLABLE = "QUOTE_UNFULFILLABLE"
    INVALID_PHONE = "INVALID_PHONE"
    ITEMS_UNSHIPPABLE = "ITEMS_UNSHIPPABLE"
    STATE_OR_PROVINCE_REQUIRED = "STATE_OR_PROVINCE_REQUIRED"
    SHIPPING_OPTION_INVALID = "SHIPPING_OPTION_INVALID"
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    QUOTE_NOT_MUTABLE = "QUOTE_NOT_MUTABLE"
    QUOTE_REPLACEMENT_REQUIRED = "QUOTE_REPLACEMENT_REQUIRED"
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"
    QUOTE_TEMPORARILY_UNAVAILABLE = "QUOTE_TEMPORARILY_UNAVAILABLE"
    CHECKOUT_NOT_FOUND = "CHECKOUT_NOT_FOUND"
    CHECKOUT_TEMPORARILY_UNAVAILABLE = "CHECKOUT_TEMPORARILY_UNAVAILABLE"
    IDEMPOTENT_PARAMETER_MISMATCH = "IDEMPOTENT_PARAMETER_MISMATCH"
    IDEMPOTENCY_REQUEST_IN_PROGRESS = "IDEMPOTENCY_REQUEST_IN_PROGRESS"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"


class Money(Wire):
    """``{"amount": number, "currency": string}``, the agentic amount everywhere."""

    amount: WireMoney
    currency: str = Field(min_length=3, max_length=3)


class NextAction(Wire):
    """Where to send the user: a Reap-hosted card-entry or approval page."""

    type: Literal["REDIRECT"]
    url: str
    expires_at: str | None = None


class Presentation(Wire):
    """How the hosted page returns the user: a redirect to an HTTPS ``returnUrl``."""

    type: Literal["REDIRECT"] = "REDIRECT"
    return_url: HttpsUrl


# Enrollments: POST /agentic/enrollments, GET /agentic/enrollments[/{id}],
# POST /agentic/enrollments/{id}/revoke


class EnrollmentStatus(StrEnum):
    """Enrollment status (https://docs.reap.global/agentic-payments/lifecycle.md)."""

    REQUIRES_ACTION = "REQUIRES_ACTION"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class ClientReferenceOwner(Wire):
    """The client's own customer that owns an external-card enrollment."""

    type: Literal["CLIENT_REFERENCE"] = "CLIENT_REFERENCE"
    id: NonEmpty
    email: Email


class ReapUserOwner(Wire):
    """The Reap cardholder resolved from an enrolled Reap card."""

    type: Literal["REAP_USER"]
    id: str


class CreateReapCardEnrollmentRequest(Wire):
    """Enroll a card Reap issued. The OpenAPI says this source is "coming soon"."""

    source: Literal["REAP_CARD"] = "REAP_CARD"
    card_id: UuidFormat


class CreateBinSponsorEnrollmentRequest(Wire):
    """Enroll a BIN-sponsor card. The OpenAPI says this source is "coming soon"."""

    source: Literal["BIN_SPONSOR"] = "BIN_SPONSOR"
    card_id: NonEmpty


class CreateExternalEnrollmentRequest(Wire):
    """Enroll a new card the user enters on Reap's hosted card-entry page."""

    source: Literal["EXTERNAL"] = "EXTERNAL"
    owner: ClientReferenceOwner
    presentation: Presentation


type CreateEnrollmentRequest = Annotated[
    CreateReapCardEnrollmentRequest
    | CreateBinSponsorEnrollmentRequest
    | CreateExternalEnrollmentRequest,
    Field(discriminator="source"),
]
"""``POST /agentic/enrollments``; requires an ``Idempotency-Key``."""


class ReapCardEnrollmentCreated(Wire):
    """The create response for a Reap card."""

    id: str
    status: EnrollmentStatus
    source: Literal["REAP_CARD"]
    owner: ReapUserOwner


class BinSponsorEnrollmentCreated(Wire):
    """The create response for a BIN-sponsor card; it carries no owner."""

    id: str
    status: EnrollmentStatus
    source: Literal["BIN_SPONSOR"]


class ExternalEnrollmentCreated(Wire):
    """The create response for an external card, with the hosted card-entry redirect."""

    id: str
    status: EnrollmentStatus
    source: Literal["EXTERNAL"]
    owner: ClientReferenceOwner
    next_action: NextAction | None


type EnrollmentCreated = Annotated[
    ReapCardEnrollmentCreated | BinSponsorEnrollmentCreated | ExternalEnrollmentCreated,
    Field(discriminator="source"),
]
"""The ``POST /agentic/enrollments`` response; its shape depends on ``source``."""


class EnrollmentOwner(Wire):
    """The owner on a read enrollment: a Reap user or a client reference."""

    type: Literal["REAP_USER", "CLIENT_REFERENCE"]
    id: str
    email: str | None = None


class PaymentMethod(Wire):
    """The stored card, for display: network, last four digits and expiry."""

    type: Literal["CARD"]
    network: str
    last4: str
    expiry_month: int
    expiry_year: int


class Enrollment(Wire):
    """``GET /agentic/enrollments/{id}``, also each item of the list and the revoke response."""

    id: str
    status: EnrollmentStatus
    owner: EnrollmentOwner
    payment_method: PaymentMethod | None
    next_action: NextAction | None
    created_at: str
    updated_at: str


# Products: POST /agentic/products/{search,details,variant}


class MerchantPreference(Wire):
    """Prefer, or search only, one merchant by name."""

    mode: Literal["PREFER", "ONLY"]
    merchant_name: NonEmpty


class SearchContext(Wire):
    """Country and currency for pricing."""

    country: str | None = None
    currency: str | None = None


class PriceFilter(Wire):
    """A price band; strings on the wire, as the OpenAPI has them."""

    min: DecimalString | None = None
    max: DecimalString | None = None


class SearchFilters(Wire):
    """Result filters."""

    price: PriceFilter | None = None
    availability: Literal["AVAILABLE_ONLY"] | None = None


class SearchPagination(Wire):
    """Paging for a search: a cursor from the previous page and a page size of 1 to 50."""

    cursor: str | None = None
    limit: int | None = Field(default=None, ge=1, le=50)


class ProductSearchRequest(Wire):
    """``POST /agentic/products/search``."""

    query: NonEmpty
    merchant_preference: MerchantPreference | None = None
    context: SearchContext | None = None
    filters: SearchFilters | None = None
    pagination: SearchPagination | None = None


class MerchantName(Wire):
    """The merchant behind a product."""

    name: str


class PriceRange(Wire):
    """The lowest and highest variant prices of a product."""

    min: Money
    max: Money


class PreviewVariant(Wire):
    """The one variant a search result previews."""

    id: str
    name: str | None = None
    price: Money
    available: bool | None = None


class ProductSummary(Wire):
    """One search result."""

    id: str
    merchant: MerchantName
    name: str
    image_url: str | None = None
    price_range: PriceRange
    available: bool | None = None
    preview_variant: PreviewVariant | None = None


class SearchPage(Wire):
    """Paging of a search response."""

    next_cursor: str | None
    has_next_page: bool
    returned_count: int


class ProductSearchResponse(Wire):
    """The search response."""

    id: str
    products: list[ProductSummary]
    pagination: SearchPage
    warnings: list[str]


class ProductDetailsRequest(Wire):
    """``POST /agentic/products/details``: 1 to 10 product ids."""

    product_ids: list[str] = Field(min_length=1, max_length=10)


class Media(Wire):
    """An image or other medium of a product or variant."""

    type: str
    url: str
    alt_text: str | None = None


class OptionValue(Wire):
    """One selectable value of an option group, by ``optionId``."""

    option_id: str
    label: str
    available: bool | None = None


class ProductOption(Wire):
    """An option group, such as colour."""

    name: str
    values: list[OptionValue]


class VariantOption(Wire):
    """One option a variant was resolved from."""

    name: str
    value: str


class Variant(Wire):
    """A purchasable variant: ``POST /agentic/products/variant`` and a default variant.

    "The returned variant id is the only id accepted by ``POST /agentic/quotes``."
    """

    id: str
    name: str | None = None
    options: list[VariantOption] | None = None
    price: Money
    available: bool | None = None
    requires_shipping: bool | None = None
    media: list[Media] | None = None


class ProductDetail(Wire):
    """A product expanded into its description, media, option groups and default variant."""

    id: str
    merchant: MerchantName | None = None
    name: str
    description: str | None = None
    media: list[Media] | None = None
    options: list[ProductOption]
    default_variant: Variant


class ProductError(Wire):
    """A product id that failed to resolve; it does not stop the call."""

    product_id: str
    code: str
    message: str | None = None


class ProductDetailsResponse(Wire):
    """The details response."""

    products: list[ProductDetail]
    errors: list[ProductError]


class ResolveVariantRequest(Wire):
    """``POST /agentic/products/variant``: a product and the chosen option ids."""

    product_id: str
    option_ids: list[str] = Field(min_length=1)


# Quotes: POST /agentic/quotes, GET /agentic/quotes/{id},
# POST /agentic/quotes/{id}/shipping-option


class QuoteItem(Wire):
    """One line of an items quote."""

    variant_id: str
    quantity: int = Field(gt=0)


class ShippingAddress(Wire):
    """Required for external checkout quotes and when any item requires shipping."""

    first_name: NonEmpty
    last_name: NonEmpty
    phone: str = Field(pattern=PHONE_PATTERN)
    address_line1: NonEmpty
    address_line2: str | None = None
    city: NonEmpty
    region: str | None = None
    postal_code: str | None = None
    country: str


class ExternalCheckout(Wire):
    """A checkout URL the client assembled; Reap sends it on without rebuilding it."""

    model_config = ConfigDict(extra="forbid")

    merchant_domain: str = Field(min_length=1, max_length=253)
    checkout_url: str = Field(max_length=8192)


class CreateItemsQuoteRequest(Wire):
    """A quote from Reap-discovered variants (1 to 20 items)."""

    model_config = ConfigDict(extra="forbid")

    email: Email
    offer_code: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=_OFFER_CODE_PATTERN
    )
    items: list[QuoteItem] = Field(min_length=1, max_length=20)
    shipping_address: ShippingAddress | None = None


class CreateExternalCheckoutQuoteRequest(Wire):
    """A quote from a checkout URL; the shipping address is always required."""

    model_config = ConfigDict(extra="forbid")

    email: Email
    offer_code: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=_OFFER_CODE_PATTERN
    )
    external_checkout: ExternalCheckout
    shipping_address: ShippingAddress


def _quote_form(value: object) -> str:
    """Tell the two quote request forms apart by which of their keys is present.

    Args:
        value: A raw mapping or an already-built request.

    Returns:
        ``items`` or ``external``.
    """
    if isinstance(value, CreateExternalCheckoutQuoteRequest):
        return "external"
    if isinstance(value, dict) and "externalCheckout" in value:
        return "external"
    return "items"


type CreateQuoteRequest = Annotated[
    Annotated[CreateItemsQuoteRequest, Tag("items")]
    | Annotated[CreateExternalCheckoutQuoteRequest, Tag("external")],
    Discriminator(_quote_form),
]
"""``POST /agentic/quotes``: exactly one of ``items`` and ``externalCheckout``."""


class ShippingOptionDetail(Wire):
    """A free-form key and value on a shipping option."""

    key: str
    value: str


class ShippingOption(Wire):
    """One shipping option; one arrives preselected."""

    id: str
    name: str
    selected: bool
    price: Money
    details: list[ShippingOptionDetail] | None = None


class Tax(Wire):
    """Tax on a quote, and whether prices already include it."""

    amount: Money
    included_in_prices: bool | None = None


class NamedAmount(Wire):
    """A discount or an additional charge."""

    name: str
    amount: Money


class AmountBreakdown(Wire):
    """The itemised total. ``final_amount`` is what the gate decides on, as Reap sends it."""

    items_subtotal: Money
    shipping: Money | None = None
    tax: Tax | None = None
    discounts: list[NamedAmount] | None = None
    additional_charges: list[NamedAmount] | None = None
    final_amount: Money


class Quote(Wire):
    """A priced merchant checkout; it has no status, only ``expiresAt``."""

    id: str
    shipping_options: list[ShippingOption]
    amount_breakdown: AmountBreakdown
    expires_at: str


class SelectShippingOptionRequest(Wire):
    """``POST /agentic/quotes/{id}/shipping-option``."""

    shipping_option_id: str


# Checkouts: POST /agentic/checkouts, GET /agentic/checkouts/{id}

SIMULATE_CHECKOUT_HEADER = "X-Simulate-Checkout"
"""Sandbox only: "Simulates a completed checkout in the sandbox. This header is rejected in
production." Its one value is ``COMPLETED``."""

type SimulateCheckout = Literal["COMPLETED"]


class CheckoutStatus(StrEnum):
    """Checkout status (https://docs.reap.global/agentic-payments/lifecycle.md)."""

    REQUIRES_ACTION = "REQUIRES_ACTION"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"

    @property
    def is_final(self) -> bool:
        """Whether the checkout can never leave this status.

        Returns:
            True for ``COMPLETED``, ``FAILED`` and ``EXPIRED``.
        """
        return self in _FINAL_CHECKOUT_STATUSES


_FINAL_CHECKOUT_STATUSES = frozenset(
    {CheckoutStatus.COMPLETED, CheckoutStatus.FAILED, CheckoutStatus.EXPIRED}
)


class CreateCheckoutRequest(Wire):
    """``POST /agentic/checkouts``; requires an ``Idempotency-Key``."""

    quote_id: UuidId
    enrollment_id: UuidId
    presentation: Presentation


class CheckoutCreated(Wire):
    """The create response: an ``amount`` and the approval redirect, but no ``orderId``."""

    id: str
    status: CheckoutStatus
    quote_id: str
    enrollment_id: str | None
    amount: Money | None = None
    next_action: NextAction | None


class Checkout(Wire):
    """``GET /agentic/checkouts/{id}``; a completed one carries the order id and amount charged."""

    id: str
    status: CheckoutStatus
    quote_id: str | None = None
    enrollment_id: str | None = None
    order_id: str | None
    final_amount: Money | None = None
    next_action: NextAction | None
    created_at: str | None = None
    updated_at: str | None = None

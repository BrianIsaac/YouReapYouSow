"""``KwalClient``: the agentic seam answered by the Kwal participant gateway.

The control plane buys through ``AgenticClient`` (``reap/client.py``): enrolment,
search, details, variant, quote, shipping, checkout and reads of the checkout, in Reap's
shapes. Kwal's participant contract has the same steps on its own routes and shapes
(``kwal/models.py``), so this client translates each operation and the control plane,
the gate's rules, the ledger, the report and the dashboard run unchanged on
``REAP_BACKEND=kwal``. The mapping and the reasons are in ``docs/kwal-backend.md``.

Translations, each a named choice:

- **The enrolment is the participant's set-up card.** Kwal's participant has one card,
  issued by Reap and set up with the skill; purchases are charged to it. Its status is
  ``ACTIVE`` when the setup is ``READY`` (an active card and an observed deposit),
  ``REQUIRES_ACTION`` while it is not started or pending, ``FAILED`` when it needs Kwal's
  operator. Its id is a UUID derived from the session, so it stays the same as the setup
  advances and never carries the vault or owner address; it is not the token and the
  token cannot be read back from it. Kwal reports no card network or last four digits.
- **The search** sends the query and the page size, the only fields Kwal takes; the
  others are named in ``warnings``. A product without a merchant or a price is dropped
  and named there too (rule 9 needs the merchant, rule 10 the price). The search id is
  minted here.
- **Details** read each product and resolve its default variant with the first value of
  each option: Kwal reports no default variant, so one is asked for, never invented.
  A variant's options by name come from its product's read.
- **Quotes** carry no idempotency key on Kwal: each attempt is a new quote, as the seam's
  keys are one per attempt anyway. ``finalAmount`` is Kwal's ``total`` as sent; with no
  ``subtotal`` the total stands for it. Kwal states no "tax included" flag, so it is read
  from Kwal's own figures: included when subtotal and shipping make the total, excluded
  when the tax must be added to make it, unknown otherwise. A quote id that is not a
  UUID (the local quote mode's ``sandbox_quote_``) is carried as a UUID this client maps
  back, because a checkout request takes a UUID; a restart forgets the mapping, and a
  quote it forgot fails its checkout as not found, never charged. A checkout-URL quote
  is refused.
- **The checkout's payment id is the gate's claim key, hashed** (``pay_`` and 32 hex
  digits): the same claim sends the same payment, which Kwal replays and "never pays
  twice" (``references/checkout.md:56``). A 5xx or a lost answer may have saved a payment,
  so it is ambiguous, never a refusal (``references/checkout.md:62``). An unfunded vault
  (``KWAL_FUNDS_NEEDED``) or a busy card (``KWAL_CARD_BUSY``) saved nothing. No completion
  header exists on Kwal: in the sandbox the payment is a card spend Kwal approves against
  the vault (``card_spend`` is True).
- **A payment reads as a checkout**: pending is ``PROCESSING``; completed is ``COMPLETED``
  with Kwal's order id, else the card transaction id, and ``charged`` as the final
  amount; declined, or refused at ``quote_already_used``, is ``FAILED``; any other error
  stays ``PROCESSING``, so the poll's deadline hands it to the operator as an unknown
  outcome; requires-action carries Kwal's approval link.
- **Money** is ``{minorUnits, decimals, currency}`` made exact; USDC is recorded as USD
  at Kwal's own 1 USD = 1 USDC (``references/funding.md:10``); any other currency that is
  not a three-letter code is refused.
- **Observed on the real gateway**: quotes are Reap's landed prices with
  UUID ids (not the local quote mode); every quote needs a delivery address; a first
  quote can wait in Kwal's line (``503 ParticipantUnavailable``) and lands when asked
  again; a product with no options does not resolve to a variant and is reported among
  the details' errors.
- **Errors** are read by their tag only, never their text, which may be private
  (``scripts/transport.py:39-41``). A response that repeats the token is refused.

The dormant card path is not offered: every card-path call answers
``KWAL_CARD_PATH_UNSUPPORTED``, and the startup check refuses ``REAP_PURCHASE_PATH=card``
with ``REAP_BACKEND=kwal``.
"""

import asyncio
import hashlib
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, NoReturn
from urllib.parse import quote as path_quote

import httpx
from pydantic import BaseModel, JsonValue, ValidationError

from youreapyousow.clock import Clock, utc_now
from youreapyousow.config import Settings
from youreapyousow.kwal.models import (
    QUOTE_ALREADY_USED,
    USDC,
    KwalAmount,
    KwalErrorBody,
    KwalFunding,
    KwalPayment,
    KwalProductDetail,
    KwalProducts,
    KwalQuote,
    KwalSetup,
    KwalShippingAddress,
    KwalVariant,
    PaymentRequest,
    PaymentState,
    QuoteLine,
    QuoteRequest,
    SetupState,
    ShippingRequest,
    VariantRequest,
)
from youreapyousow.kwal.session import KwalSession, load_session
from youreapyousow.reap.client import (
    Monotonic,
    ReapError,
    ReapResponseError,
    ReapTransportError,
    RetryPolicy,
    Sleep,
    poll_until_settled,
)
from youreapyousow.reap.models import (
    Account,
    Activity,
    AgenticErrorCode,
    AmountBreakdown,
    Balance,
    Card,
    CardTransaction,
    Checkout,
    CheckoutCreated,
    CheckoutStatus,
    ClientReferenceOwner,
    CreateAccountRequest,
    CreateCardRequest,
    CreateCheckoutRequest,
    CreateEnrollmentRequest,
    CreateExternalEnrollmentRequest,
    CreateItemsQuoteRequest,
    CreateQuoteRequest,
    CreateUserRequest,
    CreateWebhookRequest,
    EffectivePolicies,
    Enrollment,
    EnrollmentCreated,
    EnrollmentOwner,
    EnrollmentStatus,
    ExternalEnrollmentCreated,
    FiatDeposit,
    MerchantName,
    Money,
    NextAction,
    OptionValue,
    Page,
    Policy,
    PolicyCreate,
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
    ScopeType,
    SearchPage,
    SelectShippingOptionRequest,
    ShippingOption,
    SimulateAuthorizationRequest,
    SimulateCheckout,
    SimulateClearingRequest,
    SimulateFiatDepositRequest,
    Tax,
    User,
    Variant,
    VariantOption,
    WebhookEndpoint,
)

PARTICIPANT = "/kwal/participant/v1"
PARTICIPANT_OWNER = "kwal-participant"
"""The enrolment's owner: the participant, never an address."""

_NAMESPACE = uuid.UUID("5f0c8a52-5d07-4f6c-9b3e-6b1f2a1d7c40")
_PAYMENT_PREFIX = "pay_"
_DEFAULT_ERRORS: Mapping[str, str] = {
    "ParticipantUnauthenticated": "KWAL_SESSION_REJECTED",
    "ParticipantNotFound": AgenticErrorCode.AGENTIC_RESOURCE_NOT_FOUND,
    "ParticipantBadRequest": AgenticErrorCode.AGENTIC_REQUEST_REJECTED,
    "ParticipantUnavailable": AgenticErrorCode.AGENTIC_SERVICE_UNAVAILABLE,
    "ParticipantFundingRequest": "KWAL_FUNDS_NEEDED",
    "ParticipantCardBusy": "KWAL_CARD_BUSY",
}
_SETUP_STATUS = {
    SetupState.NOT_STARTED: EnrollmentStatus.REQUIRES_ACTION,
    SetupState.PENDING: EnrollmentStatus.REQUIRES_ACTION,
    SetupState.READY: EnrollmentStatus.ACTIVE,
    SetupState.NEEDS_OPERATOR: EnrollmentStatus.FAILED,
}
_PAYMENT_STATUS = {
    PaymentState.REQUIRES_ACTION: CheckoutStatus.REQUIRES_ACTION,
    PaymentState.PENDING: CheckoutStatus.PROCESSING,
    PaymentState.COMPLETED: CheckoutStatus.COMPLETED,
    PaymentState.DECLINED: CheckoutStatus.FAILED,
}


def payment_id_for(idempotency_key: str) -> str:
    """Mint the Kwal payment id for a claim key: one claim, one payment.

    Args:
        idempotency_key: The gate's claim key.

    Returns:
        ``pay_`` and 32 hex digits of the key's SHA-256.
    """
    return _PAYMENT_PREFIX + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]


def _iso(unix_seconds: int) -> str:
    return datetime.fromtimestamp(unix_seconds, UTC).isoformat()


def _money(amount: KwalAmount, *, field: str) -> Money:
    """Translate a Kwal amount to the seam's money.

    Args:
        amount: The amount as Kwal sent it.
        field: What it is, for the error.

    Returns:
        The exact amount; USDC as USD at Kwal's 1:1.

    Raises:
        ReapResponseError: If it names no currency or one the seam cannot carry.
    """
    currency = amount.currency
    if currency == USDC:
        currency = "USD"
    if currency is None or len(currency) != 3:
        raise ReapResponseError(f"Kwal {field}: an amount in {amount.currency!r} cannot be used")
    return Money(amount=amount.value, currency=currency)


def _zero(currency: str) -> Money:
    return Money(amount=Decimal(0), currency=currency)


def _segment(resource_id: str) -> str:
    return path_quote(resource_id, safe="")


def _unsupported(operation: str) -> NoReturn:
    raise ReapError(
        501,
        "KWAL_CARD_PATH_UNSUPPORTED",
        f"{operation}: the card path is not offered on Kwal; its card is the participant's",
    )


class _NoCardPath:
    """The dormant card path's operations, refused on Kwal."""

    async def create_user(self, req: CreateUserRequest) -> User:
        """Refuse: Kwal's participant is registered with the skill."""
        _unsupported("create_user")

    async def simulate_user_application(self, user_id: str, status: str) -> None:
        """Refuse: no card-path KYC on Kwal."""
        _unsupported("simulate_user_application")

    async def create_account(self, req: CreateAccountRequest, *, idempotency_key: str) -> Account:
        """Refuse: Kwal opens its own account."""
        _unsupported("create_account")

    async def get_balance(self, account_id: str) -> Balance:
        """Refuse: the vault's funding is read with ``funding``."""
        _unsupported("get_balance")

    async def simulate_fiat_deposit(self, req: SimulateFiatDepositRequest) -> FiatDeposit:
        """Refuse: the vault is funded on chain by the owner."""
        _unsupported("simulate_fiat_deposit")

    async def create_card(self, req: CreateCardRequest, *, idempotency_key: str) -> Card:
        """Refuse: Kwal issues its own card."""
        _unsupported("create_card")

    async def get_card(self, card_id: str) -> Card:
        """Refuse: the card is Kwal's."""
        _unsupported("get_card")

    async def freeze_card(self, card_id: str) -> Card:
        """Refuse: the card is Kwal's."""
        _unsupported("freeze_card")

    async def unfreeze_card(self, card_id: str) -> Card:
        """Refuse: the card is Kwal's."""
        _unsupported("unfreeze_card")

    async def delete_card(self, card_id: str) -> None:
        """Refuse: the card is Kwal's."""
        _unsupported("delete_card")

    async def create_policy(self, req: PolicyCreate, *, idempotency_key: str | None) -> Policy:
        """Refuse: no spend policies on Kwal's card."""
        _unsupported("create_policy")

    async def disable_policy(self, policy_id: str) -> Policy:
        """Refuse: no spend policies on Kwal's card."""
        _unsupported("disable_policy")

    async def effective_policies(
        self, scope_type: ScopeType, scope_id: str | None
    ) -> EffectivePolicies:
        """Refuse: no spend policies on Kwal's card."""
        _unsupported("effective_policies")

    async def simulate_authorization(
        self, req: SimulateAuthorizationRequest, *, idempotency_key: str | None
    ) -> CardTransaction:
        """Refuse: Kwal's payment worker makes the card spend."""
        _unsupported("simulate_authorization")

    async def simulate_clearing(self, req: SimulateClearingRequest) -> CardTransaction:
        """Refuse: Kwal's payment worker clears the card spend."""
        _unsupported("simulate_clearing")

    async def get_card_transaction(self, transaction_id: str) -> CardTransaction:
        """Refuse: a payment names its card transaction."""
        _unsupported("get_card_transaction")

    async def list_activities(
        self, *, card_id: str | None, cursor: str | None, limit: int
    ) -> Page[Activity]:
        """Refuse: no activity feed on Kwal."""
        _unsupported("list_activities")

    async def create_webhook(self, req: CreateWebhookRequest) -> WebhookEndpoint:
        """Refuse: Kwal is polled."""
        _unsupported("create_webhook")


class KwalClient(_NoCardPath):
    """The agentic seam over the Kwal participant gateway, with the saved session."""

    def __init__(
        self,
        session: KwalSession,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
        retry: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Monotonic = time.monotonic,
        clock: Clock = utc_now,
        quote_retry_every_s: float = 20.0,
        quote_patience_s: float = 120.0,
    ) -> None:
        """Create the client.

        Args:
            session: The participant session the skill saved.
            transport: Optional transport, used to reach the in-process mock.
            timeout_s: Per-request timeout; the skill allows 30 s.
            retry: How far ``Retry-After`` is honoured; the defaults when None.
            sleep: How the client waits; replaced in tests.
            monotonic: The clock ``poll_checkout`` measures its deadline on.
            clock: Time source for the enrolment's timestamps.
            quote_retry_every_s: The wait between asks of a quote waiting in Kwal's line,
                at least; the skill's own guidance is 15 to 30 s.
            quote_patience_s: How long one quote attempt waits in Kwal's line before the
                seam's ``QUOTE_TEMPORARILY_UNAVAILABLE`` surfaces; shorter than the
                skill's five minutes so one tick of the agent stays bounded.
        """
        self._session = session
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._clock = clock
        self._quote_retry_every_s = quote_retry_every_s
        self._quote_patience_s = quote_patience_s
        self._enrolment_id = str(
            uuid.uuid5(
                _NAMESPACE,
                f"{session.service_url}|"
                + hashlib.sha256(session.token.get_secret_value().encode()).hexdigest(),
            )
        )
        self._quotes: dict[str, str] = {}
        self._seam_quotes: dict[str, str] = {}
        self._options: dict[str, dict[str, tuple[str, str]]] = {}
        self._http = httpx.AsyncClient(
            base_url=session.service_url,
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {session.token.get_secret_value()}",
                "Accept": "application/json",
            },
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "KwalClient":
        """Build the client from the saved session the settings name.

        Args:
            settings: The settings; ``PWS_CREDENTIALS_FILE`` or the skill's default path.

        Returns:
            The client.

        Raises:
            KwalSessionError: If the session cannot be used.
        """
        return cls(load_session(settings.kwal_credentials_path()))

    @property
    def backend(self) -> Literal["kwal"]:
        """Return which backend this is.

        Returns:
            ``kwal``.
        """
        return "kwal"

    @property
    def card_spend(self) -> bool:
        """Return whether checkouts are card spends: on Kwal, always.

        Returns:
            True.
        """
        return True

    @property
    def session(self) -> KwalSession:
        """Return the participant session in use; its token stays secret.

        Returns:
            The session.
        """
        return self._session

    @property
    def enrolment_id(self) -> str:
        """Return the participant card's enrolment id, as the seam carries it.

        Returns:
            A UUID derived from the session.
        """
        return self._enrolment_id

    # Transport

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        params: Mapping[str, str | int] | None = None,
        errors: Mapping[str, str] | None = None,
        unknown_on_5xx: bool = False,
    ) -> JsonValue:
        """Send one request, resending it after a ``429`` as ``RetryPolicy`` allows.

        Args:
            method: HTTP method.
            path: Path under the participant prefix.
            body: JSON body.
            params: Query parameters.
            errors: Error codes by tag for this route, over the defaults.
            unknown_on_5xx: Whether a 5xx may have saved a write (the checkout).

        Returns:
            The decoded JSON body.

        Raises:
            ReapError: Kwal answered with an error.
            ReapTransportError: The request failed in transport, or its outcome is unknown.
        """
        retries = 0
        while True:
            try:
                return await self._once(
                    method,
                    path,
                    body=body,
                    params=params,
                    errors=errors,
                    unknown_on_5xx=unknown_on_5xx,
                )
            except ReapError as error:
                if error.status != 429:
                    raise
                wait = self._retry.wait_for(error, retries, self._retry.rate_limit_retries)
                if wait is None:
                    raise
                retries += 1
                await self._sleep(wait)

    async def _once(
        self,
        method: str,
        path: str,
        *,
        body: BaseModel | None,
        params: Mapping[str, str | int] | None,
        errors: Mapping[str, str] | None,
        unknown_on_5xx: bool,
    ) -> JsonValue:
        route = f"{method} {PARTICIPANT}{path}"
        try:
            response = await self._http.request(
                method,
                f"{PARTICIPANT}{path}",
                json=body.model_dump(mode="json", by_alias=True, exclude_none=True)
                if body is not None
                else None,
                params=dict(params) if params else None,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise ReapTransportError(
                f"{route}: not sent ({type(error).__name__})", maybe_sent=False
            ) from error
        except httpx.HTTPError as error:
            raise ReapTransportError(
                f"{route}: no answer ({type(error).__name__})", maybe_sent=True
            ) from error
        if response.status_code >= 400:
            self._refuse(route, response, errors or {}, unknown_on_5xx=unknown_on_5xx)
        if self._session.token.get_secret_value() in response.text:
            raise ReapResponseError(f"{route}: the response repeats the session token")
        if not response.content:
            raise ReapResponseError(f"{route}: an empty body")
        try:
            return response.json()
        except ValueError as error:
            raise ReapResponseError(f"{route}: not JSON") from error

    @staticmethod
    def _refuse(
        route: str,
        response: httpx.Response,
        errors: Mapping[str, str],
        *,
        unknown_on_5xx: bool,
    ) -> NoReturn:
        try:
            tag = KwalErrorBody.model_validate_json(response.content).tag
        except ValueError:
            tag = None
        status = response.status_code
        named = tag or f"HTTP {status}"
        if unknown_on_5xx and status >= 500:
            raise ReapTransportError(f"{route}: {named}; a payment may exist", maybe_sent=True)
        retry_after = response.headers.get("Retry-After")
        try:
            retry_after_s = float(retry_after) if retry_after is not None else None
        except ValueError:
            retry_after_s = None
        if status == 429:
            code: str = AgenticErrorCode.RATE_LIMIT_EXCEEDED
        elif tag is not None and tag in errors:
            code = errors[tag]
        elif tag is not None and tag in _DEFAULT_ERRORS:
            code = _DEFAULT_ERRORS[tag]
        elif status >= 500:
            code = AgenticErrorCode.AGENTIC_SERVICE_UNAVAILABLE
        else:
            code = f"KWAL_HTTP_{status}"
        raise ReapError(status, str(code), f"{route}: {named}", retry_after_s=retry_after_s)

    async def _read[M: BaseModel](
        self,
        model: type[M],
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        params: Mapping[str, str | int] | None = None,
        errors: Mapping[str, str] | None = None,
        unknown_on_5xx: bool = False,
    ) -> M:
        raw = await self._send(
            method, path, body=body, params=params, errors=errors, unknown_on_5xx=unknown_on_5xx
        )
        try:
            return model.model_validate(raw)
        except ValidationError as error:
            raise ReapResponseError(
                f"{method} {PARTICIPANT}{path}: unreadable response: {error.error_count()} "
                f"field(s) off the participant contract"
            ) from error

    # Setup and funding, for /status and swap_check

    async def setup(self) -> KwalSetup:
        """Read the participant's vault and card setup: ``GET /status``.

        Returns:
            The setup as Kwal reports it.
        """
        return await self._read(KwalSetup, "GET", "/status")

    async def funding(self) -> KwalFunding:
        """Read what the card can spend from the vault: ``GET /funding``.

        Returns:
            The funding readiness, in USDC.
        """
        return await self._read(KwalFunding, "GET", "/funding")

    # Enrolment

    async def _enrolment(self) -> Enrollment:
        setup = await self.setup()
        now = self._clock().isoformat()
        return Enrollment(
            id=self._enrolment_id,
            status=_SETUP_STATUS[setup.state],
            owner=EnrollmentOwner(type="CLIENT_REFERENCE", id=PARTICIPANT_OWNER),
            payment_method=None,
            next_action=None,
            created_at=now,
            updated_at=now,
        )

    async def create_enrollment(
        self, req: CreateEnrollmentRequest, *, idempotency_key: str
    ) -> EnrollmentCreated:
        """Return the participant's card as the objective's enrolment; nothing is created.

        Args:
            req: An ``EXTERNAL`` request; its owner is kept as the enrolment's.
            idempotency_key: Unused: nothing is written.

        Returns:
            The participant card's enrolment, as its setup reads now.

        Raises:
            ReapError: For another source.
        """
        if not isinstance(req, CreateExternalEnrollmentRequest):
            raise ReapError(
                400,
                AgenticErrorCode.AGENTIC_REQUEST_REJECTED,
                "Kwal's enrolment is the participant's own card",
            )
        enrolment = await self._enrolment()
        return ExternalEnrollmentCreated(
            id=enrolment.id,
            status=enrolment.status,
            source="EXTERNAL",
            owner=ClientReferenceOwner(id=req.owner.id, email=req.owner.email),
            next_action=None,
        )

    async def get_enrollment(self, enrollment_id: str) -> Enrollment:
        """Read the participant card's enrolment.

        Args:
            enrollment_id: The id ``create_enrollment`` gave.

        Returns:
            The enrolment, as the setup reads now.

        Raises:
            ReapError: ``ENROLLMENT_NOT_FOUND`` for any other id.
        """
        self._own(enrollment_id)
        return await self._enrolment()

    def _own(self, enrollment_id: str) -> None:
        if enrollment_id != self._enrolment_id:
            raise ReapError(
                404, AgenticErrorCode.ENROLLMENT_NOT_FOUND, "not this participant's enrolment"
            )

    async def list_enrollments(
        self,
        owner_id: str,
        *,
        owner_type: Literal["REAP_USER", "CLIENT_REFERENCE"] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[Enrollment]:
        """List the participant's one enrolment.

        Args:
            owner_id: Unused: the participant has one card.
            owner_type: Unused.
            limit: Unused.
            cursor: Unused.

        Returns:
            One page holding the enrolment.
        """
        return Page[Enrollment](items=[await self._enrolment()], next_cursor=None)

    async def revoke_enrollment(self, enrollment_id: str) -> Enrollment:
        """Refuse: the participant's card is resolved with Kwal's operator.

        Args:
            enrollment_id: The enrolment.

        Raises:
            ReapError: Always.
        """
        self._own(enrollment_id)
        raise ReapError(
            400,
            AgenticErrorCode.AGENTIC_REQUEST_REJECTED,
            "Kwal's card cannot be revoked here; ask Kwal's operator",
        )

    # Discovery

    async def search_products(self, req: ProductSearchRequest) -> ProductSearchResponse:
        """Search Kwal's catalogue: ``GET /products?query=&limit=``.

        Args:
            req: The search; only the query and the page size reach Kwal.

        Returns:
            The products that carry a merchant and a price, and what was left out.
        """
        params: dict[str, str | int] = {"query": req.query}
        warnings: list[str] = []
        if req.pagination is not None and req.pagination.limit is not None:
            params["limit"] = req.pagination.limit
        unsent = [
            name
            for name, value in (
                ("merchantPreference", req.merchant_preference),
                ("context", req.context),
                ("filters", req.filters),
                ("cursor", req.pagination.cursor if req.pagination else None),
            )
            if value is not None
        ]
        if unsent:
            warnings.append(
                f"not sent to Kwal, whose search takes a query and a limit: {', '.join(unsent)}"
            )
        found = await self._read(KwalProducts, "GET", "/products", params=params)
        products: list[ProductSummary] = []
        for product in found.products:
            if product.merchant is None:
                warnings.append(f"{product.product_id} left out: Kwal reported no merchant")
                continue
            if product.price is None or product.price.currency is None:
                warnings.append(f"{product.product_id} left out: Kwal reported no price")
                continue
            price = _money(product.price, field="product price")
            products.append(
                ProductSummary(
                    id=product.product_id,
                    merchant=MerchantName(name=product.merchant),
                    name=product.title,
                    price_range=PriceRange(min=price, max=price),
                )
            )
        return ProductSearchResponse(
            id=f"kwal-search-{uuid.uuid4().hex}",
            products=products,
            pagination=SearchPage(
                next_cursor=None, has_next_page=False, returned_count=len(products)
            ),
            warnings=warnings,
        )

    async def _product(self, product_id: str) -> KwalProductDetail:
        detail = await self._read(KwalProductDetail, "GET", f"/products/{_segment(product_id)}")
        self._options[product_id] = {
            value.option_id: (option.name, value.label)
            for option in detail.options
            for value in option.values
        }
        return detail

    async def _variant(self, product_id: str, option_ids: list[str]) -> Variant:
        resolved = await self._read(
            KwalVariant,
            "POST",
            f"/products/{_segment(product_id)}/variant",
            body=VariantRequest(option_ids=option_ids),
            errors={"ParticipantBadRequest": AgenticErrorCode.VARIANT_RESOLUTION_FAILED},
        )
        names = self._options.get(product_id, {})
        price = (
            _money(resolved.price, field="variant price")
            if resolved.price is not None and resolved.price.currency is not None
            else _zero("USD")
        )
        return Variant(
            id=resolved.variant_id,
            name=resolved.title,
            options=[
                VariantOption(name=names[option][0], value=names[option][1])
                for option in option_ids
                if option in names
            ],
            price=price,
            available=resolved.purchasable,
        )

    async def product_details(self, req: ProductDetailsRequest) -> ProductDetailsResponse:
        """Read each product and resolve its default variant.

        Args:
            req: One to ten product ids.

        Returns:
            The products read, and each id that failed with its code.
        """
        products: list[ProductDetail] = []
        failed: list[ProductError] = []
        for product_id in req.product_ids:
            try:
                detail = await self._product(product_id)
                default = await self._variant(
                    product_id, [option.values[0].option_id for option in detail.options]
                )
            except ReapError as error:
                failed.append(
                    ProductError(product_id=product_id, code=error.code, message=error.message)
                )
                continue
            products.append(
                ProductDetail(
                    id=detail.product.product_id,
                    merchant=MerchantName(name=detail.product.merchant)
                    if detail.product.merchant
                    else None,
                    name=detail.product.title,
                    options=[
                        ProductOption(
                            name=option.name,
                            values=[
                                OptionValue(option_id=v.option_id, label=v.label)
                                for v in option.values
                            ],
                        )
                        for option in detail.options
                    ],
                    default_variant=default,
                )
            )
        return ProductDetailsResponse(products=products, errors=failed)

    async def resolve_variant(self, req: ResolveVariantRequest) -> Variant:
        """Resolve chosen options to the variant a quote accepts.

        Args:
            req: The product and one option id per option.

        Returns:
            The variant, with its options by name.
        """
        if req.product_id not in self._options:
            await self._product(req.product_id)
        return await self._variant(req.product_id, list(req.option_ids))

    # Quotes

    def _seam_quote_id(self, kwal_id: str) -> str:
        try:
            uuid.UUID(kwal_id)
        except ValueError:
            seam = str(uuid.uuid5(_NAMESPACE, f"quote|{kwal_id}"))
            self._quotes[seam] = kwal_id
            self._seam_quotes[kwal_id] = seam
            return seam
        return kwal_id

    def _kwal_quote_id(self, seam_id: str) -> str:
        return self._quotes.get(seam_id, seam_id)

    def _quote(self, quote: KwalQuote) -> Quote:
        total = _money(quote.total, field="quote total")
        options = [
            ShippingOption(
                id=o.shipping_option_id,
                name=o.label,
                selected=o.shipping_option_id == quote.selected_shipping_option_id,
                price=_money(o.price, field="shipping price")
                if o.price is not None and o.price.currency is not None
                else _zero(total.currency),
            )
            for o in quote.shipping_options
        ]

        def part(amount: KwalAmount | None, field: str) -> Money | None:
            if amount is None or amount.currency is None:
                return None
            return _money(amount, field=field)

        tax = part(quote.tax, "tax")
        subtotal = part(quote.subtotal, "subtotal")
        shipping = part(quote.shipping, "shipping")
        included: bool | None = None
        if tax is not None and tax.amount > 0 and subtotal is not None:
            before_tax = subtotal.amount + (shipping.amount if shipping else Decimal(0))
            if before_tax == total.amount:
                included = True
            elif before_tax + tax.amount == total.amount:
                included = False
        return Quote(
            id=self._seam_quote_id(quote.quote_id),
            shipping_options=options,
            amount_breakdown=AmountBreakdown(
                items_subtotal=subtotal or total,
                shipping=shipping,
                tax=Tax(amount=tax, included_in_prices=included) if tax is not None else None,
                final_amount=total,
            ),
            expires_at=_iso(quote.expires_at_unix_seconds),
        )

    async def create_quote(
        self,
        req: CreateQuoteRequest,
        *,
        idempotency_key: str,
        retry_key: Callable[[], str] | None = None,
    ) -> Quote:
        """Price catalogue variants: ``POST /quotes``.

        ``ParticipantUnavailable`` on a quote is Kwal's line: "A call that waits in line
        keeps its place ... A retry with the same inputs gets the result of the call in
        line" (``references/quotes.md:68``). So the same request is asked again every
        ``quote_retry_every_s`` (or ``Retry-After``, if longer) until
        ``quote_patience_s``, then ``QUOTE_TEMPORARILY_UNAVAILABLE`` surfaces. It stays
        one attempt: no new attempt key is minted. The real gateway has answered a first
        quote this way and the same quote a moment later.

        Args:
            req: An items quote; a checkout-URL quote is refused.
            idempotency_key: The attempt's key; Kwal takes none, so it is only recorded.
            retry_key: Unused: a resend on Kwal is the same attempt.

        Returns:
            The quote in the seam's shape.

        Raises:
            ReapError: Kwal refused the quote, it is a checkout-URL quote, or Kwal was
                still unavailable after the retries.
        """
        if not isinstance(req, CreateItemsQuoteRequest):
            raise ReapError(
                400,
                AgenticErrorCode.AGENTIC_REQUEST_REJECTED,
                "Kwal quotes catalogue variants only, not a checkout URL",
            )
        address = req.shipping_address
        body = QuoteRequest(
            email=req.email,
            lines=[QuoteLine(variant_id=i.variant_id, quantity=i.quantity) for i in req.items],
            shipping_address=KwalShippingAddress(
                first_name=address.first_name,
                last_name=address.last_name,
                phone=address.phone,
                line1=address.address_line1,
                line2=address.address_line2,
                city=address.city,
                region=address.region,
                postal_code=address.postal_code,
                country=address.country,
            )
            if address is not None
            else None,
        )
        started = self._monotonic()
        while True:
            try:
                landed = await self._read(
                    KwalQuote,
                    "POST",
                    "/quotes",
                    body=body,
                    errors={
                        "ParticipantUnavailable": AgenticErrorCode.QUOTE_TEMPORARILY_UNAVAILABLE
                    },
                )
                return self._quote(landed)
            except ReapError as error:
                if error.code != AgenticErrorCode.QUOTE_TEMPORARILY_UNAVAILABLE:
                    raise
                wait = max(error.retry_after_s or 0.0, self._quote_retry_every_s)
                if self._monotonic() - started + wait > self._quote_patience_s:
                    raise
                await self._sleep(wait)

    async def get_quote(self, quote_id: str) -> Quote:
        """Read a quote: ``GET /quotes/{id}``.

        Args:
            quote_id: The quote, as the seam carries it.

        Returns:
            The quote.
        """
        landed = await self._read(
            KwalQuote,
            "GET",
            f"/quotes/{_segment(self._kwal_quote_id(quote_id))}",
            errors={"ParticipantNotFound": AgenticErrorCode.QUOTE_NOT_FOUND},
        )
        return self._quote(landed)

    async def select_shipping_option(
        self, quote_id: str, req: SelectShippingOptionRequest, *, idempotency_key: str | None
    ) -> Quote:
        """Choose a shipping option: ``POST /quotes/{id}/shipping``.

        Args:
            quote_id: The quote, as the seam carries it.
            req: The option.
            idempotency_key: Unused: Kwal takes none.

        Returns:
            The quote, re-priced.
        """
        landed = await self._read(
            KwalQuote,
            "POST",
            f"/quotes/{_segment(self._kwal_quote_id(quote_id))}/shipping",
            body=ShippingRequest(shipping_option_id=req.shipping_option_id),
            errors={
                "ParticipantBadRequest": AgenticErrorCode.SHIPPING_OPTION_INVALID,
                "ParticipantNotFound": AgenticErrorCode.QUOTE_NOT_FOUND,
            },
        )
        return self._quote(landed)

    # Checkouts

    def _status(self, payment: KwalPayment) -> CheckoutStatus:
        if payment.state == PaymentState.ERROR:
            if payment.step == QUOTE_ALREADY_USED:
                return CheckoutStatus.FAILED
            return CheckoutStatus.PROCESSING
        return _PAYMENT_STATUS[payment.state]

    @staticmethod
    def _next_action(payment: KwalPayment) -> NextAction | None:
        if payment.state != PaymentState.REQUIRES_ACTION or payment.approval_url is None:
            return None
        expiry = payment.approval_expires_at_unix_seconds
        return NextAction(
            type="REDIRECT",
            url=payment.approval_url,
            expires_at=_iso(expiry) if expiry is not None else None,
        )

    async def create_checkout(
        self,
        req: CreateCheckoutRequest,
        *,
        idempotency_key: str,
        simulate: SimulateCheckout | None = None,
    ) -> CheckoutCreated:
        """Pay a quote with the participant's card: ``POST /payments``.

        Args:
            req: The quote and the participant's enrolment.
            idempotency_key: The gate's claim key; it mints the payment id.
            simulate: Unused: Kwal has no completion header (``card_spend``).

        Returns:
            The payment as a created checkout.

        Raises:
            ReapError: ``KWAL_FUNDS_NEEDED``, ``KWAL_CARD_BUSY`` or another definite
                refusal; nothing was saved.
            ReapTransportError: The outcome is unknown; replay the same key.
        """
        self._own(req.enrollment_id)
        payment_id = payment_id_for(idempotency_key)
        payment = await self._read(
            KwalPayment,
            "POST",
            "/payments",
            body=PaymentRequest(payment_id=payment_id, quote_id=self._kwal_quote_id(req.quote_id)),
            errors={"ParticipantNotFound": AgenticErrorCode.QUOTE_NOT_FOUND},
            unknown_on_5xx=True,
        )
        if payment.payment_id != payment_id:
            raise ReapResponseError("POST /payments: the answer names another payment")
        return CheckoutCreated(
            id=payment.payment_id,
            status=self._status(payment),
            quote_id=req.quote_id,
            enrollment_id=req.enrollment_id,
            amount=_money(payment.amount, field="payment amount")
            if payment.amount is not None and payment.amount.currency is not None
            else None,
            next_action=self._next_action(payment),
        )

    async def get_checkout(self, checkout_id: str) -> Checkout:
        """Read a payment as a checkout: ``GET /payments/{id}``.

        Args:
            checkout_id: The payment id.

        Returns:
            The checkout; a completed one carries its order id and the amount charged.

        Raises:
            ReapResponseError: A completed payment that names no amount.
        """
        payment = await self._read(
            KwalPayment,
            "GET",
            f"/payments/{_segment(checkout_id)}",
            errors={"ParticipantNotFound": AgenticErrorCode.CHECKOUT_NOT_FOUND},
        )
        if payment.payment_id != checkout_id:
            raise ReapResponseError(f"GET /payments/{checkout_id}: the answer names another")
        status = self._status(payment)
        order_id: str | None = None
        final: Money | None = None
        if status == CheckoutStatus.COMPLETED:
            order_id = payment.order_id or payment.card_transaction_id or payment.payment_id
            charged = payment.charged if payment.charged is not None else payment.amount
            if charged is None or charged.currency is None:
                raise ReapResponseError(f"payment {checkout_id} completed without an amount")
            final = _money(charged, field="charged")
        quote_id = payment.quote_id
        return Checkout(
            id=payment.payment_id,
            status=status,
            quote_id=self._seam_quotes.get(quote_id, quote_id) if quote_id else None,
            enrollment_id=self._enrolment_id,
            order_id=order_id,
            final_amount=final,
            next_action=self._next_action(payment),
        )

    async def poll_checkout(
        self,
        checkout_id: str,
        *,
        every_s: float = 1.0,
        deadline_s: float = 120.0,
        max_every_s: float = 5.0,
    ) -> Checkout:
        """Read a payment until it settles, waits for approval, or the deadline passes.

        Args:
            checkout_id: The payment id.
            every_s: The first wait between reads.
            deadline_s: How long to poll before giving up.
            max_every_s: The longest wait between reads.

        Returns:
            The checkout once it is ``COMPLETED``, ``FAILED``, ``EXPIRED`` or
            ``REQUIRES_ACTION``.
        """
        return await poll_until_settled(
            self.get_checkout,
            checkout_id,
            every_s=every_s,
            deadline_s=deadline_s,
            max_every_s=max_every_s,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

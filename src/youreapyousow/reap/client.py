"""The Reap client interface, its one HTTP implementation, and the two backends.

``ReapSandbox`` speaks to Reap's real sandbox; its wire code is exercised in the tests
against the mock only. ``ReapMock`` is the same HTTP client pointed at the in-process
mock server, so the two share every line of wire code and return identical shapes by
construction.

The agentic operations follow https://docs.reap.global/api-reference/openapi.json and
the guides under https://docs.reap.global/agentic-payments/. Retrying follows
https://docs.reap.global/api-reference/idempotency.md, the rate-limiting page and Reap's
changelog:

- A ``429`` is not cached, so it is sent again with the same key after ``Retry-After``.
- ``QUOTE_TEMPORARILY_UNAVAILABLE`` is cached under its key ("Reusing the same idempotency
  key replays the first response, including ``503``. A new key starts a new attempt."), so
  a quote is retried only under a fresh key the caller mints.
- ``CHECKOUT_TEMPORARILY_UNAVAILABLE`` is never retried here: the control plane answers it
  with a fresh quote and a new claim.

Where the docs are silent, this module assumes, by name: ``Retry-After`` is a whole or
decimal number of seconds (both pages say "the number of seconds"); a missing,
negative or non-numeric one waits ``RetryPolicy.default_wait_s``; a wait longer than
``RetryPolicy.max_wait_s`` is not taken but surfaced on ``ReapError.retry_after_s``; and a
``429`` is recognised by its status as well as its code, since a proxy in front of Reap
could answer one without Reap's body.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, JsonValue, SecretStr, TypeAdapter, ValidationError

from youreapyousow.clock import Clock, utc_now
from youreapyousow.reap.mock.agentic import AgenticMockEngine
from youreapyousow.reap.mock.engine import (
    AuthorizationMode,
    DeliveryResponse,
    MockReapEngine,
    WebhookDelivery,
)
from youreapyousow.reap.mock.server import create_mock_app
from youreapyousow.reap.models import (
    REAP_VERSION,
    SIMULATE_CHECKOUT_HEADER,
    Account,
    Activity,
    AgenticErrorCode,
    Balance,
    Card,
    CardTransaction,
    Checkout,
    CheckoutCreated,
    CheckoutStatus,
    CreateAccountRequest,
    CreateCardRequest,
    CreateCheckoutRequest,
    CreateEnrollmentRequest,
    CreateQuoteRequest,
    CreateUserRequest,
    CreateWebhookRequest,
    EffectivePolicies,
    Enrollment,
    EnrollmentCreated,
    ErrorResponse,
    FiatDeposit,
    Page,
    Policy,
    PolicyCreate,
    ProductDetailsRequest,
    ProductDetailsResponse,
    ProductSearchRequest,
    ProductSearchResponse,
    Quote,
    ResolveVariantRequest,
    ScopeType,
    SelectShippingOptionRequest,
    SimulateApplicationRequest,
    SimulateAuthorizationRequest,
    SimulateCheckout,
    SimulateClearingRequest,
    SimulateFiatDepositRequest,
    User,
    Variant,
    WebhookEndpoint,
    Wire,
)

SG_SANDBOX_URL = "https://sg.sandbox.api.reap.global"
MOCK_BASE_URL = "http://reap-mock.local"

type Backend = Literal["mock", "sandbox", "kwal"]

_TRANSACTION = TypeAdapter[CardTransaction](CardTransaction)
_POLICY = TypeAdapter[Policy](Policy)
_ACTIVITIES = TypeAdapter[Page[Activity]](Page[Activity])
_ENROLLMENT_CREATED = TypeAdapter[EnrollmentCreated](EnrollmentCreated)
_ENROLLMENTS = TypeAdapter[Page[Enrollment]](Page[Enrollment])
_ENROLLMENT = TypeAdapter[Enrollment](Enrollment)
_SEARCH = TypeAdapter[ProductSearchResponse](ProductSearchResponse)
_DETAILS = TypeAdapter[ProductDetailsResponse](ProductDetailsResponse)
_VARIANT = TypeAdapter[Variant](Variant)
_QUOTE = TypeAdapter[Quote](Quote)
_CHECKOUT_CREATED = TypeAdapter[CheckoutCreated](CheckoutCreated)
_CHECKOUT = TypeAdapter[Checkout](Checkout)

# Idempotency-Key bounds on every agentic create (OpenAPI: minLength 1, maxLength 255).
_MAX_KEY_LENGTH = 255
# The statuses ``poll_checkout`` returns on; only ``PROCESSING`` keeps it polling.
_POLL_EXITS = frozenset(
    {
        CheckoutStatus.COMPLETED,
        CheckoutStatus.FAILED,
        CheckoutStatus.EXPIRED,
        CheckoutStatus.REQUIRES_ACTION,
    }
)

type Sleep = Callable[[float], Awaitable[None]]
type Monotonic = Callable[[], float]


class ReapError(Exception):
    """Reap answered with an error body. Branch on ``code``, never on ``status``."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        """Create the error.

        Args:
            status: HTTP status.
            code: Reap's error code, such as ``CARD_NOT_FOUND``.
            message: Reap's message.
            detail: Reap's ``detail`` object, such as ``{"reason": "EXPIRED"}``.
            retry_after_s: The ``Retry-After`` the response carried, in seconds.
        """
        super().__init__(f"Reap {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail
        self.retry_after_s = retry_after_s


class ReapTransportError(Exception):
    """The request failed in transport.

    Attributes:
        maybe_sent: False only when the connection was never established, so the
            request certainly did not reach Reap; otherwise the outcome is unknown.
    """

    def __init__(self, message: str, *, maybe_sent: bool) -> None:
        """Create the error.

        Args:
            message: What failed.
            maybe_sent: Whether the request may have reached Reap.
        """
        super().__init__(message)
        self.maybe_sent = maybe_sent


class ReapResponseError(ReapTransportError):
    """Reap answered 2xx with a body the models cannot read.

    The request certainly reached Reap and its outcome is not known to this side, so it is a
    transport error with ``maybe_sent`` always True: a checkout create that ends here is
    reconciled, never retried under a new key.
    """

    def __init__(self, message: str) -> None:
        """Create the error.

        Args:
            message: What could not be read.
        """
        super().__init__(message, maybe_sent=True)


class CheckoutPollTimeoutError(Exception):
    """A checkout was still ``PROCESSING``, or unreadable, when the poll's deadline passed.

    Attributes:
        checkout_id: The checkout polled.
        last: The last checkout read, or None if no read succeeded.
        waited_s: How long the poll ran.
    """

    def __init__(self, checkout_id: str, last: Checkout | None, waited_s: float) -> None:
        """Create the timeout.

        Args:
            checkout_id: The checkout polled.
            last: The last checkout read, if any.
            waited_s: How long the poll ran.
        """
        status = last.status.value if last is not None else "unread"
        super().__init__(f"checkout {checkout_id} still {status} after {waited_s:g} s")
        self.checkout_id = checkout_id
        self.last = last
        self.waited_s = waited_s


@dataclass(frozen=True)
class RetryPolicy:
    """How far the client goes in honouring ``Retry-After`` before surfacing the error.

    Attributes:
        rate_limit_retries: Resends after a ``429``, each under the same key.
        quote_retries: Fresh attempts after ``QUOTE_TEMPORARILY_UNAVAILABLE``, each under a
            new key.
        max_wait_s: The longest single wait taken; a longer ``Retry-After`` is surfaced.
        default_wait_s: The wait when ``Retry-After`` is missing or unreadable.
    """

    rate_limit_retries: int = 3
    quote_retries: int = 2
    max_wait_s: float = 10.0
    default_wait_s: float = 1.0

    def wait_for(self, error: ReapError, retries_so_far: int, limit: int) -> float | None:
        """Decide whether to wait and retry after an error, and for how long.

        Args:
            error: The error Reap answered with.
            retries_so_far: Retries already made for this request.
            limit: The most retries allowed.

        Returns:
            Seconds to wait, or None to surface the error now.
        """
        if retries_so_far >= limit:
            return None
        wait = error.retry_after_s if error.retry_after_s is not None else self.default_wait_s
        return wait if wait <= self.max_wait_s else None


def _retry_after(response: httpx.Response) -> float | None:
    """Read ``Retry-After`` as seconds.

    Args:
        response: The error response.

    Returns:
        The seconds to wait, or None if the header is missing, negative or not a number.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _is_rate_limit(error: ReapError) -> bool:
    """Whether an error is a ``429``, by code or by status.

    Args:
        error: The error.

    Returns:
        True for a rate-limit answer.
    """
    return error.code == AgenticErrorCode.RATE_LIMIT_EXCEEDED or error.status == 429


def _check_key(idempotency_key: str) -> str:
    """Check an ``Idempotency-Key`` against Reap's bounds before anything is sent.

    Args:
        idempotency_key: The key.

    Returns:
        The key, unchanged.

    Raises:
        ValueError: If it is empty or longer than 255 characters.
    """
    if not 1 <= len(idempotency_key) <= _MAX_KEY_LENGTH:
        raise ValueError(f"Idempotency-Key must be 1 to {_MAX_KEY_LENGTH} characters")
    return idempotency_key


def _segment(resource_id: str) -> str:
    """Quote an id for use as one path segment.

    Args:
        resource_id: An id Reap issued.

    Returns:
        The id, percent-encoded so it cannot change the path.
    """
    return quote(resource_id, safe="")


async def poll_until_settled(
    read: Callable[[str], Awaitable[Checkout]],
    checkout_id: str,
    *,
    every_s: float,
    deadline_s: float,
    max_every_s: float,
    sleep: Sleep,
    monotonic: Monotonic,
) -> Checkout:
    """Read a checkout until it reaches one of four exits or the deadline passes.

    It reads at once, then after ``every_s``, doubling the wait up to ``max_every_s``
    and never sleeping past the deadline, where it reads one last time. A dropped read
    or a transient code (``AGENTIC_SERVICE_UNAVAILABLE``, a ``429`` beyond the client's
    own retries) is ridden out until the deadline; any other error surfaces. Every
    backend's ``poll_checkout`` is this loop over its own ``get_checkout``.

    Args:
        read: The backend's ``get_checkout``.
        checkout_id: The checkout.
        every_s: The first wait between reads.
        deadline_s: How long to poll before giving up.
        max_every_s: The longest wait between reads.
        sleep: How to wait.
        monotonic: The clock the deadline is measured on.

    Returns:
        The checkout once it is ``COMPLETED``, ``FAILED``, ``EXPIRED`` or
        ``REQUIRES_ACTION``.

    Raises:
        ValueError: If the timing makes no sense.
        CheckoutPollTimeoutError: Still ``PROCESSING``, or never read, at the deadline.
        ReapError: The backend answered with a definite error, such as
            ``CHECKOUT_NOT_FOUND``.
    """
    if every_s <= 0 or deadline_s < 0 or max_every_s < every_s:
        raise ValueError("poll timing needs every_s > 0, deadline_s >= 0, max_every_s >= every_s")
    started = monotonic()
    wait = every_s
    last: Checkout | None = None
    last_error: Exception | None = None
    while True:
        try:
            last = await read(checkout_id)
        except ReapTransportError as error:
            last_error = error
        except ReapError as error:
            transient = _is_rate_limit(error) or (
                error.code == AgenticErrorCode.AGENTIC_SERVICE_UNAVAILABLE
            )
            if not transient:
                raise
            last_error = error
        else:
            if last.status in _POLL_EXITS:
                return last
            last_error = None
        elapsed = monotonic() - started
        if elapsed >= deadline_s:
            raise CheckoutPollTimeoutError(checkout_id, last, elapsed) from last_error
        await sleep(min(wait, deadline_s - elapsed))
        wait = min(wait * 2, max_every_s)


class AgenticClient(Protocol):
    """Reap's Agentic Payments operations: enrolment, discovery, quotes and checkouts."""

    @property
    def card_spend(self) -> bool:
        """Whether every sandbox checkout is a simulated spend on the participant's own card.

        True for Kwal, whose sandbox payment is approved against the participant's vault
        and takes no completion header; False for Reap, where a checkout the gate allows
        is sent with ``X-Simulate-Checkout: COMPLETED``.
        """
        ...

    async def create_enrollment(
        self, req: CreateEnrollmentRequest, *, idempotency_key: str
    ) -> EnrollmentCreated:
        """Store a card for agentic purchases (``POST /agentic/enrollments``)."""
        ...

    async def get_enrollment(self, enrollment_id: str) -> Enrollment:
        """Read an enrollment (``GET /agentic/enrollments/{id}``)."""
        ...

    async def list_enrollments(
        self,
        owner_id: str,
        *,
        owner_type: Literal["REAP_USER", "CLIENT_REFERENCE"] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[Enrollment]:
        """List one owner's enrollments (``GET /agentic/enrollments``)."""
        ...

    async def revoke_enrollment(self, enrollment_id: str) -> Enrollment:
        """Revoke an enrollment, finally (``POST /agentic/enrollments/{id}/revoke``)."""
        ...

    async def search_products(self, req: ProductSearchRequest) -> ProductSearchResponse:
        """Search merchant catalogues (``POST /agentic/products/search``)."""
        ...

    async def product_details(self, req: ProductDetailsRequest) -> ProductDetailsResponse:
        """Expand products into options and variants (``POST /agentic/products/details``)."""
        ...

    async def resolve_variant(self, req: ResolveVariantRequest) -> Variant:
        """Resolve chosen options to a variant (``POST /agentic/products/variant``)."""
        ...

    async def create_quote(
        self,
        req: CreateQuoteRequest,
        *,
        idempotency_key: str,
        retry_key: Callable[[], str] | None = None,
    ) -> Quote:
        """Price a merchant checkout (``POST /agentic/quotes``)."""
        ...

    async def get_quote(self, quote_id: str) -> Quote:
        """Read a quote (``GET /agentic/quotes/{id}``)."""
        ...

    async def select_shipping_option(
        self, quote_id: str, req: SelectShippingOptionRequest, *, idempotency_key: str | None
    ) -> Quote:
        """Choose shipping and re-price (``POST /agentic/quotes/{id}/shipping-option``)."""
        ...

    async def create_checkout(
        self,
        req: CreateCheckoutRequest,
        *,
        idempotency_key: str,
        simulate: SimulateCheckout | None = None,
    ) -> CheckoutCreated:
        """Open the payment for a quote (``POST /agentic/checkouts``)."""
        ...

    async def get_checkout(self, checkout_id: str) -> Checkout:
        """Read a checkout (``GET /agentic/checkouts/{id}``)."""
        ...

    async def poll_checkout(
        self,
        checkout_id: str,
        *,
        every_s: float = 1.0,
        deadline_s: float = 120.0,
        max_every_s: float = 5.0,
    ) -> Checkout:
        """Read a checkout until it leaves ``PROCESSING`` or the deadline passes."""
        ...


class ReapClient(AgenticClient, Protocol):
    """The Reap operations the control plane uses."""

    @property
    def backend(self) -> Backend:
        """Which backend this is, shown on the dashboard."""
        ...

    async def create_user(self, req: CreateUserRequest) -> User:
        """Create a cardholder (``POST /users/``)."""
        ...

    async def simulate_user_application(self, user_id: str, status: str) -> None:
        """Set KYC status (``POST /simulation/users/{id}/application``)."""
        ...

    async def create_account(self, req: CreateAccountRequest, *, idempotency_key: str) -> Account:
        """Open an account (``POST /accounts/``)."""
        ...

    async def get_balance(self, account_id: str) -> Balance:
        """Read a balance (``GET /accounts/{id}/balance``)."""
        ...

    async def simulate_fiat_deposit(self, req: SimulateFiatDepositRequest) -> FiatDeposit:
        """Fund the project (``POST /simulation/fiat-deposits``)."""
        ...

    async def create_card(self, req: CreateCardRequest, *, idempotency_key: str) -> Card:
        """Issue a card (``POST /cards/``)."""
        ...

    async def get_card(self, card_id: str) -> Card:
        """Fetch a card (``GET /cards/{id}``)."""
        ...

    async def freeze_card(self, card_id: str) -> Card:
        """Freeze a card (``POST /cards/{id}/freeze``)."""
        ...

    async def unfreeze_card(self, card_id: str) -> Card:
        """Unfreeze a card (``POST /cards/{id}/unfreeze``)."""
        ...

    async def delete_card(self, card_id: str) -> None:
        """Delete a card irreversibly (``DELETE /cards/{id}``)."""
        ...

    async def create_policy(self, req: PolicyCreate, *, idempotency_key: str | None) -> Policy:
        """Attach a spend policy (``POST /policies/``)."""
        ...

    async def disable_policy(self, policy_id: str) -> Policy:
        """Disable a policy (``POST /policies/{id}/disable``)."""
        ...

    async def effective_policies(
        self, scope_type: ScopeType, scope_id: str | None
    ) -> EffectivePolicies:
        """Policies in force with usage (``GET /policies/effective``)."""
        ...

    async def simulate_authorization(
        self, req: SimulateAuthorizationRequest, *, idempotency_key: str | None
    ) -> CardTransaction:
        """Simulate a charge (``POST /simulation/card-transactions/authorization``)."""
        ...

    async def simulate_clearing(self, req: SimulateClearingRequest) -> CardTransaction:
        """Simulate settlement (``POST /simulation/card-transactions/clearing``)."""
        ...

    async def get_card_transaction(self, transaction_id: str) -> CardTransaction:
        """Fetch a transaction (``GET /card-transactions/{id}``)."""
        ...

    async def list_activities(
        self, *, card_id: str | None, cursor: str | None, limit: int
    ) -> Page[Activity]:
        """Read the activity feed (``GET /activities/``)."""
        ...

    async def create_webhook(self, req: CreateWebhookRequest) -> WebhookEndpoint:
        """Register a webhook endpoint (``POST /webhooks/``)."""
        ...

    async def aclose(self) -> None:
        """Release the connection pool."""
        ...


class ReapHttpClient:
    """``ReapClient`` over HTTP with Reap's headers, idempotency keys and error shape."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        backend: Backend,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
        retry: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        """Create the client.

        Args:
            base_url: Reap host, such as the Singapore sandbox.
            api_key: Bearer key; never logged.
            backend: Label shown on the dashboard.
            transport: Optional transport, used to reach the in-process mock.
            timeout_s: Per-request timeout.
            retry: How far ``Retry-After`` is honoured; the defaults when None.
            sleep: How the client waits; replaced in tests.
            monotonic: The clock ``poll_checkout`` measures its deadline on.
        """
        self._backend: Backend = backend
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {api_key.get_secret_value()}",
                "Reap-Version": REAP_VERSION,
            },
        )

    @property
    def backend(self) -> Backend:
        """Return which backend this is.

        Returns:
            ``mock`` or ``sandbox``.
        """
        return self._backend

    @property
    def card_spend(self) -> bool:
        """Return whether checkouts are card spends; on Reap they take the sandbox header.

        Returns:
            False.
        """
        return False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Wire | None = None,
        params: dict[str, str | int] | None = None,
        idempotency_key: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonValue:
        """Send a request, resending it under the same key after a ``429``.

        Args:
            method: HTTP method.
            path: Path under the base URL.
            body: JSON body, serialised as Reap expects it.
            params: Query parameters.
            idempotency_key: Sent as ``Idempotency-Key`` when given.
            headers: Further headers, such as ``X-Simulate-Checkout``.

        Returns:
            The decoded JSON body, or None for an empty one.

        Raises:
            ReapError: Reap answered with an error, after any bounded ``429`` retries.
        """
        retries = 0
        while True:
            try:
                return await self._send(
                    method,
                    path,
                    body=body,
                    params=params,
                    idempotency_key=idempotency_key,
                    headers=headers,
                )
            except ReapError as error:
                if not _is_rate_limit(error):
                    raise
                wait = self._retry.wait_for(error, retries, self._retry.rate_limit_retries)
                if wait is None:
                    raise
                retries += 1
                await self._sleep(wait)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: Wire | None,
        params: dict[str, str | int] | None,
        idempotency_key: str | None,
        headers: Mapping[str, str] | None,
    ) -> JsonValue:
        """Send one request and map its failure modes.

        Args:
            method: HTTP method.
            path: Path under the base URL.
            body: JSON body.
            params: Query parameters.
            idempotency_key: Sent as ``Idempotency-Key`` when given.
            headers: Further headers.

        Returns:
            The decoded JSON body, or None for an empty one.

        Raises:
            ReapTransportError: The request failed in transport; ``maybe_sent`` says whether
                it may have reached Reap.
            ReapError: Reap answered with a status of 400 or above.
        """
        sent = dict(headers or {})
        if idempotency_key:
            sent["Idempotency-Key"] = idempotency_key
        try:
            response = await self._http.request(
                method,
                path,
                json=body.to_wire() if body is not None else None,
                params=params,
                headers=sent,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise ReapTransportError(f"{method} {path}: {error!r}", maybe_sent=False) from error
        except httpx.HTTPError as error:
            raise ReapTransportError(f"{method} {path}: {error!r}", maybe_sent=True) from error
        if response.status_code >= 400:
            detail: dict[str, Any] | None = None
            try:
                error_body = ErrorResponse.model_validate_json(response.content).error
                code, message, detail = error_body.code, error_body.message, error_body.detail
            except ValueError:
                code, message = "UNPARSEABLE_ERROR", response.text[:200]
            raise ReapError(
                response.status_code,
                code,
                message,
                detail=detail,
                retry_after_s=_retry_after(response),
            )
        return response.json() if response.content else None

    async def _model[M: BaseModel](
        self,
        model: type[M],
        method: str,
        path: str,
        *,
        body: Wire | None = None,
        idempotency_key: str | None = None,
        params: dict[str, str | int] | None = None,
    ) -> M:
        raw = await self._request(
            method, path, body=body, params=params, idempotency_key=idempotency_key
        )
        return model.model_validate(raw)

    async def create_user(self, req: CreateUserRequest) -> User:
        """Create a cardholder.

        Args:
            req: The request.

        Returns:
            The user, with KYC not started.
        """
        return await self._model(User, "POST", "/users/", body=req)

    async def simulate_user_application(self, user_id: str, status: str) -> None:
        """Set a user's KYC status (sandbox only).

        Args:
            user_id: The user.
            status: ``APPROVED``, ``REJECTED``, ``RETRY_REQUIRED`` or ``IN_REVIEW``.
        """
        body = SimulateApplicationRequest.model_validate({"status": status})
        await self._request("POST", f"/simulation/users/{user_id}/application", body=body)

    async def create_account(self, req: CreateAccountRequest, *, idempotency_key: str) -> Account:
        """Open an account.

        Args:
            req: The request.
            idempotency_key: Required by Reap for chargeable resources.

        Returns:
            The account.
        """
        return await self._model(
            Account, "POST", "/accounts/", body=req, idempotency_key=idempotency_key
        )

    async def get_balance(self, account_id: str) -> Balance:
        """Read a balance.

        Args:
            account_id: The account.

        Returns:
            The balance.
        """
        return await self._model(Balance, "GET", f"/accounts/{account_id}/balance")

    async def simulate_fiat_deposit(self, req: SimulateFiatDepositRequest) -> FiatDeposit:
        """Fund a Program-Funded project (sandbox only).

        Args:
            req: The request.

        Returns:
            The settled deposit.
        """
        return await self._model(FiatDeposit, "POST", "/simulation/fiat-deposits", body=req)

    async def create_card(self, req: CreateCardRequest, *, idempotency_key: str) -> Card:
        """Issue a card.

        Args:
            req: The request.
            idempotency_key: Required by Reap.

        Returns:
            The card.
        """
        return await self._model(Card, "POST", "/cards/", body=req, idempotency_key=idempotency_key)

    async def get_card(self, card_id: str) -> Card:
        """Fetch a card.

        Args:
            card_id: The card.

        Returns:
            The card.
        """
        return await self._model(Card, "GET", f"/cards/{card_id}")

    async def freeze_card(self, card_id: str) -> Card:
        """Freeze a card: the soft stop.

        Args:
            card_id: The card.

        Returns:
            The frozen card.
        """
        return await self._model(Card, "POST", f"/cards/{card_id}/freeze")

    async def unfreeze_card(self, card_id: str) -> Card:
        """Unfreeze a card.

        Args:
            card_id: The card.

        Returns:
            The active card.
        """
        return await self._model(Card, "POST", f"/cards/{card_id}/unfreeze")

    async def delete_card(self, card_id: str) -> None:
        """Delete a card irreversibly.

        Args:
            card_id: The card.
        """
        await self._request("DELETE", f"/cards/{card_id}")

    async def create_policy(self, req: PolicyCreate, *, idempotency_key: str | None) -> Policy:
        """Attach a spend policy.

        Args:
            req: The request.
            idempotency_key: Optional for policies.

        Returns:
            The stored policy.
        """
        raw = await self._request("POST", "/policies/", body=req, idempotency_key=idempotency_key)
        return _POLICY.validate_python(raw)

    async def disable_policy(self, policy_id: str) -> Policy:
        """Disable a policy.

        Args:
            policy_id: The policy.

        Returns:
            The disabled policy.
        """
        return _POLICY.validate_python(
            await self._request("POST", f"/policies/{policy_id}/disable")
        )

    async def effective_policies(
        self, scope_type: ScopeType, scope_id: str | None
    ) -> EffectivePolicies:
        """Read the policies in force for a scope, with limit usage.

        Args:
            scope_type: PROJECT, USER or CARD.
            scope_id: The user or card, for those scopes.

        Returns:
            The effective policies.
        """
        params: dict[str, str | int] = {"scopeType": scope_type.value}
        if scope_id is not None:
            params["scopeId"] = scope_id
        return await self._model(EffectivePolicies, "GET", "/policies/effective", params=params)

    async def simulate_authorization(
        self, req: SimulateAuthorizationRequest, *, idempotency_key: str | None
    ) -> CardTransaction:
        """Simulate a merchant charge (sandbox only).

        The idempotency key is not documented for simulation endpoints; it is sent
        anyway, is harmless if ignored, and the mock honours it.

        Args:
            req: The request.
            idempotency_key: The purchase's key from the gate.

        Returns:
            The pending or declined transaction.
        """
        raw = await self._request(
            "POST",
            "/simulation/card-transactions/authorization",
            body=req,
            idempotency_key=idempotency_key,
        )
        return _TRANSACTION.validate_python(raw)

    async def simulate_clearing(self, req: SimulateClearingRequest) -> CardTransaction:
        """Simulate settlement of an authorisation (sandbox only).

        Args:
            req: The request.

        Returns:
            The cleared transaction.
        """
        raw = await self._request("POST", "/simulation/card-transactions/clearing", body=req)
        return _TRANSACTION.validate_python(raw)

    async def get_card_transaction(self, transaction_id: str) -> CardTransaction:
        """Fetch a transaction.

        Args:
            transaction_id: The transaction.

        Returns:
            The transaction in its current status.
        """
        raw = await self._request("GET", f"/card-transactions/{transaction_id}")
        return _TRANSACTION.validate_python(raw)

    async def list_activities(
        self, *, card_id: str | None, cursor: str | None, limit: int
    ) -> Page[Activity]:
        """Read the activity feed.

        Args:
            card_id: Only this card, when given.
            cursor: Cursor from the previous page.
            limit: Page size, 1 to 100.

        Returns:
            One page of activities.
        """
        params: dict[str, str | int] = {"limit": limit}
        if card_id is not None:
            params["cardId"] = card_id
        if cursor is not None:
            params["cursor"] = cursor
        return _ACTIVITIES.validate_python(
            await self._request("GET", "/activities/", params=params)
        )

    async def create_webhook(self, req: CreateWebhookRequest) -> WebhookEndpoint:
        """Register a webhook endpoint; the signing secret is returned once.

        Args:
            req: The request.

        Returns:
            The endpoint.
        """
        return await self._model(WebhookEndpoint, "POST", "/webhooks/", body=req)

    async def _agentic[T](
        self,
        adapter: TypeAdapter[T],
        method: str,
        path: str,
        *,
        body: Wire | None = None,
        params: dict[str, str | int] | None = None,
        idempotency_key: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> T:
        """Send an agentic request and read its answer, or say the outcome is unknown.

        Args:
            adapter: Reads the response body.
            method: HTTP method.
            path: Path under the base URL.
            body: JSON body.
            params: Query parameters.
            idempotency_key: Sent as ``Idempotency-Key`` when given.
            headers: Further headers.

        Returns:
            The parsed response.

        Raises:
            ReapResponseError: The 2xx body did not match the documented shape.
        """
        raw = await self._request(
            method,
            path,
            body=body,
            params=params,
            idempotency_key=idempotency_key,
            headers=headers,
        )
        try:
            return adapter.validate_python(raw)
        except ValidationError as error:
            raise ReapResponseError(f"{method} {path}: unreadable response: {error}") from error

    async def create_enrollment(
        self, req: CreateEnrollmentRequest, *, idempotency_key: str
    ) -> EnrollmentCreated:
        """Store a card for agentic purchases; an external card returns a hosted redirect.

        Args:
            req: The request for one card source.
            idempotency_key: Required by Reap; a replay returns the same hosted URL.

        Returns:
            The enrollment, shaped by its source.
        """
        return await self._agentic(
            _ENROLLMENT_CREATED,
            "POST",
            "/agentic/enrollments",
            body=req,
            idempotency_key=_check_key(idempotency_key),
        )

    async def get_enrollment(self, enrollment_id: str) -> Enrollment:
        """Read an enrollment; only an ``ACTIVE`` one can be charged.

        Args:
            enrollment_id: The enrollment.

        Returns:
            The enrollment with its stored card, if any.
        """
        return await self._agentic(
            _ENROLLMENT, "GET", f"/agentic/enrollments/{_segment(enrollment_id)}"
        )

    async def list_enrollments(
        self,
        owner_id: str,
        *,
        owner_type: Literal["REAP_USER", "CLIENT_REFERENCE"] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[Enrollment]:
        """List one owner's enrollments.

        Args:
            owner_id: The owner; a list is always scoped to exactly one.
            owner_type: Reap defaults to ``CLIENT_REFERENCE`` when it is left out.
            limit: Page size, 1 to 100; Reap defaults to 20.
            cursor: ``nextCursor`` from the previous page.

        Returns:
            One page of enrollments.
        """
        params: dict[str, str | int] = {"ownerId": owner_id}
        if owner_type is not None:
            params["ownerType"] = owner_type
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._agentic(_ENROLLMENTS, "GET", "/agentic/enrollments", params=params)

    async def revoke_enrollment(self, enrollment_id: str) -> Enrollment:
        """Revoke an enrollment; Reap says "Revoking is final".

        Args:
            enrollment_id: The enrollment.

        Returns:
            The enrollment, ``REVOKED``.
        """
        return await self._agentic(
            _ENROLLMENT,
            "POST",
            f"/agentic/enrollments/{_segment(enrollment_id)}/revoke",
        )

    async def search_products(self, req: ProductSearchRequest) -> ProductSearchResponse:
        """Search merchant catalogues with free text.

        Args:
            req: The query, with optional merchant preference, context, filters and paging.

        Returns:
            One page of products, each with a price range and a preview variant.
        """
        return await self._agentic(_SEARCH, "POST", "/agentic/products/search", body=req)

    async def product_details(self, req: ProductDetailsRequest) -> ProductDetailsResponse:
        """Expand products into their options and default variants.

        Args:
            req: 1 to 10 product ids.

        Returns:
            The products, and per-product errors for ids that did not resolve.
        """
        return await self._agentic(_DETAILS, "POST", "/agentic/products/details", body=req)

    async def resolve_variant(self, req: ResolveVariantRequest) -> Variant:
        """Resolve chosen option ids to the variant a quote accepts.

        Args:
            req: The product and its chosen option ids.

        Returns:
            The variant.
        """
        return await self._agentic(_VARIANT, "POST", "/agentic/products/variant", body=req)

    async def create_quote(
        self,
        req: CreateQuoteRequest,
        *,
        idempotency_key: str,
        retry_key: Callable[[], str] | None = None,
    ) -> Quote:
        """Price a merchant checkout from variants or a checkout URL.

        ``QUOTE_TEMPORARILY_UNAVAILABLE`` is cached under the key it answered, so a retry is a
        new attempt under a new key. With ``retry_key`` the client waits ``Retry-After`` and
        tries again, up to ``RetryPolicy.quote_retries`` times; without it, the error surfaces
        at once with ``retry_after_s`` set.

        Args:
            req: The quote request, items or external checkout.
            idempotency_key: The first attempt's key; the caller persists it before calling.
            retry_key: Mints the key for each further attempt. Called once per retry, just
                before that retry is sent, so the caller can persist it; every call must
                return a key never used before.

        Returns:
            The quote with its shipping options and amount breakdown.

        Raises:
            ReapError: Reap refused the quote, or was still unavailable after the retries.
        """
        key = _check_key(idempotency_key)
        retries = 0
        while True:
            try:
                return await self._agentic(
                    _QUOTE, "POST", "/agentic/quotes", body=req, idempotency_key=key
                )
            except ReapError as error:
                if error.code != AgenticErrorCode.QUOTE_TEMPORARILY_UNAVAILABLE:
                    raise
                if retry_key is None:
                    raise
                wait = self._retry.wait_for(error, retries, self._retry.quote_retries)
                if wait is None:
                    raise
                retries += 1
                await self._sleep(wait)
                key = _check_key(retry_key())

    async def get_quote(self, quote_id: str) -> Quote:
        """Read a quote's current total without changing it.

        Args:
            quote_id: The quote.

        Returns:
            The quote.
        """
        return await self._agentic(_QUOTE, "GET", f"/agentic/quotes/{_segment(quote_id)}")

    async def select_shipping_option(
        self, quote_id: str, req: SelectShippingOptionRequest, *, idempotency_key: str | None
    ) -> Quote:
        """Choose a shipping option and re-price the quote.

        Args:
            quote_id: The quote.
            req: The shipping option.
            idempotency_key: Not required by Reap, but honoured when sent.

        Returns:
            The quote with a fresh amount breakdown.
        """
        return await self._agentic(
            _QUOTE,
            "POST",
            f"/agentic/quotes/{_segment(quote_id)}/shipping-option",
            body=req,
            idempotency_key=_check_key(idempotency_key) if idempotency_key is not None else None,
        )

    async def create_checkout(
        self,
        req: CreateCheckoutRequest,
        *,
        idempotency_key: str,
        simulate: SimulateCheckout | None = None,
    ) -> CheckoutCreated:
        """Open the payment for an unexpired quote against an ``ACTIVE`` enrollment.

        ``CHECKOUT_TEMPORARILY_UNAVAILABLE`` is not retried here (see the module docstring).

        Args:
            req: The quote, the enrollment and the return URL.
            idempotency_key: Required by Reap; the gate's claim key, one per claim.
            simulate: ``COMPLETED`` sends ``X-Simulate-Checkout``, sandbox only ("This header
                is rejected in production").

        Returns:
            The checkout as created; read it again with ``get_checkout`` for the order id.
        """
        headers = {SIMULATE_CHECKOUT_HEADER: simulate} if simulate is not None else None
        return await self._agentic(
            _CHECKOUT_CREATED,
            "POST",
            "/agentic/checkouts",
            body=req,
            idempotency_key=_check_key(idempotency_key),
            headers=headers,
        )

    async def get_checkout(self, checkout_id: str) -> Checkout:
        """Read a checkout; a completed one carries ``orderId`` and ``finalAmount``.

        Args:
            checkout_id: The checkout.

        Returns:
            The checkout in its current status.
        """
        return await self._agentic(_CHECKOUT, "GET", f"/agentic/checkouts/{_segment(checkout_id)}")

    async def poll_checkout(
        self,
        checkout_id: str,
        *,
        every_s: float = 1.0,
        deadline_s: float = 120.0,
        max_every_s: float = 5.0,
    ) -> Checkout:
        """Read a checkout until it reaches one of four exits or the deadline passes.

        It reads at once, then after ``every_s``, doubling the wait up to ``max_every_s``
        and never sleeping past the deadline, where it reads one last time. A dropped read
        or a transient code (``AGENTIC_SERVICE_UNAVAILABLE``, a ``429`` beyond the client's
        own retries) is ridden out until the deadline; any other error surfaces.

        Args:
            checkout_id: The checkout.
            every_s: The first wait between reads.
            deadline_s: How long to poll before giving up.
            max_every_s: The longest wait between reads.

        Returns:
            The checkout once it is ``COMPLETED``, ``FAILED``, ``EXPIRED`` or
            ``REQUIRES_ACTION``.

        Raises:
            ValueError: If the timing makes no sense.
            CheckoutPollTimeoutError: Still ``PROCESSING``, or never read, at the deadline.
            ReapError: Reap answered with a definite error, such as ``CHECKOUT_NOT_FOUND``.
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


class ReapSandbox(ReapHttpClient):
    """Reap's real sandbox.

    Paths, headers and payloads follow Reap's OpenAPI (API version 2025-02-14); every
    line of wire code is exercised against the mock in the tests, not against Reap.
    """

    def __init__(self, *, api_key: SecretStr, base_url: str = SG_SANDBOX_URL) -> None:
        """Create a sandbox client.

        Args:
            api_key: The sandbox key from ``.env``.
            base_url: The regional sandbox host.
        """
        super().__init__(base_url=base_url, api_key=api_key, backend="sandbox")


def httpx_delivery(client: httpx.AsyncClient) -> WebhookDelivery:
    """Deliver mock webhooks with an httpx client (real HTTP, or an ASGI transport).

    Args:
        client: The client used to POST each delivery.

    Returns:
        A delivery function for the mock engine.
    """

    async def deliver(url: str, headers: dict[str, str], body: bytes) -> DeliveryResponse:
        response = await client.post(url, content=body, headers=headers)
        return DeliveryResponse(status=response.status_code, body=response.content)

    return deliver


class ReapMock(ReapHttpClient):
    """The default backend: the real HTTP client talking to the in-process mock."""

    def __init__(
        self,
        *,
        clock: Clock = utc_now,
        authorization_mode: AuthorizationMode = AuthorizationMode.MANAGED,
        delivery: WebhookDelivery | None = None,
        agentic: AgenticMockEngine | None = None,
    ) -> None:
        """Create the mock backend.

        Args:
            clock: Time source for the mock's timestamps and windows.
            authorization_mode: Managed, or External with a real-time authoriser.
            delivery: How the mock delivers webhooks; settable later on ``engine``.
            agentic: The agentic mock to serve; one over every bundled catalogue if None.
        """
        self.engine = MockReapEngine(
            clock=clock, authorization_mode=authorization_mode, delivery=delivery
        )
        self.agentic = agentic or AgenticMockEngine(clock=clock)
        super().__init__(
            base_url=MOCK_BASE_URL,
            api_key=SecretStr("mock-key"),
            backend="mock",
            transport=httpx.ASGITransport(
                app=create_mock_app(self.engine, clock, agentic=self.agentic)
            ),
        )

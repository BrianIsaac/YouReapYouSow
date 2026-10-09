"""The Kwal participant gateway, in memory, over the agentic mock's catalogue and quotes.

Kwal fronts Reap: its participant is "a new Reap sandbox participant", its catalogue and
quotes are Reap's, and its payment is a spend on a Reap-issued sandbox card that Kwal
approves against the participant's vault (the skill's ``references/checkout.md``). So
this mock keeps Kwal's own state (the setup, the vault's card-spendable USDC, the
payments) and asks the agentic mock (``reap/mock/agentic.py``) for products, variants
and quotes, translating each into the participant contract's shapes
(``kwal/models.py``). ``create_kwal_mock_app`` serves it on the skill's routes, so
``KwalClient`` runs every line of its wire code against it.

Documented by the skill (``payward/kwal-skill`` at ``be5f52c``), implemented as written:

- A sandbox payment is a simulated card spend: saved, then authorised (step
  ``card_authorization``, the amount held on the vault), then cleared (step
  ``card_clearing``, ``charged`` in USDC, a ``cardTransactionId``, no order id)
  (``references/checkout.md:41-58``). USD totals are funded at 1 USD = 1 USDC
  (``references/funding.md:10``).
- The caller's ``paymentId`` replays its payment and never pays twice
  (``references/checkout.md:14``, ``:56``); a vault that cannot cover the quote is
  ``400 ParticipantFundingRequest`` and a card another payment holds ``409
  ParticipantCardBusy`` naming the holder, and neither saves anything
  (``scripts/checkout.py:55-61``; ``references/checkout.md:48``, ``:54``); a quote
  another payment holds is that payment's, and a second payment on it is recorded as an
  error at ``quote_already_used`` (``scripts/checkout.py:44-53``).
- Errors are ``{"type": "tag:kraken.com,2025:<Tag>"}`` with ``Retry-After`` where given
  (``scripts/transport.py:39-52``, ``:77-82``); a token other than the participant's is
  ``401 ParticipantUnauthenticated``.
- The local quote mode, when configured: ``sandbox_quote_`` ids, "fixed demo pricing of 1
  USDC per item", no shipping, 15 minutes (``references/quotes.md:5-10``).
- The hosted approval mode, when configured: the payment waits on an https approval link
  and completes with an order id once approved (``scripts/checkout.py:126-133``).

Assumed, by name, where the skill is silent:

- The payment worker runs between the create and the next read: the first read after
  ``KwalMockConfig.reads_to_clear`` reads the payment cleared.
- The participant starts set up and funded (``KwalMockConfig.funded_usdc``, 500 USDC),
  as the Reap mock enrols its own published test card; tests change it with
  ``set_setup`` and ``fund``. Its vault address is a placeholder, never a real one.
- A product's price in a search is its cheapest variant's; option ids are the agentic
  mock's; an empty option list resolves the default variant.
- An agentic mock error maps to ``ParticipantNotFound`` (404), ``ParticipantUnavailable``
  (503) or ``ParticipantBadRequest`` (any other).
"""

import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from youreapyousow.clock import Clock, utc_now
from youreapyousow.kwal.models import (
    ERROR_TYPE_PREFIX,
    QUOTE_ALREADY_USED,
    USDC,
    USDC_DECIMALS,
    FundingState,
    KwalAmount,
    KwalFunding,
    KwalOption,
    KwalOptionValue,
    KwalPayment,
    KwalProduct,
    KwalProductDetail,
    KwalProducts,
    KwalQuote,
    KwalSetup,
    KwalShippingOption,
    KwalVariant,
    PaymentRequest,
    PaymentState,
    QuoteRequest,
    SetupState,
    ShippingRequest,
    VariantRequest,
)
from youreapyousow.reap.mock.agentic import AgenticMockEngine, AgenticMockError
from youreapyousow.reap.models import (
    CreateItemsQuoteRequest,
    Money,
    ProductDetailsRequest,
    ProductSearchRequest,
    Quote,
    QuoteItem,
    ResolveVariantRequest,
    SearchPagination,
    SelectShippingOptionRequest,
    ShippingAddress,
)

MOCK_BASE_URL = "https://kwal-mock.local"
MOCK_TOKEN = "kwal-mock-token"
PLACEHOLDER_VAULT = "0x" + "ab" * 20
"""The mock participant's vault: a placeholder, never a real address."""

PREFIX = "/kwal/participant/v1"
_LOCAL_QUOTE_LIFE = timedelta(minutes=15)
_CENTS = 2


class Route(StrEnum):
    """The participant routes the mock serves, for fault injection."""

    STATUS = "status"
    FUNDING = "funding"
    SEARCH = "search"
    PRODUCT = "product"
    VARIANT = "variant"
    CREATE_QUOTE = "create_quote"
    GET_QUOTE = "get_quote"
    SHIPPING = "shipping"
    CREATE_PAYMENT = "create_payment"
    GET_PAYMENT = "get_payment"


class KwalMockError(Exception):
    """An error the mock answers in the participant contract's error shape."""

    def __init__(
        self,
        status: int,
        tag: str,
        title: str | None = None,
        *,
        data: dict[str, Any] | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        """Create the error.

        Args:
            status: HTTP status.
            tag: The participant error tag, such as ``ParticipantCardBusy``.
            title: A human title; the tag when None.
            data: The ``data`` object, if any.
            retry_after_s: Sent as ``Retry-After`` when set.
        """
        self.status = status
        self.tag = tag
        self.title = title or tag
        self.data = data
        self.retry_after_s = retry_after_s
        super().__init__(f"{status} {tag}: {self.title}")

    def response(self) -> Response:
        """Answer the error as the gateway does.

        Returns:
            The JSON error response.
        """
        body: dict[str, Any] = {"type": f"{ERROR_TYPE_PREFIX}{self.tag}", "title": self.title}
        if self.data is not None:
            body["data"] = self.data
        headers = (
            {"Retry-After": f"{self.retry_after_s:g}"} if self.retry_after_s is not None else None
        )
        return JSONResponse(body, status_code=self.status, headers=headers)


class DroppedResponseError(httpx.ReadError):
    """A response the mock lost on purpose after the route ran (a test fault)."""


@dataclass(frozen=True)
class KwalMockConfig:
    """The mock's named assumptions and modes.

    Attributes:
        funded_usdc: What the vault can spend at the start.
        local_quotes: Answer quotes in the skill's local quote mode.
        hosted_approval: Make payments wait on an approval link instead of a card spend.
        reads_to_clear: Reads after the create at which a card spend reads cleared.
    """

    funded_usdc: Decimal = Decimal(500)
    local_quotes: bool = False
    hosted_approval: bool = False
    reads_to_clear: int = 1


@dataclass
class _Payment:
    payment: KwalPayment
    required: Decimal
    reads: int = 0


@dataclass
class _Faults:
    errors: dict[Route, deque[KwalMockError]] = field(
        default_factory=dict[Route, deque[KwalMockError]]
    )
    drops: dict[Route, int] = field(default_factory=dict[Route, int])


def _usd(money: Money) -> KwalAmount:
    return KwalAmount.of(money.amount, money.currency, _CENTS)


def _usdc(amount: Decimal) -> KwalAmount:
    return KwalAmount.of(amount, USDC, USDC_DECIMALS)


def _from_agentic(error: AgenticMockError) -> KwalMockError:
    if error.status == 404:
        tag = "ParticipantNotFound"
    elif error.status == 503:
        tag = "ParticipantUnavailable"
    else:
        tag = "ParticipantBadRequest"
    status = error.status if error.status in (404, 503) else 400
    return KwalMockError(status, tag, error.message, retry_after_s=error.retry_after_s)


class KwalMockEngine:
    """The participant gateway's behaviour, without HTTP."""

    def __init__(
        self,
        agentic: AgenticMockEngine | None = None,
        *,
        clock: Clock = utc_now,
        token: str = MOCK_TOKEN,
        config: KwalMockConfig | None = None,
    ) -> None:
        """Create a participant that is set up and funded.

        Args:
            agentic: The Reap agentic mock behind the gateway; every bundled catalogue
                when None.
            clock: Time source.
            token: The participant's bearer token.
            config: The mock's modes; the defaults when None.
        """
        self.agentic = agentic or AgenticMockEngine(clock=clock)
        self.token = token
        self.config = config or KwalMockConfig()
        self._clock = clock
        self.setup = KwalSetup(
            state=SetupState.READY,
            vault_address=PLACEHOLDER_VAULT,
            chain="ink-sepolia",
            card_status="ACTIVE",
            deposit_observed=True,
        )
        self._available = self.config.funded_usdc
        self._payments: dict[str, _Payment] = {}
        self._quotes: dict[str, KwalQuote] = {}
        self._held_by: dict[str, str] = {}
        self._orders = 0
        self._faults = _Faults()

    # Faults and state, for tests

    def inject(
        self,
        route: Route,
        status: int,
        tag: str,
        *,
        data: dict[str, Any] | None = None,
        retry_after_s: float | None = None,
        times: int = 1,
    ) -> None:
        """Answer the next calls of a route with an error, before the route runs.

        Args:
            route: The route.
            status: HTTP status.
            tag: The participant error tag.
            data: The ``data`` object, if any.
            retry_after_s: Sent as ``Retry-After`` when set.
            times: How many consecutive calls answer with it.
        """
        error = KwalMockError(status, tag, data=data, retry_after_s=retry_after_s)
        self._faults.errors.setdefault(route, deque()).extend(error for _ in range(times))

    def take_error(self, route: Route) -> KwalMockError | None:
        """Take the next injected error for a route, if any.

        Args:
            route: The route.

        Returns:
            The error, or None.
        """
        queue = self._faults.errors.get(route)
        return queue.popleft() if queue else None

    def drop_next(self, route: Route, *, times: int = 1) -> None:
        """Lose the next responses of a route after it ran.

        Args:
            route: The route.
            times: How many.
        """
        self._faults.drops[route] = self._faults.drops.get(route, 0) + times

    def take_drop(self, route: Route) -> bool:
        """Whether to lose this response.

        Args:
            route: The route.

        Returns:
            True once for each drop asked for.
        """
        left = self._faults.drops.get(route, 0)
        if left:
            self._faults.drops[route] = left - 1
        return bool(left)

    def set_setup(self, setup: KwalSetup) -> None:
        """Put the participant's setup in another state.

        Args:
            setup: The setup as the gateway will report it.
        """
        self.setup = setup

    def fund(self, usdc: Decimal) -> None:
        """Set what the card can spend from the vault.

        Args:
            usdc: The card-spendable USDC.
        """
        self._available = usdc

    def approve(self, payment_id: str) -> KwalPayment:
        """Approve a payment waiting on its link, as the person would on the hosted page.

        Args:
            payment_id: The payment.

        Returns:
            The payment, completed with its order id.

        Raises:
            KwalMockError: If it is not waiting for approval.
        """
        stored = self._stored(payment_id)
        if stored.payment.state != PaymentState.REQUIRES_ACTION:
            raise KwalMockError(400, "ParticipantBadRequest", "the payment is not waiting")
        self._orders += 1
        stored.payment = stored.payment.model_copy(
            update={
                "state": PaymentState.COMPLETED,
                "step": None,
                "approval_url": None,
                "approval_expires_at_unix_seconds": None,
                "order_id": f"KWAL-ORDER-{self._orders:06d}",
                "charged": _usdc(stored.required),
                "checkout_status": "PARTICIPANT_CHECKOUT_STATUS_COMPLETED",
            }
        )
        return stored.payment

    # Setup and funding

    def status(self) -> KwalSetup:
        """``GET /status``.

        Returns:
            The participant's setup.
        """
        return self.setup

    def funding(self, *, required_minor_units: int | None = None) -> KwalFunding:
        """``GET /funding``: what the card can spend, against an amount if asked.

        Args:
            required_minor_units: The USDC minor units to check against, if any.

        Returns:
            The funding readiness.
        """
        if self.setup.state != SetupState.READY:
            return KwalFunding(state=FundingState.SETUP_NEEDED)
        available = _usdc(self._available)
        required = Decimal(required_minor_units or 0).scaleb(-USDC_DECIMALS)
        gap = max(required - self._available, Decimal(0))
        enough = self._available >= required and (required > 0 or self._available > 0)
        return KwalFunding(
            state=FundingState.READY if enough else FundingState.FUNDS_NEEDED,
            vault_address=self.setup.vault_address,
            chain=self.setup.chain,
            available=available,
            required=_usdc(required) if required_minor_units is not None else None,
            shortfall=_usdc(gap) if gap else None,
        )

    # Products

    def search(self, query: str, *, limit: int | None = None) -> KwalProducts:
        """``GET /products?query=&limit=``.

        Args:
            query: The search words.
            limit: The most products to return.

        Returns:
            The products, each with its merchant and its cheapest price.
        """
        request = ProductSearchRequest(
            query=query, pagination=SearchPagination(limit=limit) if limit else None
        )
        try:
            found = self.agentic.search_products(request)
        except AgenticMockError as error:
            raise _from_agentic(error) from error
        return KwalProducts(
            products=[
                KwalProduct(
                    product_id=p.id,
                    title=p.name,
                    merchant=p.merchant.name,
                    price=_usd(p.price_range.min),
                )
                for p in found.products
            ]
        )

    def product(self, product_id: str) -> KwalProductDetail:
        """``GET /products/{id}``.

        Args:
            product_id: The product.

        Returns:
            The product and its options.

        Raises:
            KwalMockError: ``ParticipantNotFound`` for an unknown product.
        """
        details = self.agentic.product_details(ProductDetailsRequest(product_ids=[product_id]))
        if not details.products:
            raise KwalMockError(404, "ParticipantNotFound", f"no product {product_id}")
        detail = details.products[0]
        return KwalProductDetail(
            product=KwalProduct(
                product_id=detail.id,
                title=detail.name,
                merchant=detail.merchant.name if detail.merchant else None,
                price=_usd(detail.default_variant.price),
            ),
            options=[
                KwalOption(
                    name=o.name,
                    values=[
                        KwalOptionValue(option_id=v.option_id, label=v.label) for v in o.values
                    ],
                )
                for o in detail.options
            ],
        )

    def variant(self, product_id: str, req: VariantRequest) -> KwalVariant:
        """``POST /products/{id}/variant``.

        Args:
            product_id: The product.
            req: One option id for every option, or none for the default variant.

        Returns:
            The variant, purchasable when available.
        """
        try:
            if req.option_ids:
                resolved = self.agentic.resolve_variant(
                    ResolveVariantRequest(product_id=product_id, option_ids=req.option_ids)
                )
            else:
                details = self.agentic.product_details(
                    ProductDetailsRequest(product_ids=[product_id])
                )
                if not details.products:
                    raise KwalMockError(404, "ParticipantNotFound", f"no product {product_id}")
                resolved = details.products[0].default_variant
        except AgenticMockError as error:
            raise _from_agentic(error) from error
        return KwalVariant(
            variant_id=resolved.id,
            title=resolved.name,
            price=_usd(resolved.price),
            purchasable=resolved.available is not False,
        )

    # Quotes

    def create_quote(self, req: QuoteRequest) -> KwalQuote:
        """``POST /quotes``.

        Args:
            req: The email, the lines and the address.

        Returns:
            The quote.
        """
        if self.config.local_quotes:
            return self._local_quote(req)
        address = req.shipping_address
        try:
            quote = self.agentic.create_quote(
                CreateItemsQuoteRequest(
                    email=req.email,
                    items=[
                        QuoteItem(variant_id=line.variant_id, quantity=line.quantity)
                        for line in req.lines
                    ],
                    shipping_address=ShippingAddress(
                        first_name=address.first_name,
                        last_name=address.last_name,
                        phone=address.phone,
                        address_line1=address.line1,
                        address_line2=address.line2,
                        city=address.city,
                        region=address.region,
                        postal_code=address.postal_code,
                        country=address.country,
                    )
                    if address is not None
                    else None,
                )
            )
        except (AgenticMockError, ValidationError) as error:
            if isinstance(error, AgenticMockError):
                raise _from_agentic(error) from error
            raise KwalMockError(400, "ParticipantBadRequest", "invalid quote request") from error
        return self._keep(self._from_reap(quote, req))

    def _local_quote(self, req: QuoteRequest) -> KwalQuote:
        for line in req.lines:
            if self.agentic.catalogue.find_variant(line.variant_id) is None:
                raise KwalMockError(400, "ParticipantBadRequest", "unknown variant")
        items = sum(line.quantity for line in req.lines)
        expires = self._clock() + _LOCAL_QUOTE_LIFE
        return self._keep(
            KwalQuote(
                quote_id=f"sandbox_quote_{uuid.uuid4().hex[:12]}",
                lines=req.lines,
                total=KwalAmount(minor_units=str(items), currency="USD"),
                expires_at_unix_seconds=int(expires.timestamp()),
            )
        )

    def _from_reap(self, quote: Quote, req: QuoteRequest) -> KwalQuote:
        breakdown = quote.amount_breakdown
        selected = next((o.id for o in quote.shipping_options if o.selected), None)
        return KwalQuote(
            quote_id=quote.id,
            lines=req.lines,
            shipping_options=[
                KwalShippingOption(shipping_option_id=o.id, label=o.name, price=_usd(o.price))
                for o in quote.shipping_options
            ],
            selected_shipping_option_id=selected,
            subtotal=_usd(breakdown.items_subtotal),
            shipping=_usd(breakdown.shipping) if breakdown.shipping else None,
            tax=_usd(breakdown.tax.amount) if breakdown.tax else None,
            total=_usd(breakdown.final_amount),
            expires_at_unix_seconds=int(datetime.fromisoformat(quote.expires_at).timestamp()),
        )

    def _keep(self, quote: KwalQuote) -> KwalQuote:
        self._quotes[quote.quote_id] = quote
        return quote

    def get_quote(self, quote_id: str) -> KwalQuote:
        """``GET /quotes/{id}``.

        Args:
            quote_id: The quote.

        Returns:
            The quote, naming the payment that holds it, if any.

        Raises:
            KwalMockError: ``ParticipantNotFound`` for an unknown quote.
        """
        quote = self._quotes.get(quote_id)
        if quote is None:
            raise KwalMockError(404, "ParticipantNotFound", f"no quote {quote_id}")
        return quote.model_copy(update={"payment_id": self._held_by.get(quote_id)})

    def select_shipping(self, quote_id: str, req: ShippingRequest) -> KwalQuote:
        """``POST /quotes/{id}/shipping``: the quote, re-priced.

        Args:
            quote_id: The quote.
            req: The shipping option.

        Returns:
            The quote with its new total.
        """
        current = self.get_quote(quote_id)
        try:
            quote = self.agentic.select_shipping_option(
                quote_id, SelectShippingOptionRequest(shipping_option_id=req.shipping_option_id)
            )
        except AgenticMockError as error:
            raise _from_agentic(error) from error
        return self._keep(
            self._from_reap(quote, QuoteRequest(email="kept@example.com", lines=current.lines))
        )

    # Payments

    def create_payment(self, req: PaymentRequest) -> KwalPayment:
        """``POST /payments``: save the payment, then spend on the card against the vault.

        Args:
            req: The caller's payment id and the quote.

        Returns:
            The payment as saved.

        Raises:
            KwalMockError: The id names another quote (``ParticipantBadRequest``); the
                quote is unknown or expired; the card is busy; the vault cannot cover it.
        """
        existing = self._payments.get(req.payment_id)
        if existing is not None:
            if existing.payment.quote_id != req.quote_id:
                raise KwalMockError(400, "ParticipantBadRequest", "payment id reused")
            return existing.payment
        quote = self.get_quote(req.quote_id)
        if self._clock().timestamp() >= quote.expires_at_unix_seconds:
            raise KwalMockError(400, "ParticipantBadRequest", "quote has expired")
        if quote.payment_id is not None:
            used = KwalPayment(
                payment_id=req.payment_id,
                state=PaymentState.ERROR,
                step=QUOTE_ALREADY_USED,
                quote_id=req.quote_id,
                reason="quote is already attached to another payment; create a new quote",
            )
            self._payments[req.payment_id] = _Payment(used, Decimal(0))
            return used
        holder = next(
            (
                p.payment.payment_id
                for p in self._payments.values()
                if p.payment.state in (PaymentState.PENDING, PaymentState.REQUIRES_ACTION)
            ),
            None,
        )
        if holder is not None:
            raise KwalMockError(409, "ParticipantCardBusy", data={"holdingPaymentId": holder})
        required = quote.total.value
        if required > self._available:
            raise KwalMockError(400, "ParticipantFundingRequest", "the vault cannot cover it")
        self._held_by[req.quote_id] = req.payment_id
        if self.config.hosted_approval:
            payment = KwalPayment(
                payment_id=req.payment_id,
                state=PaymentState.REQUIRES_ACTION,
                step="hosted_approval",
                approval_url=f"https://kwal-mock.local/approve/{req.payment_id}",
                approval_expires_at_unix_seconds=int(
                    (self._clock() + timedelta(minutes=15)).timestamp()
                ),
                checkout_id=str(uuid.uuid4()),
                amount=quote.total,
                quote_id=req.quote_id,
            )
        else:
            self._available -= required
            payment = KwalPayment(
                payment_id=req.payment_id,
                state=PaymentState.PENDING,
                step="card_authorization",
                card_transaction_id=str(uuid.uuid4()),
                held=_usdc(required),
                amount=quote.total,
                quote_id=req.quote_id,
                reason="approved; the amount is held on your vault funds",
            )
        self._payments[req.payment_id] = _Payment(payment, required)
        return payment

    def _stored(self, payment_id: str) -> _Payment:
        stored = self._payments.get(payment_id)
        if stored is None:
            raise KwalMockError(404, "ParticipantNotFound", f"no payment {payment_id}")
        return stored

    def get_payment(self, payment_id: str) -> KwalPayment:
        """``GET /payments/{id}``: the card spend clears once the worker has run.

        Args:
            payment_id: The payment.

        Returns:
            The payment as now saved.
        """
        stored = self._stored(payment_id)
        payment = stored.payment
        if payment.state == PaymentState.PENDING and payment.step == "card_authorization":
            stored.reads += 1
            if stored.reads >= self.config.reads_to_clear:
                stored.payment = payment.model_copy(
                    update={
                        "state": PaymentState.COMPLETED,
                        "step": "card_clearing",
                        "held": None,
                        "charged": _usdc(stored.required),
                        "reason": "paid from your vault by a simulated card spend; nothing ships",
                    }
                )
        return stored.payment


type _Action = Callable[[], BaseModel]


def create_kwal_mock_app(engine: KwalMockEngine) -> FastAPI:
    """Serve the engine on the participant routes.

    Args:
        engine: The gateway's behaviour.

    Returns:
        The app.
    """
    app = FastAPI(title="Kwal participant gateway mock")

    async def answer(request: Request, route: Route, action: _Action) -> Response:
        if request.headers.get("authorization") != f"Bearer {engine.token}":
            return KwalMockError(401, "ParticipantUnauthenticated").response()
        injected = engine.take_error(route)
        if injected is not None:
            return injected.response()
        try:
            body = action().model_dump(mode="json", by_alias=True, exclude_none=True)
        except KwalMockError as error:
            return error.response()
        except ValidationError:
            return KwalMockError(400, "ParticipantBadRequest", "invalid request").response()
        if engine.take_drop(route):
            raise DroppedResponseError(f"{route}: response dropped by the mock")
        return JSONResponse(body)

    async def read[M: BaseModel](request: Request, model: type[M]) -> M:
        return model.model_validate_json(await request.body())

    def post[M: BaseModel](
        model: type[M], route: Route, act: Callable[[M], BaseModel]
    ) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            try:
                parsed = await read(request, model)
            except ValidationError:
                return KwalMockError(400, "ParticipantBadRequest", "invalid request").response()
            return await answer(request, route, lambda: act(parsed))

        return handle

    @app.get(f"{PREFIX}/status")
    async def status(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await answer(request, Route.STATUS, engine.status)

    @app.get(f"{PREFIX}/funding")
    async def funding(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        requiredMinorUnits: int | None = None,  # noqa: N803
    ) -> Response:
        return await answer(
            request,
            Route.FUNDING,
            lambda: engine.funding(required_minor_units=requiredMinorUnits),
        )

    @app.get(f"{PREFIX}/products")
    async def search(  # pyright: ignore[reportUnusedFunction]
        request: Request, query: str, limit: int | None = None
    ) -> Response:
        return await answer(request, Route.SEARCH, lambda: engine.search(query, limit=limit))

    @app.get(f"{PREFIX}/products/{{product_id}}")
    async def product(request: Request, product_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await answer(request, Route.PRODUCT, lambda: engine.product(product_id))

    @app.post(f"{PREFIX}/products/{{product_id}}/variant")
    async def variant(request: Request, product_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await post(VariantRequest, Route.VARIANT, lambda r: engine.variant(product_id, r))(
            request
        )

    app.add_api_route(
        f"{PREFIX}/quotes",
        post(QuoteRequest, Route.CREATE_QUOTE, engine.create_quote),
        methods=["POST"],
    )

    @app.get(f"{PREFIX}/quotes/{{quote_id}}")
    async def get_quote(request: Request, quote_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await answer(request, Route.GET_QUOTE, lambda: engine.get_quote(quote_id))

    @app.post(f"{PREFIX}/quotes/{{quote_id}}/shipping")
    async def shipping(request: Request, quote_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await post(
            ShippingRequest, Route.SHIPPING, lambda r: engine.select_shipping(quote_id, r)
        )(request)

    app.add_api_route(
        f"{PREFIX}/payments",
        post(PaymentRequest, Route.CREATE_PAYMENT, engine.create_payment),
        methods=["POST"],
    )

    @app.get(f"{PREFIX}/payments/{{payment_id}}")
    async def get_payment(request: Request, payment_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await answer(request, Route.GET_PAYMENT, lambda: engine.get_payment(payment_id))

    return app

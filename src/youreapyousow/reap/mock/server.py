"""The mock engines served on Reap's documented paths, so the real HTTP client talks to them.

Card paths: headers are enforced as Reap enforces them: ``Authorization: Bearer`` must be
present, ``Reap-Version`` must equal the pinned version, and ``Idempotency-Key`` is required
where the spec requires it. A reused key with the same body replays the stored response for
24 hours; with a different body it is refused with 409 (mock choice: the code
``IDEMPOTENCY_KEY_REUSED`` is not documented).

Agentic paths (``/agentic/...``, the engine in ``reap/mock/agentic.py``) follow
https://docs.reap.global/api-reference/{errors,idempotency,rate-limiting}.md and the
OpenAPI:

- A missing ``Authorization`` is ``401 API_KEY_REQUIRED``, a non-Bearer one ``401
  INVALID_AUTH_HEADER``, another key than the configured one ``401 INVALID_API_KEY``; a
  missing ``Reap-Version`` is ``400 API_VERSION_HEADER_MISSING``, another version ``400
  API_VERSION_INVALID``. No endpoint for the method and path is ``404 ROUTE_NOT_FOUND``; a
  body that is not JSON ``400 PARSE_ERROR``; a body, query or header off the schema ``422
  VALIDATION_FAILED`` with ``detail.on`` and ``detail.errors`` (``path``, ``message``,
  ``code``). ``Idempotency-Key`` is required on the enrollment, quote and checkout creates
  ("Omitting it there returns 422") and honoured on every other POST.
- The first response under a key, status, body and ``Retry-After``, is replayed for 24
  hours with ``Idempotent-Replayed: true``, 4xx and 5xx included; 401, 422 and 429 are not
  cached. The same key with a different request is ``400 IDEMPOTENT_PARAMETER_MISMATCH``;
  while a request under a key is in flight, another is ``409
  IDEMPOTENCY_REQUEST_IN_PROGRESS``. Neither is cached.

Bodies and queries are read by their wire (camelCase) names only, so a snake_case name is a
missing field ("The API ignores fields it does not recognize").

Assumed, by name: a key is scoped to the project, not the path, so reusing it on another
operation is a mismatch; "the same request" means the same method, path, JSON body (key
order and whitespace aside) and ``X-Simulate-Checkout``. A replay re-sends the first
response's ``Retry-After`` with it. The hosted card entry and approval pages (``/hosted/...``)
are the mock's own, local stand-ins for Reap's, need no API key, and answer HTML: a
completed step redirects (303) to the ``returnUrl``, a refused one re-renders with the
reason (400). A dropped response (``AgenticMockEngine.drop_next_response``) is raised as
``DroppedResponseError``, an ``httpx.ReadError``, after the operation ran and its response
was cached: through the in-process transport the client sees a read failure; behind a real
server it becomes a 500.
"""

import hashlib
import html
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

from youreapyousow.clock import Clock, utc_now
from youreapyousow.reap.mock.agentic import (
    MESSAGES,
    TEST_CARDS,
    TEST_OTP,
    AgenticMockEngine,
    AgenticMockError,
    HostedEnrollment,
    HostedStepError,
    Operation,
)
from youreapyousow.reap.mock.engine import MockReapEngine, MockReapError
from youreapyousow.reap.models import (
    REAP_VERSION,
    SIMULATE_CHECKOUT_HEADER,
    AgenticErrorCode,
    CheckoutStatus,
    CreateAccountRequest,
    CreateCardRequest,
    CreateCheckoutRequest,
    CreateEnrollmentRequest,
    CreateQuoteRequest,
    CreateUserRequest,
    CreateWebhookRequest,
    EnrollmentStatus,
    Money,
    PolicyCreate,
    ProductDetailsRequest,
    ProductSearchRequest,
    ResolveVariantRequest,
    ScopeType,
    SelectShippingOptionRequest,
    SimulateApplicationRequest,
    SimulateAuthorizationRequest,
    SimulateClearingRequest,
    SimulateFiatDepositRequest,
    Wire,
)

IDEMPOTENCY_REQUIRED = frozenset({"/accounts/", "/cards/"})
IDEMPOTENCY_TTL = timedelta(hours=24)

_POLICY_CREATE = TypeAdapter[PolicyCreate](PolicyCreate)

type Action = Callable[[JsonValue], Awaitable[BaseModel | None]]


@dataclass(frozen=True)
class _Stored:
    body_hash: str
    status: int
    content: JsonValue
    at: datetime


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "detail": None}}
    )


def _render(model: BaseModel | None) -> tuple[int, JsonValue]:
    if model is None:
        return 204, None
    return 200, model.to_wire() if isinstance(model, Wire) else model.model_dump(mode="json")


def _response(status: int, content: JsonValue) -> Response:
    if content is None:
        return Response(status_code=status)
    return JSONResponse(status_code=status, content=content)


class _MockReapRoutes:
    """Route handlers binding HTTP to the engine."""

    def __init__(self, engine: MockReapEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock
        self._replay: dict[tuple[str, str], _Stored] = {}

    async def _post(self, request: Request, action: Action) -> Response:
        """Run a POST with Reap's idempotency semantics and error shape.

        Args:
            request: The incoming request.
            action: Takes the parsed JSON body and returns the response model.

        Returns:
            The response, replayed when the key was seen with the same body.
        """
        raw = await request.body()
        path = request.url.path
        key = request.headers.get("idempotency-key")
        if path in IDEMPOTENCY_REQUIRED and not key:
            return _error(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required")
        body_hash = hashlib.sha256(raw).hexdigest()
        stored = self._replay.get((path, key)) if key else None
        if stored is not None and self._clock() - stored.at < IDEMPOTENCY_TTL:
            if stored.body_hash != body_hash:
                return _error(409, "IDEMPOTENCY_KEY_REUSED", "Key reused with a different body")
            return _response(stored.status, stored.content)
        try:
            status, content = _render(await action(json.loads(raw) if raw else None))
        except MockReapError as error:
            status, content = error.status, _error_body(error.code, error.message)
        except (ValidationError, json.JSONDecodeError) as error:
            status, content = 400, _error_body("VALIDATION_ERROR", str(error)[:500])
        if key:
            self._replay[(path, key)] = _Stored(body_hash, status, content, self._clock())
        return _response(status, content)

    async def _get(self, action: Callable[[], BaseModel]) -> Response:
        try:
            status, content = _render(action())
        except MockReapError as error:
            return _error(error.status, error.code, error.message)
        return _response(status, content)

    async def create_user(self, request: Request) -> Response:
        async def act(body: JsonValue) -> BaseModel:
            return self._engine.create_user(CreateUserRequest.model_validate(body))

        return await self._post(request, act)

    async def get_user(self, user_id: str) -> Response:
        return await self._get(lambda: self._engine.get_user(user_id))

    async def simulate_application(self, user_id: str, request: Request) -> Response:
        async def act(body: JsonValue) -> None:
            req = SimulateApplicationRequest.model_validate(body)
            await self._engine.simulate_application(user_id, req.status)

        return await self._post(request, act)

    async def create_account(self, request: Request) -> Response:
        async def act(body: JsonValue) -> BaseModel:
            return self._engine.create_account(CreateAccountRequest.model_validate(body))

        return await self._post(request, act)

    async def balance(self, account_id: str) -> Response:
        return await self._get(lambda: self._engine.get_balance(account_id))

    async def fiat_deposit(self, request: Request) -> Response:
        async def act(body: JsonValue) -> BaseModel:
            req = SimulateFiatDepositRequest.model_validate(body)
            return await self._engine.simulate_fiat_deposit(req)

        return await self._post(request, act)

    async def create_card(self, request: Request) -> Response:
        async def act(body: JsonValue) -> BaseModel:
            return self._engine.create_card(CreateCardRequest.model_validate(body))

        return await self._post(request, act)

    async def get_card(self, card_id: str) -> Response:
        return await self._get(lambda: self._engine.get_card(card_id))

    async def freeze(self, card_id: str, request: Request) -> Response:
        async def act(_: JsonValue) -> BaseModel:
            return await self._engine.set_frozen(card_id, frozen=True)

        return await self._post(request, act)

    async def unfreeze(self, card_id: str, request: Request) -> Response:
        async def act(_: JsonValue) -> BaseModel:
            return await self._engine.set_frozen(card_id, frozen=False)

        return await self._post(request, act)

    async def delete_card(self, card_id: str) -> Response:
        try:
            self._engine.delete_card(card_id)
        except MockReapError as error:
            return _error(error.status, error.code, error.message)
        return Response(status_code=204)

    async def create_policy(self, request: Request) -> Response:
        async def act(body: JsonValue) -> BaseModel:
            return self._engine.create_policy(_POLICY_CREATE.validate_python(body))

        return await self._post(request, act)

    async def disable_policy(self, policy_id: str, request: Request) -> Response:
        async def act(_: JsonValue) -> BaseModel:
            return self._engine.disable_policy(policy_id)

        return await self._post(request, act)

    async def effective(self, request: Request) -> Response:
        params = request.query_params
        try:
            scope = ScopeType(params.get("scopeType", ""))
        except ValueError:
            return _error(400, "VALIDATION_ERROR", "scopeType must be PROJECT, USER or CARD")
        return await self._get(
            lambda: self._engine.effective_policies(scope, params.get("scopeId"))
        )

    async def authorise(self, request: Request) -> Response:
        async def act(body: JsonValue) -> BaseModel:
            req = SimulateAuthorizationRequest.model_validate(body)
            return await self._engine.simulate_authorization(req)

        return await self._post(request, act)

    async def clear(self, request: Request) -> Response:
        async def act(body: JsonValue) -> BaseModel:
            req = SimulateClearingRequest.model_validate(body)
            return await self._engine.simulate_clearing(req)

        return await self._post(request, act)

    async def transaction(self, transaction_id: str) -> Response:
        return await self._get(lambda: self._engine.get_transaction(transaction_id))

    async def activities(self, request: Request) -> Response:
        params = request.query_params
        limit = max(1, min(int(params.get("limit", "20")), 100))
        return await self._get(
            lambda: self._engine.activities(
                card_id=params.get("cardId"),
                type_=params.get("type"),
                cursor=params.get("cursor"),
                limit=limit,
            )
        )

    async def create_webhook(self, request: Request) -> Response:
        async def act(body: JsonValue) -> BaseModel:
            return self._engine.create_webhook(CreateWebhookRequest.model_validate(body))

        return await self._post(request, act)


def _error_body(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": None}}


# Agentic paths

AGENTIC_PREFIX = "/agentic"
HOSTED_PREFIX = "/hosted"
IDEMPOTENT_REPLAYED = "Idempotent-Replayed"

_MAX_KEY_LENGTH = 255
_UNCACHED_STATUSES = frozenset({401, 422, 429})
_UNCACHED_CODES = frozenset(
    {
        AgenticErrorCode.IDEMPOTENT_PARAMETER_MISMATCH,
        AgenticErrorCode.IDEMPOTENCY_REQUEST_IN_PROGRESS,
    }
)
_ENROLLMENT = TypeAdapter[CreateEnrollmentRequest](CreateEnrollmentRequest)
_QUOTE = TypeAdapter[CreateQuoteRequest](CreateQuoteRequest)

type AgenticAction = Callable[[JsonValue], BaseModel]


class DroppedResponseError(httpx.ReadError):
    """A response the mock lost on purpose after the operation ran (a test fault)."""


class _EnrollmentQuery(Wire):
    """The query of ``GET /agentic/enrollments``, constrained as the OpenAPI has it."""

    owner_id: str = Field(min_length=1)
    owner_type: Literal["REAP_USER", "CLIENT_REFERENCE"] = "CLIENT_REFERENCE"
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1)


@dataclass(frozen=True)
class _Answer:
    """A response as cached under an idempotency key.

    Attributes:
        status: HTTP status.
        content: The JSON body.
        headers: Headers replayed with it, such as ``Retry-After``.
        code: The error code, if it is an error.
    """

    status: int
    content: JsonValue
    headers: dict[str, str] = field(default_factory=dict[str, str])
    code: str | None = None

    def response(self, *, replayed: bool = False) -> Response:
        """Render the answer.

        Args:
            replayed: Whether to mark it ``Idempotent-Replayed: true``.

        Returns:
            The JSON response.
        """
        headers = dict(self.headers)
        if replayed:
            headers[IDEMPOTENT_REPLAYED] = "true"
        return JSONResponse(status_code=self.status, content=self.content, headers=headers)


@dataclass(frozen=True)
class _Cached:
    fingerprint: str
    answer: _Answer
    at: datetime


def _seconds(value: float) -> str:
    return format(value, "g")


def _agentic_answer(error: AgenticMockError) -> _Answer:
    headers = (
        {"Retry-After": _seconds(error.retry_after_s)} if error.retry_after_s is not None else {}
    )
    return _Answer(
        error.status,
        {"error": {"code": error.code, "message": error.message, "detail": error.detail}},
        headers,
        error.code,
    )


def _agentic_error(status: int, code: str, detail: dict[str, Any] | None = None) -> _Answer:
    return _agentic_answer(AgenticMockError(status, code, MESSAGES.get(code), detail=detail))


def _validation_failed(on: str, error: ValidationError, *, tagged: bool = False) -> _Answer:
    """Turn a pydantic error into the errors page's ``VALIDATION_FAILED`` detail.

    Args:
        on: ``body``, ``query``, ``params`` or ``headers``.
        error: The validation error.
        tagged: Whether the first location element is a union's tag, not a field.

    Returns:
        The 422 answer.
    """
    errors = [
        {
            "path": ".".join(str(part) for part in item["loc"][1 if tagged else 0 :]),
            "message": item["msg"],
            "code": item["type"],
        }
        for item in error.errors()
    ]
    return _agentic_error(422, AgenticErrorCode.VALIDATION_FAILED, {"on": on, "errors": errors})


def _header_invalid(name: str, message: str) -> _Answer:
    return _agentic_error(
        422,
        AgenticErrorCode.VALIDATION_FAILED,
        {"on": "headers", "errors": [{"path": name, "message": message, "code": "invalid"}]},
    )


def _canonical(raw: bytes) -> str:
    """Reduce a body to what makes two requests the same: its JSON, key order aside.

    Args:
        raw: The raw body.

    Returns:
        A canonical text form.
    """
    try:
        return json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return raw.hex()


def _wire_json(model: BaseModel) -> JsonValue:
    return model.to_wire() if isinstance(model, Wire) else model.model_dump(mode="json")


class _AgenticRoutes:
    """Route handlers binding HTTP to the agentic engine, with Reap's idempotency."""

    def __init__(self, engine: AgenticMockEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock
        self._cache: dict[str, _Cached] = {}
        self._in_flight: set[str] = set()

    async def _post(
        self,
        request: Request,
        operation: Operation,
        action: AgenticAction,
        *,
        key_required: bool = False,
        has_body: bool = True,
    ) -> Response:
        """Run an agentic POST with Reap's idempotency semantics.

        Args:
            request: The incoming request.
            operation: The operation, for the engine's test hooks.
            action: Takes the parsed JSON body and returns the response model.
            key_required: Whether ``Idempotency-Key`` is required.
            has_body: Whether the operation takes a body.

        Returns:
            The response, replayed when the key was seen with the same request.

        Raises:
            DroppedResponseError: The engine was told to lose this response.
        """
        raw = await request.body()
        key = request.headers.get("idempotency-key")
        if key is None and key_required:
            return _header_invalid("Idempotency-Key", "Required").response()
        if key is not None and not 1 <= len(key) <= _MAX_KEY_LENGTH:
            return _header_invalid("Idempotency-Key", "Must be 1 to 255 characters").response()
        simulate = request.headers.get(SIMULATE_CHECKOUT_HEADER)
        fingerprint = hashlib.sha256(
            "\n".join([request.method, request.url.path, _canonical(raw), simulate or ""]).encode()
        ).hexdigest()
        if key is not None:
            cached = self._cache.get(key)
            if cached is not None and self._clock() - cached.at < IDEMPOTENCY_TTL:
                if cached.fingerprint != fingerprint:
                    return _agentic_error(
                        400, AgenticErrorCode.IDEMPOTENT_PARAMETER_MISMATCH
                    ).response()
                return cached.answer.response(replayed=True)
            if key in self._in_flight:
                return _agentic_error(
                    409, AgenticErrorCode.IDEMPOTENCY_REQUEST_IN_PROGRESS
                ).response()
            self._in_flight.add(key)
        try:
            gate = self._engine.take_hold(operation)
            if gate is not None:
                await gate.wait()
            answer = self._run(raw, action, has_body=has_body)
        finally:
            if key is not None:
                self._in_flight.discard(key)
        if (
            key is not None
            and answer.status not in _UNCACHED_STATUSES
            and answer.code not in _UNCACHED_CODES
        ):
            self._cache[key] = _Cached(fingerprint, answer, self._clock())
        return self._deliver(operation, answer)

    def _run(self, raw: bytes, action: AgenticAction, *, has_body: bool) -> _Answer:
        """Parse the body and run the action, mapping each failure to Reap's answer.

        Args:
            raw: The raw body.
            action: The action.
            has_body: Whether the operation takes a body.

        Returns:
            The answer.
        """
        try:
            body: JsonValue = json.loads(raw) if has_body and raw else None
        except ValueError:
            return _agentic_error(400, "PARSE_ERROR")
        try:
            return _Answer(200, _wire_json(action(body)))
        except AgenticMockError as error:
            return _agentic_answer(error)
        except _TaggedValidationError as error:
            return _validation_failed("body", error.error, tagged=True)
        except ValidationError as error:
            return _validation_failed("body", error)

    def _deliver(self, operation: Operation, answer: _Answer) -> Response:
        if self._engine.take_drop(operation):
            raise DroppedResponseError(f"{operation}: response dropped by the mock")
        return answer.response()

    async def _get(self, operation: Operation, action: Callable[[], BaseModel]) -> Response:
        try:
            answer = _Answer(200, _wire_json(action()))
        except AgenticMockError as error:
            answer = _agentic_answer(error)
        return self._deliver(operation, answer)

    async def list_enrollments(self, request: Request) -> Response:
        try:
            query = _read(_EnrollmentQuery, dict(request.query_params))
        except ValidationError as error:
            return _validation_failed("query", error).response()
        return await self._get(
            Operation.LIST_ENROLLMENTS,
            lambda: self._engine.list_enrollments(
                owner_id=query.owner_id,
                owner_type=query.owner_type,
                limit=query.limit,
                cursor=query.cursor,
            ),
        )

    async def create_enrollment(self, request: Request) -> Response:
        return await self._post(
            request,
            Operation.CREATE_ENROLLMENT,
            lambda body: self._engine.create_enrollment(_tagged(_ENROLLMENT, body)),
            key_required=True,
        )

    async def get_enrollment(self, enrollment_id: str) -> Response:
        return await self._get(
            Operation.GET_ENROLLMENT, lambda: self._engine.get_enrollment(enrollment_id)
        )

    async def revoke_enrollment(self, enrollment_id: str, request: Request) -> Response:
        return await self._post(
            request,
            Operation.REVOKE_ENROLLMENT,
            lambda _: self._engine.revoke_enrollment(enrollment_id),
            has_body=False,
        )

    async def search(self, request: Request) -> Response:
        return await self._post(
            request,
            Operation.SEARCH,
            lambda body: self._engine.search_products(_read(ProductSearchRequest, body)),
        )

    async def details(self, request: Request) -> Response:
        return await self._post(
            request,
            Operation.DETAILS,
            lambda body: self._engine.product_details(_read(ProductDetailsRequest, body)),
        )

    async def variant(self, request: Request) -> Response:
        return await self._post(
            request,
            Operation.VARIANT,
            lambda body: self._engine.resolve_variant(_read(ResolveVariantRequest, body)),
        )

    async def create_quote(self, request: Request) -> Response:
        return await self._post(
            request,
            Operation.CREATE_QUOTE,
            lambda body: self._engine.create_quote(_tagged(_QUOTE, body)),
            key_required=True,
        )

    async def get_quote(self, quote_id: str) -> Response:
        return await self._get(Operation.GET_QUOTE, lambda: self._engine.get_quote(quote_id))

    async def select_shipping_option(self, quote_id: str, request: Request) -> Response:
        return await self._post(
            request,
            Operation.SELECT_SHIPPING_OPTION,
            lambda body: self._engine.select_shipping_option(
                quote_id, _read(SelectShippingOptionRequest, body)
            ),
        )

    async def create_checkout(self, request: Request) -> Response:
        simulate = request.headers.get(SIMULATE_CHECKOUT_HEADER)
        if simulate is not None and simulate != "COMPLETED":
            return _header_invalid(SIMULATE_CHECKOUT_HEADER, "Must be COMPLETED").response()
        return await self._post(
            request,
            Operation.CREATE_CHECKOUT,
            lambda body: self._engine.create_checkout(
                _read(CreateCheckoutRequest, body), simulate=simulate
            ),
            key_required=True,
        )

    async def get_checkout(self, checkout_id: str) -> Response:
        return await self._get(
            Operation.GET_CHECKOUT, lambda: self._engine.get_checkout(checkout_id)
        )

    async def route_not_found(self) -> Response:
        return _agentic_error(404, "ROUTE_NOT_FOUND").response()

    # Hosted pages

    async def enrollment_page(self, enrollment_id: str) -> Response:
        try:
            page = self._engine.enrollment_page(enrollment_id)
        except HostedStepError as error:
            return _html(404, "Not found", f"<p>{_e(str(error))}</p>")
        return _enrollment_html(page)

    async def submit_card(self, enrollment_id: str, request: Request) -> Response:
        form = _form(await request.body())
        try:
            return_url = self._engine.submit_card(
                enrollment_id,
                number=form.get("number", ""),
                cvc=form.get("cvc", ""),
                expiry=form.get("expiry", ""),
                otp=form.get("otp", ""),
            )
        except HostedStepError as error:
            try:
                page = self._engine.enrollment_page(enrollment_id)
            except HostedStepError:
                return _html(404, "Not found", f"<p>{_e(str(error))}</p>")
            return _enrollment_html(page, error=str(error))
        return RedirectResponse(return_url, status_code=303)

    async def checkout_page(self, checkout_id: str) -> Response:
        try:
            return _checkout_html(self._engine, checkout_id)
        except HostedStepError as error:
            return _html(404, "Not found", f"<p>{_e(str(error))}</p>")

    async def approve(self, checkout_id: str) -> Response:
        try:
            return_url = self._engine.approve_checkout(checkout_id)
        except HostedStepError as error:
            try:
                return _checkout_html(self._engine, checkout_id, error=str(error))
            except HostedStepError:
                return _html(404, "Not found", f"<p>{_e(str(error))}</p>")
        return RedirectResponse(return_url, status_code=303)


def _read[M: BaseModel](model: type[M], data: object) -> M:
    """Validate by wire names only: Reap ignores fields it does not know.

    Args:
        model: The request model.
        data: The parsed body or query.

    Returns:
        The validated request.
    """
    return model.model_validate(data, by_alias=True, by_name=False)


class _TaggedValidationError(Exception):
    """A union's validation error, whose locations start with the union's tag."""

    def __init__(self, error: ValidationError) -> None:
        super().__init__(str(error))
        self.error = error


def _tagged[T](adapter: TypeAdapter[T], body: JsonValue) -> T:
    """Validate a body against a tagged union, marking its errors as tagged.

    Args:
        adapter: The union's adapter.
        body: The body.

    Returns:
        The validated request.

    Raises:
        _TaggedValidationError: The body is off the schema.
    """
    try:
        return adapter.validate_python(body, by_alias=True, by_name=False)
    except ValidationError as error:
        raise _TaggedValidationError(error) from error


def _form(raw: bytes) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _money(money: Money, *, off: bool = False) -> str:
    return f"{'-' if off else ''}{Decimal(money.amount):.2f} {money.currency}"


def _when(moment: datetime) -> str:
    return f"{moment.day} {moment:%b %Y, %H:%M} UTC"


_PAGE_STYLE = """
:root { color-scheme: light dark; --bg: #f5f6f8; --card: #ffffff; --ink: #1d2330;
  --muted: #5d6677; --line: #e1e4ea; --accent: #2457d6; --accent-ink: #ffffff;
  --bad: #b42318; }
@media (prefers-color-scheme: dark) { :root { --bg: #11141a; --card: #1a1f28;
  --ink: #e8ebf1; --muted: #9aa3b5; --line: #2b3240; --accent: #7ea2ff; --accent-ink: #0b1020;
  --bad: #ff8a80; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 28rem; margin: 3rem auto; padding: 0 1rem; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 1.5rem; }
.badge { display: inline-block; font-size: 0.75rem; letter-spacing: 0.04em;
  text-transform: uppercase; color: var(--muted); border: 1px solid var(--line);
  border-radius: 999px; padding: 0.1rem 0.6rem; }
h1 { font-size: 1.25rem; margin: 0.75rem 0 1rem; }
p { margin: 0 0 1rem; }
.muted { color: var(--muted); font-size: 0.875rem; }
.url { overflow-wrap: anywhere; }
details { margin: 1rem 0 0; }
summary { cursor: pointer; }
ul { margin: 0.5rem 0; padding-left: 1.25rem; font-variant-numeric: tabular-nums; }
.error { color: var(--bad); font-weight: 600; }
label { display: block; font-size: 0.875rem; color: var(--muted); margin: 0 0 0.25rem; }
input { width: 100%; font: inherit; color: inherit; background: transparent;
  border: 1px solid var(--line); border-radius: 8px; padding: 0.5rem 0.75rem;
  margin: 0 0 0.875rem; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
button { width: 100%; font: inherit; font-weight: 600; color: var(--accent-ink);
  background: var(--accent); border: 0; border-radius: 8px; padding: 0.65rem 1rem;
  cursor: pointer; }
table { width: 100%; border-collapse: collapse; margin: 0 0 1rem; }
td { padding: 0.35rem 0; border-bottom: 1px solid var(--line); }
td.amount { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
tr.subtotal td { border-top: 2px solid var(--line); }
tr.total td { font-weight: 700; border-bottom: 0; }
"""


def _html(status: int, title: str, body: str) -> HTMLResponse:
    """Render a hosted page, plainly marked as the local mock rather than Reap.

    Args:
        status: HTTP status.
        title: The page title.
        body: The card's inner HTML, already escaped.

    Returns:
        The page.
    """
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)} - Reap agentic mock</title><style>{_PAGE_STYLE}</style></head>"
        '<body><main><div class="card"><span class="badge">Local mock, not Reap</span>'
        f"<h1>{_e(title)}</h1>{body}</div></main></body></html>"
    )
    return HTMLResponse(page, status_code=status)


def _enrollment_html(page: HostedEnrollment, *, error: str = "") -> HTMLResponse:
    """The hosted card entry page: the form while a card is awaited, else the status.

    Args:
        page: What the page shows.
        error: Why the last submission was refused, if it was.

    Returns:
        The page.
    """
    notice = f'<p class="error">{_e(error)}</p>' if error else ""
    intro = (
        f"<p>Store a card for agentic purchases by <strong>{_e(page.email)}</strong>. "
        f'Afterwards you return to <span class="muted url">{_e(page.return_url)}</span>.</p>'
    )
    status = 400 if error else 200
    if page.status != EnrollmentStatus.REQUIRES_ACTION:
        body = f"{notice}{intro}<p>This enrollment is <strong>{_e(page.status)}</strong>.</p>"
        return _html(status, "Card entry", body)
    cards = "".join(
        f"<li>{_e(number)}, CVC {_e(cvc)}, expiry {_e(expiry)}</li>"
        for number, (cvc, expiry) in TEST_CARDS.items()
    )
    form = (
        '<form method="post">'
        '<label for="number">Card number</label>'
        '<input id="number" name="number" inputmode="numeric" autocomplete="off" '
        'placeholder="0000 0000 0000 0000" required>'
        '<div class="row"><div><label for="expiry">Expiry</label>'
        '<input id="expiry" name="expiry" placeholder="MM/YY" required></div>'
        '<div><label for="cvc">CVC</label>'
        '<input id="cvc" name="cvc" inputmode="numeric" required></div></div>'
        '<label for="otp">One-time password</label>'
        '<input id="otp" name="otp" inputmode="numeric" required>'
        '<button type="submit">Store card</button></form>'
        f'<p class="muted">Open until {_e(_when(page.expires_at))}.</p>'
        '<details class="muted"><summary>Sandbox test cards</summary>'
        f"<ul>{cards}</ul>One-time password {_e(TEST_OTP)}.</details>"
    )
    return _html(status, "Card entry", f"{notice}{intro}{form}")


def _checkout_html(engine: AgenticMockEngine, checkout_id: str, *, error: str = "") -> HTMLResponse:
    """The hosted approval page: the order and its total, and Approve while it waits.

    Args:
        engine: The engine.
        checkout_id: The checkout.
        error: Why the last approval was refused, if it was.

    Returns:
        The page.

    Raises:
        HostedStepError: There is no such checkout.
    """
    page = engine.checkout_page(checkout_id)
    rows = ""
    for line in page.lines:
        total = Money(amount=line.price.amount * line.quantity, currency=line.price.currency)
        rows += (
            f'<tr><td>{_e(line.name)}<div class="muted">Quantity {line.quantity}</div></td>'
            f'<td class="amount">{_e(_money(total))}</td></tr>'
        )
    breakdown = page.breakdown
    parts: list[tuple[str, Money, bool]] = [("Items", breakdown.items_subtotal, False)]
    parts += [(d.name, d.amount, True) for d in breakdown.discounts or []]
    if breakdown.shipping is not None:
        parts.append(("Shipping", breakdown.shipping, False))
    if breakdown.tax is not None:
        included = " (included in prices)" if breakdown.tax.included_in_prices else ""
        parts.append((f"Tax{included}", breakdown.tax.amount, False))
    parts += [(c.name, c.amount, False) for c in breakdown.additional_charges or []]
    for index, (name, amount, off) in enumerate(parts):
        subtotal = ' class="subtotal"' if index == 0 else ""
        rows += (
            f"<tr{subtotal}><td>{_e(name)}</td>"
            f'<td class="amount">{_e(_money(amount, off=off))}</td></tr>'
        )
    rows += (
        '<tr class="total"><td>Total</td>'
        f'<td class="amount">{_e(_money(breakdown.final_amount))}</td></tr>'
    )
    notice = f'<p class="error">{_e(error)}</p>' if error else ""
    intro = f"<p>Approve this charge at <strong>{_e(page.merchant)}</strong>.</p>"
    if page.status == CheckoutStatus.REQUIRES_ACTION:
        action = (
            f'<form method="post" action="{_e(page.id)}/approve">'
            '<button type="submit">Approve</button></form>'
            f'<p class="muted">Open until {_e(_when(page.expires_at))}.</p>'
        )
    else:
        action = f"<p>This checkout is <strong>{_e(page.status)}</strong>.</p>"
    body = f"{notice}{intro}<table>{rows}</table>{action}"
    return _html(400 if error else 200, "Approve purchase", body)


def _add_agentic_routes(app: FastAPI, engine: AgenticMockEngine, clock: Clock) -> None:
    """Mount the agentic operations, the hosted pages and the agentic catch-all.

    Args:
        app: The app.
        engine: The agentic engine.
        clock: Time source for idempotency expiry.
    """
    routes = _AgenticRoutes(engine, clock)
    table: list[tuple[str, str, Callable[..., Awaitable[Response]]]] = [
        ("GET", "/agentic/enrollments", routes.list_enrollments),
        ("POST", "/agentic/enrollments", routes.create_enrollment),
        ("GET", "/agentic/enrollments/{enrollment_id}", routes.get_enrollment),
        ("POST", "/agentic/enrollments/{enrollment_id}/revoke", routes.revoke_enrollment),
        ("POST", "/agentic/products/search", routes.search),
        ("POST", "/agentic/products/details", routes.details),
        ("POST", "/agentic/products/variant", routes.variant),
        ("POST", "/agentic/quotes", routes.create_quote),
        ("GET", "/agentic/quotes/{quote_id}", routes.get_quote),
        ("POST", "/agentic/quotes/{quote_id}/shipping-option", routes.select_shipping_option),
        ("POST", "/agentic/checkouts", routes.create_checkout),
        ("GET", "/agentic/checkouts/{checkout_id}", routes.get_checkout),
        ("GET", "/hosted/enrollments/{enrollment_id}", routes.enrollment_page),
        ("POST", "/hosted/enrollments/{enrollment_id}", routes.submit_card),
        ("GET", "/hosted/checkouts/{checkout_id}", routes.checkout_page),
        ("POST", "/hosted/checkouts/{checkout_id}/approve", routes.approve),
    ]
    for method, path, endpoint in table:
        app.add_api_route(path, endpoint, methods=[method])
    for catch_all in (AGENTIC_PREFIX, f"{AGENTIC_PREFIX}/{{rest:path}}"):
        app.add_api_route(
            catch_all, routes.route_not_found, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
        )


def _agentic_headers(request: Request, engine: AgenticMockEngine) -> Response | None:
    """Check Reap's authentication and version headers on an agentic path.

    Args:
        request: The request.
        engine: The engine, for the configured API key.

    Returns:
        The error response, or None when the headers pass.
    """
    auth = request.headers.get("authorization")
    if auth is None:
        return _agentic_error(401, "API_KEY_REQUIRED").response()
    scheme, _, token = auth.partition(" ")
    if scheme != "Bearer" or not token.strip():
        return _agentic_error(401, "INVALID_AUTH_HEADER").response()
    if engine.config.api_key is not None and token.strip() != engine.config.api_key:
        return _agentic_error(401, "INVALID_API_KEY").response()
    version = request.headers.get("reap-version")
    if version is None:
        return _agentic_error(400, "API_VERSION_HEADER_MISSING").response()
    if version != REAP_VERSION:
        return _agentic_error(400, "API_VERSION_INVALID").response()
    return None


def create_mock_app(
    engine: MockReapEngine,
    clock: Clock = utc_now,
    *,
    agentic: AgenticMockEngine | None = None,
) -> FastAPI:
    """Build the FastAPI app that serves the mock engines on Reap's paths.

    Args:
        engine: The card engine holding the mock's state.
        clock: Time source for idempotency expiry.
        agentic: The agentic engine; one over the three bundled catalogues on ``clock``
            when None. It is also kept on ``app.state.agentic`` for its test hooks.

    Returns:
        The app.
    """
    app = FastAPI(title="Reap mock", docs_url=None, redoc_url=None, openapi_url=None)
    routes = _MockReapRoutes(engine, clock)
    agentic_engine = agentic if agentic is not None else AgenticMockEngine(clock=clock)
    app.state.agentic = agentic_engine

    async def reap_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path.startswith(f"{HOSTED_PREFIX}/"):
            return await call_next(request)
        if path == AGENTIC_PREFIX or path.startswith(f"{AGENTIC_PREFIX}/"):
            refused = _agentic_headers(request, agentic_engine)
            return refused if refused is not None else await call_next(request)
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return _error(401, "UNAUTHORIZED", "Missing bearer API key")
        if request.headers.get("reap-version") != REAP_VERSION:
            return _error(400, "INVALID_VERSION", f"Reap-Version must be {REAP_VERSION}")
        return await call_next(request)

    app.middleware("http")(reap_headers)
    table: list[tuple[str, str, Callable[..., Awaitable[Response]]]] = [
        ("POST", "/users/", routes.create_user),
        ("GET", "/users/{user_id}", routes.get_user),
        ("POST", "/simulation/users/{user_id}/application", routes.simulate_application),
        ("POST", "/accounts/", routes.create_account),
        ("GET", "/accounts/{account_id}/balance", routes.balance),
        ("POST", "/simulation/fiat-deposits", routes.fiat_deposit),
        ("POST", "/cards/", routes.create_card),
        ("GET", "/cards/{card_id}", routes.get_card),
        ("POST", "/cards/{card_id}/freeze", routes.freeze),
        ("POST", "/cards/{card_id}/unfreeze", routes.unfreeze),
        ("DELETE", "/cards/{card_id}", routes.delete_card),
        ("POST", "/policies/", routes.create_policy),
        ("GET", "/policies/effective", routes.effective),
        ("POST", "/policies/{policy_id}/disable", routes.disable_policy),
        ("POST", "/simulation/card-transactions/authorization", routes.authorise),
        ("POST", "/simulation/card-transactions/clearing", routes.clear),
        ("GET", "/card-transactions/{transaction_id}", routes.transaction),
        ("GET", "/activities/", routes.activities),
        ("POST", "/webhooks/", routes.create_webhook),
    ]
    for method, path, endpoint in table:
        app.add_api_route(path, endpoint, methods=[method])
    _add_agentic_routes(app, agentic_engine, clock)
    return app

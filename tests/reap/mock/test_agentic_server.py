"""The agentic mock over HTTP, driven by the real client and, for hosted pages, a browser."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from youreapyousow.clock import ManualClock
from youreapyousow.reap.client import (
    MOCK_BASE_URL,
    ReapError,
    ReapHttpClient,
    ReapMock,
    ReapTransportError,
)
from youreapyousow.reap.mock.agentic import (
    DOCUMENTED_ERRORS,
    RETRY_AFTER_CODES,
    TEST_OTP,
    AgenticMockConfig,
    AgenticMockEngine,
    Environment,
    Operation,
    bundled_catalogue,
)
from youreapyousow.reap.mock.engine import MockReapEngine
from youreapyousow.reap.mock.server import DroppedResponseError, create_mock_app
from youreapyousow.reap.models import (
    REAP_VERSION,
    AgenticErrorCode,
    CheckoutStatus,
    ClientReferenceOwner,
    CreateCheckoutRequest,
    CreateExternalEnrollmentRequest,
    CreateItemsQuoteRequest,
    EnrollmentCreated,
    EnrollmentStatus,
    ExternalEnrollmentCreated,
    Presentation,
    PriceFilter,
    ProductDetailsRequest,
    ProductSearchRequest,
    QuoteItem,
    ResolveVariantRequest,
    SearchContext,
    SearchFilters,
    SearchPagination,
    SelectShippingOptionRequest,
    ShippingAddress,
)

pytestmark = pytest.mark.anyio

HEADERS = {"Authorization": "Bearer k", "Reap-Version": REAP_VERSION}
RETURN_URL = "https://example.com/return"
CARD = {"number": "4622 9431 2313 7797", "cvc": "640", "expiry": "12/27", "otp": TEST_OTP}
SG_ADDRESS = ShippingAddress(
    first_name="Site",
    last_name="Engineer",
    phone="+6561234567",
    address_line1="1 Example Road",
    city="Singapore",
    postal_code="000001",
    country="SG",
)


@dataclass
class Mock:
    """The mock app, the real client on it, and a raw client playing the browser.

    Attributes:
        engine: The agentic engine behind the app, for test hooks.
        reap: The real HTTP client, unchanged, on the in-process app.
        http: A raw HTTP client on the same app.
        clock: The manual clock the engine, the client's sleeps and its deadline share.
    """

    engine: AgenticMockEngine
    reap: ReapHttpClient
    http: httpx.AsyncClient
    clock: ManualClock


@pytest.fixture
async def mock(clock: ManualClock) -> AsyncIterator[Mock]:
    """The agentic mock behind the real client; the client's waits move the manual clock.

    Yields:
        The mock.
    """
    engine = AgenticMockEngine(bundled_catalogue(), clock=clock)
    app = create_mock_app(MockReapEngine(clock=clock), clock, agentic=engine)
    transport = httpx.ASGITransport(app=app)

    async def sleep(seconds: float) -> None:
        clock.advance(seconds=seconds)

    reap = ReapHttpClient(
        base_url=MOCK_BASE_URL,
        api_key=SecretStr("k"),
        backend="mock",
        transport=transport,
        sleep=sleep,
        monotonic=lambda: clock().timestamp(),
    )
    async with httpx.AsyncClient(transport=transport, base_url=MOCK_BASE_URL) as http:
        yield Mock(engine, reap, http, clock)
    await reap.aclose()


def _key() -> str:
    return str(uuid.uuid4())


def _card_entry(created: EnrollmentCreated) -> str:
    """Return the hosted card entry URL of an external enrollment.

    Returns:
        ``nextAction.url``.
    """
    assert isinstance(created, ExternalEnrollmentCreated)
    assert created.next_action is not None
    return created.next_action.url


def _enrollment_request(owner: str = "objective-1") -> CreateExternalEnrollmentRequest:
    return CreateExternalEnrollmentRequest(
        owner=ClientReferenceOwner(id=owner, email="operator@example.com"),
        presentation=Presentation(return_url=RETURN_URL),
    )


async def _active_enrollment(mock: Mock) -> str:
    created = await mock.reap.create_enrollment(_enrollment_request(), idempotency_key=_key())
    entered = await mock.http.post(_card_entry(created), data=CARD)
    assert entered.status_code == 303
    return created.id


def _quote_request(variant_id: str = "var-northwind-nvme-1tb") -> CreateItemsQuoteRequest:
    return CreateItemsQuoteRequest(
        email="operator@example.com",
        items=[QuoteItem(variant_id=variant_id, quantity=1)],
        shipping_address=SG_ADDRESS,
    )


def _checkout_request(quote_id: str, enrollment_id: str) -> CreateCheckoutRequest:
    return CreateCheckoutRequest(
        quote_id=quote_id,
        enrollment_id=enrollment_id,
        presentation=Presentation(return_url=RETURN_URL),
    )


def _error(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    assert set(body) == {"code", "message", "detail"}
    return body


# Acceptance: the real client from search to COMPLETED, no test-only shortcut


async def test_a_whole_purchase_completes_under_the_sandbox_header(mock: Mock) -> None:
    """Enrol a test card, search, detail, resolve, quote, ship, check out, poll: COMPLETED."""
    reap = mock.reap
    created = await reap.create_enrollment(_enrollment_request(), idempotency_key="enr:obj-1")
    assert created.status == EnrollmentStatus.REQUIRES_ACTION
    page = await mock.http.get(_card_entry(created))
    assert page.status_code == 200
    assert "card number" in page.text.lower()
    entered = await mock.http.post(_card_entry(created), data=CARD)
    assert (entered.status_code, entered.headers["location"]) == (303, RETURN_URL)
    enrollment = await reap.get_enrollment(created.id)
    assert enrollment.status == EnrollmentStatus.ACTIVE

    search = await reap.search_products(
        ProductSearchRequest(
            query="1TB NVMe M.2 2280 SSD",
            context=SearchContext(country="SG", currency="USD"),
            filters=SearchFilters(
                price=PriceFilter(min=Decimal("40"), max=Decimal("200")),
                availability="AVAILABLE_ONLY",
            ),
            pagination=SearchPagination(limit=20),
        )
    )
    assert "prd-northwind-nvme-1tb" in [p.id for p in search.products]
    details = await reap.product_details(
        ProductDetailsRequest(product_ids=[p.id for p in search.products])
    )
    drive = next(p for p in details.products if p.id == "prd-northwind-nvme-1tb")
    wanted = {"Capacity": "1 TB", "Interface": "NVMe", "Form factor": "M.2 2280"}
    option_ids = [
        value.option_id
        for group in drive.options
        for value in group.values
        if wanted[group.name] == value.label
    ]
    variant = await reap.resolve_variant(
        ResolveVariantRequest(product_id=drive.id, option_ids=option_ids)
    )
    assert variant.id == drive.default_variant.id

    quote = await reap.create_quote(
        _quote_request(variant.id), idempotency_key=f"quo:need-1:{variant.id}:1"
    )
    express = next(o for o in quote.shipping_options if o.name == "Express")
    quote = await reap.select_shipping_option(
        quote.id,
        SelectShippingOptionRequest(shipping_option_id=express.id),
        idempotency_key=f"shp:{quote.id}:{express.id}",
    )
    assert quote.amount_breakdown.final_amount.amount == Decimal("87")
    assert await reap.get_quote(quote.id) == quote

    checkout = await reap.create_checkout(
        _checkout_request(quote.id, created.id), idempotency_key="claim-1", simulate="COMPLETED"
    )
    assert checkout.status == CheckoutStatus.COMPLETED
    done = await reap.poll_checkout(checkout.id, every_s=1, deadline_s=120)
    assert done.status == CheckoutStatus.COMPLETED
    assert done.order_id is not None
    assert done.final_amount == quote.amount_breakdown.final_amount


async def test_a_purchase_approved_on_the_hosted_page_completes(mock: Mock) -> None:
    """Without the header: REQUIRES_ACTION, the user approves, PROCESSING, then COMPLETED."""
    reap = mock.reap
    enrollment_id = await _active_enrollment(mock)
    quote = await reap.create_quote(_quote_request(), idempotency_key=_key())
    created = await reap.create_checkout(
        _checkout_request(quote.id, enrollment_id), idempotency_key=_key()
    )
    waiting = await reap.poll_checkout(created.id, every_s=1, deadline_s=120)
    assert waiting.status == CheckoutStatus.REQUIRES_ACTION
    assert waiting.next_action is not None
    page = await mock.http.get(waiting.next_action.url)
    assert page.status_code == 200
    assert "Northwind Components" in page.text
    assert "77.00 USD" in page.text
    approved = await mock.http.post(f"{waiting.next_action.url}/approve")
    assert (approved.status_code, approved.headers["location"]) == (303, RETURN_URL)
    processing = await reap.get_checkout(created.id)
    assert processing.status == CheckoutStatus.PROCESSING
    done = await reap.poll_checkout(created.id, every_s=1, deadline_s=120)
    assert done.status == CheckoutStatus.COMPLETED
    assert done.final_amount == quote.amount_breakdown.final_amount
    assert done.order_id is not None


async def test_u1s_default_mock_backend_serves_the_agentic_paths(clock: ManualClock) -> None:
    """ReapMock answers agentic calls from the bundled catalogues."""
    reap = ReapMock(clock=clock)
    try:
        search = await reap.search_products(ProductSearchRequest(query="Sony WH 1000XM5"))
        assert [p.id for p in search.products] == ["prd-sony-wh-1000xm5"]
    finally:
        await reap.aclose()


# Reap's headers, routes, bodies


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Reap-Version": REAP_VERSION}, 401, "API_KEY_REQUIRED"),
        ({"Authorization": "Basic k", "Reap-Version": REAP_VERSION}, 401, "INVALID_AUTH_HEADER"),
        ({"Authorization": "Bearer k"}, 400, "API_VERSION_HEADER_MISSING"),
        ({"Authorization": "Bearer k", "Reap-Version": "2037-01-01"}, 400, "API_VERSION_INVALID"),
    ],
)
async def test_agentic_paths_answer_the_errors_pages_header_codes(
    mock: Mock, headers: dict[str, str], status: int, code: str
) -> None:
    """Missing or wrong Authorization and Reap-Version, with the errors page's codes."""
    response = await mock.http.get("/agentic/checkouts/x", headers=headers)
    assert response.status_code == status
    assert _error(response)["code"] == code


async def test_a_configured_key_refuses_any_other(mock: Mock) -> None:
    """With an API key set, another bearer key is INVALID_API_KEY."""
    mock.engine.config = replace(mock.engine.config, api_key="the-key")
    response = await mock.http.get("/agentic/checkouts/x", headers=HEADERS)
    assert (response.status_code, _error(response)["code"]) == (401, "INVALID_API_KEY")
    allowed = await mock.http.get(
        "/agentic/checkouts/x", headers=HEADERS | {"Authorization": "Bearer the-key"}
    )
    assert _error(allowed)["code"] == "CHECKOUT_NOT_FOUND"


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/agentic/nothing"), ("GET", "/agentic/quotes"), ("POST", "/agentic")],
)
async def test_an_unknown_route_is_route_not_found(mock: Mock, method: str, path: str) -> None:
    """No endpoint matches the method and path: 404 ROUTE_NOT_FOUND."""
    response = await mock.http.request(method, path, headers=HEADERS)
    assert (response.status_code, _error(response)["code"]) == (404, "ROUTE_NOT_FOUND")


async def test_a_body_that_does_not_parse_is_parse_error(mock: Mock) -> None:
    """Malformed JSON is 400 PARSE_ERROR."""
    response = await mock.http.post(
        "/agentic/products/search",
        content=b"{not json",
        headers=HEADERS | {"Content-Type": "application/json"},
    )
    assert (response.status_code, _error(response)["code"]) == (400, "PARSE_ERROR")


async def test_a_body_off_the_schema_is_validation_failed(mock: Mock) -> None:
    """422 VALIDATION_FAILED with detail.on and each failing path, as the errors page shows."""
    response = await mock.http.post(
        "/agentic/quotes",
        json={"email": "a@example.com", "items": [{"variantId": "v", "quantity": 0}]},
        headers=HEADERS | {"Idempotency-Key": _key()},
    )
    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "VALIDATION_FAILED"
    assert error["detail"]["on"] == "body"
    assert [e["path"] for e in error["detail"]["errors"]] == ["items.0.quantity"]
    assert all({"path", "message", "code"} <= set(e) for e in error["detail"]["errors"])


async def test_a_quote_id_that_is_not_a_uuid_is_422(mock: Mock) -> None:
    """Reap's changelog: create checkout returns 422 for a malformed quoteId."""
    response = await mock.http.post(
        "/agentic/checkouts",
        json={
            "quoteId": "not-a-uuid",
            "enrollmentId": str(uuid.uuid4()),
            "presentation": {"type": "REDIRECT", "returnUrl": RETURN_URL},
        },
        headers=HEADERS | {"Idempotency-Key": _key()},
    )
    assert response.status_code == 422
    assert [e["path"] for e in _error(response)["detail"]["errors"]] == ["quoteId"]


@pytest.mark.parametrize("path", ["/agentic/enrollments", "/agentic/quotes", "/agentic/checkouts"])
async def test_the_three_creates_require_an_idempotency_key(mock: Mock, path: str) -> None:
    """Omitting the key where it is required returns 422; so does one over 255 characters."""
    missing = await mock.http.post(path, json={}, headers=HEADERS)
    assert missing.status_code == 422
    assert _error(missing)["detail"]["on"] == "headers"
    too_long = await mock.http.post(path, json={}, headers=HEADERS | {"Idempotency-Key": "k" * 256})
    assert too_long.status_code == 422


async def test_a_simulate_header_value_other_than_completed_is_422(mock: Mock) -> None:
    """The OpenAPI's X-Simulate-Checkout is the constant COMPLETED."""
    response = await mock.http.post(
        "/agentic/checkouts",
        json={},
        headers=HEADERS | {"Idempotency-Key": _key(), "X-Simulate-Checkout": "FAILED"},
    )
    assert response.status_code == 422
    assert _error(response)["detail"]["on"] == "headers"


async def test_list_enrollments_validates_its_query(mock: Mock) -> None:
    """The query needs ownerId and a limit of 1 to 100, else 422 on the query."""
    for query in ("", "?ownerId=o&limit=0", "?ownerId=o&limit=101", "?ownerId=o&ownerType=X"):
        response = await mock.http.get(f"/agentic/enrollments{query}", headers=HEADERS)
        assert response.status_code == 422, query
        assert _error(response)["detail"]["on"] == "query"


async def test_enrollments_list_through_the_client(mock: Mock) -> None:
    """The client's query names reach the engine; pages follow nextCursor."""
    ids = [
        (await mock.reap.create_enrollment(_enrollment_request(), idempotency_key=_key())).id
        for _ in range(3)
    ]
    first = await mock.reap.list_enrollments("objective-1", limit=2)
    assert first.next_cursor is not None
    rest = await mock.reap.list_enrollments(
        "objective-1", owner_type="CLIENT_REFERENCE", cursor=first.next_cursor
    )
    assert [e.id for e in first.items + rest.items] == ids
    revoked = await mock.reap.revoke_enrollment(await _active_enrollment(mock))
    assert revoked.status == EnrollmentStatus.REVOKED


# Idempotency


async def test_a_replay_returns_the_first_response_marked_replayed(mock: Mock) -> None:
    """Same key, same body: the cached response with Idempotent-Replayed: true."""
    body = _enrollment_request().to_wire()
    keyed = HEADERS | {"Idempotency-Key": "enr:objective-1"}
    first = await mock.http.post("/agentic/enrollments", json=body, headers=keyed)
    replay = await mock.http.post("/agentic/enrollments", json=body, headers=keyed)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert "idempotent-replayed" not in first.headers
    assert replay.headers["idempotent-replayed"] == "true"
    other = await mock.http.post(
        "/agentic/enrollments", json=body, headers=HEADERS | {"Idempotency-Key": "enr:other"}
    )
    assert other.json()["id"] != first.json()["id"]


async def test_a_different_body_under_a_key_is_a_parameter_mismatch(mock: Mock) -> None:
    """Same key, different body: 400 IDEMPOTENT_PARAMETER_MISMATCH; the first stays cached."""
    keyed = HEADERS | {"Idempotency-Key": "enr:objective-1"}
    first = await mock.http.post(
        "/agentic/enrollments", json=_enrollment_request("a").to_wire(), headers=keyed
    )
    changed = await mock.http.post(
        "/agentic/enrollments", json=_enrollment_request("b").to_wire(), headers=keyed
    )
    assert (changed.status_code, _error(changed)["code"]) == (400, "IDEMPOTENT_PARAMETER_MISMATCH")
    again = await mock.http.post(
        "/agentic/enrollments", json=_enrollment_request("a").to_wire(), headers=keyed
    )
    assert again.json() == first.json()


async def test_errors_are_cached_except_401_422_and_429(mock: Mock) -> None:
    """A business error replays under its key; a 422 does not, so the fixed body runs."""
    keyed = HEADERS | {"Idempotency-Key": "quo:need-1:var_124:1"}
    sold_out = {
        "email": "a@example.com",
        "items": [{"variantId": "var_124", "quantity": 1}],
        "shippingAddress": SG_ADDRESS.to_wire(),
    }
    first = await mock.http.post("/agentic/quotes", json=sold_out, headers=keyed)
    assert _error(first)["code"] == "VARIANT_UNAVAILABLE"
    mock.engine.set_variant_available("var_124", available=True)
    replay = await mock.http.post("/agentic/quotes", json=sold_out, headers=keyed)
    assert (replay.status_code, _error(replay)["code"]) == (409, "VARIANT_UNAVAILABLE")
    assert replay.headers["idempotent-replayed"] == "true"

    invalid_key = HEADERS | {"Idempotency-Key": "quo:need-2:var_123:1"}
    no_address = {"email": "a@example.com", "items": [{"variantId": "var_123", "quantity": 1}]}
    invalid = await mock.http.post("/agentic/quotes", json=no_address, headers=invalid_key)
    assert invalid.status_code == 422
    fixed = await mock.http.post(
        "/agentic/quotes",
        json=no_address | {"shippingAddress": SG_ADDRESS.to_wire()},
        headers=invalid_key,
    )
    assert fixed.status_code == 200


async def test_a_rate_limit_is_not_cached_so_the_client_resends_the_same_key(
    mock: Mock,
) -> None:
    """429 with Retry-After; the client waits and resends the same key, which then runs."""
    mock.engine.inject_error(
        Operation.CREATE_QUOTE, AgenticErrorCode.RATE_LIMIT_EXCEEDED, retry_after_s=2
    )
    before = mock.clock()
    quote = await mock.reap.create_quote(_quote_request(), idempotency_key="quo:need-1:v:1")
    assert quote.amount_breakdown.final_amount.amount == Decimal("77")
    assert (mock.clock() - before).total_seconds() == 2


async def test_a_temporary_503_replays_under_its_key_and_a_new_key_retries(mock: Mock) -> None:
    """Reap's changelog: the same key replays the 503; a new key is a new attempt."""
    mock.engine.inject_error(
        Operation.CREATE_QUOTE, AgenticErrorCode.QUOTE_TEMPORARILY_UNAVAILABLE, retry_after_s=3
    )
    keys = iter(["quo:need-1:v:2"])
    quote = await mock.reap.create_quote(
        _quote_request(), idempotency_key="quo:need-1:v:1", retry_key=lambda: next(keys)
    )
    assert quote.amount_breakdown.final_amount.amount == Decimal("77")
    replay = await mock.http.post(
        "/agentic/quotes",
        json=_quote_request().to_wire(),
        headers=HEADERS | {"Idempotency-Key": "quo:need-1:v:1"},
    )
    assert (replay.status_code, replay.headers["retry-after"]) == (503, "3")
    assert _error(replay)["code"] == "QUOTE_TEMPORARILY_UNAVAILABLE"


async def test_a_key_expires_after_24_hours(mock: Mock) -> None:
    """Reused after 24 hours, a key is a fresh request."""
    body = _enrollment_request().to_wire()
    keyed = HEADERS | {"Idempotency-Key": "enr:objective-1"}
    first = await mock.http.post("/agentic/enrollments", json=body, headers=keyed)
    mock.clock.advance(hours=24)
    fresh = await mock.http.post("/agentic/enrollments", json=body, headers=keyed)
    assert fresh.json()["id"] != first.json()["id"]
    assert "idempotent-replayed" not in fresh.headers


async def test_a_concurrent_request_under_the_same_key_is_in_progress(mock: Mock) -> None:
    """While the first is in flight, a second with its key is 409, uncached."""
    gate = mock.engine.hold_next(Operation.CREATE_ENROLLMENT)
    body = _enrollment_request().to_wire()
    keyed = HEADERS | {"Idempotency-Key": "enr:objective-1"}
    first = asyncio.create_task(mock.http.post("/agentic/enrollments", json=body, headers=keyed))
    for _ in range(100):
        await asyncio.sleep(0)
    second = await mock.http.post("/agentic/enrollments", json=body, headers=keyed)
    assert (second.status_code, _error(second)["code"]) == (409, "IDEMPOTENCY_REQUEST_IN_PROGRESS")
    gate.set()
    done = await first
    assert done.status_code == 200
    replay = await mock.http.post("/agentic/enrollments", json=body, headers=keyed)
    assert replay.json() == done.json()


async def test_an_optional_key_is_honoured_when_sent(mock: Mock) -> None:
    """Every other POST honours a key when present: a search replays its search id."""
    keyed = HEADERS | {"Idempotency-Key": "search-1"}
    first = await mock.http.post("/agentic/products/search", json={"query": "fan"}, headers=keyed)
    replay = await mock.http.post("/agentic/products/search", json={"query": "fan"}, headers=keyed)
    assert replay.json()["id"] == first.json()["id"]
    unkeyed = await mock.http.post(
        "/agentic/products/search", json={"query": "fan"}, headers=HEADERS
    )
    assert unkeyed.json()["id"] != first.json()["id"]


async def test_a_dropped_create_is_settled_by_replaying_its_key(mock: Mock) -> None:
    """The checkout ran but its response was lost: maybe_sent, then the same key settles it."""
    enrollment_id = await _active_enrollment(mock)
    quote = await mock.reap.create_quote(_quote_request(), idempotency_key=_key())
    mock.engine.drop_next_response(Operation.CREATE_CHECKOUT)
    request = _checkout_request(quote.id, enrollment_id)
    with pytest.raises(ReapTransportError) as caught:
        await mock.reap.create_checkout(request, idempotency_key="claim-1", simulate="COMPLETED")
    assert caught.value.maybe_sent is True
    assert isinstance(caught.value.__cause__, DroppedResponseError)
    settled = await mock.reap.create_checkout(
        request, idempotency_key="claim-1", simulate="COMPLETED"
    )
    assert settled.status == CheckoutStatus.COMPLETED
    read = await mock.reap.get_checkout(settled.id)
    assert read.order_id is not None


# Every documented code, through the real client

_UUID = "0b8c3d1e-5f4a-4b6c-8d7e-9f0a1b2c3d4e"

_CALLS: dict[Operation, Callable[[ReapHttpClient], Awaitable[object]]] = {
    Operation.LIST_ENROLLMENTS: lambda r: r.list_enrollments("objective-1"),
    Operation.CREATE_ENROLLMENT: lambda r: r.create_enrollment(
        _enrollment_request(), idempotency_key=_key()
    ),
    Operation.GET_ENROLLMENT: lambda r: r.get_enrollment(_UUID),
    Operation.REVOKE_ENROLLMENT: lambda r: r.revoke_enrollment(_UUID),
    Operation.SEARCH: lambda r: r.search_products(ProductSearchRequest(query="fan")),
    Operation.DETAILS: lambda r: r.product_details(ProductDetailsRequest(product_ids=["p"])),
    Operation.VARIANT: lambda r: r.resolve_variant(
        ResolveVariantRequest(product_id="p", option_ids=["o"])
    ),
    Operation.CREATE_QUOTE: lambda r: r.create_quote(_quote_request(), idempotency_key=_key()),
    Operation.GET_QUOTE: lambda r: r.get_quote(_UUID),
    Operation.SELECT_SHIPPING_OPTION: lambda r: r.select_shipping_option(
        _UUID, SelectShippingOptionRequest(shipping_option_id="s"), idempotency_key=None
    ),
    Operation.CREATE_CHECKOUT: lambda r: r.create_checkout(
        _checkout_request(_UUID, _UUID), idempotency_key=_key()
    ),
    Operation.GET_CHECKOUT: lambda r: r.get_checkout(_UUID),
}


def test_every_operation_has_a_call() -> None:
    """The table below covers the twelve operations."""
    assert set(_CALLS) == set(Operation)


@pytest.mark.parametrize(
    ("operation", "code"),
    [(op, code) for op, table in DOCUMENTED_ERRORS.items() for code in table],
)
async def test_every_documented_code_reaches_the_client(
    mock: Mock, operation: Operation, code: str
) -> None:
    """Each operation's codes arrive at the client with their status and Retry-After."""
    mock.engine.inject_error(operation, code)
    with pytest.raises(ReapError) as caught:
        await _CALLS[operation](mock.reap)
    assert caught.value.code == code
    assert caught.value.status == DOCUMENTED_ERRORS[operation][code]
    expected = 1.0 if code in RETRY_AFTER_CODES else None
    assert caught.value.retry_after_s == expected


async def test_an_injected_detail_reaches_the_client(mock: Mock) -> None:
    """A reason the mock never produces by itself is still reachable, with its detail."""
    mock.engine.inject_error(
        Operation.CREATE_QUOTE, AgenticErrorCode.CHECKOUT_URL_INVALID, detail={"reason": "EXPIRED"}
    )
    with pytest.raises(ReapError) as caught:
        await mock.reap.create_quote(_quote_request(), idempotency_key=_key())
    assert caught.value.detail == {"reason": "EXPIRED"}


async def test_a_project_without_agentic_payments_is_403(mock: Mock) -> None:
    """Every agentic call answers AGENTIC_PAYMENTS_NOT_ENABLED."""
    mock.engine.enabled = False
    with pytest.raises(ReapError) as caught:
        await mock.reap.search_products(ProductSearchRequest(query="fan"))
    assert (caught.value.status, caught.value.code) == (403, "AGENTIC_PAYMENTS_NOT_ENABLED")


async def test_production_rejects_the_sandbox_header_over_http(clock: ManualClock) -> None:
    """The mock told it is production refuses X-Simulate-Checkout."""
    engine = AgenticMockEngine(
        clock=clock, config=AgenticMockConfig(environment=Environment.PRODUCTION)
    )
    app = create_mock_app(MockReapEngine(clock=clock), clock, agentic=engine)
    reap = ReapHttpClient(
        base_url=MOCK_BASE_URL,
        api_key=SecretStr("k"),
        backend="mock",
        transport=httpx.ASGITransport(app=app),
    )
    try:
        with pytest.raises(ReapError) as caught:
            await reap.create_checkout(
                _checkout_request(_UUID, _UUID), idempotency_key=_key(), simulate="COMPLETED"
            )
        assert (caught.value.status, caught.value.code) == (400, "AGENTIC_REQUEST_REJECTED")
    finally:
        await reap.aclose()


# Hosted pages


async def test_the_card_entry_page_refuses_a_wrong_card_and_keeps_waiting(mock: Mock) -> None:
    """A wrong card re-renders the form with the reason; the enrollment still waits."""
    created = await mock.reap.create_enrollment(_enrollment_request(), idempotency_key=_key())
    wrong = await mock.http.post(_card_entry(created), data=CARD | {"cvc": "000"})
    assert wrong.status_code == 400
    assert "test card" in wrong.text
    assert "<form" in wrong.text
    read = await mock.reap.get_enrollment(created.id)
    assert read.status == EnrollmentStatus.REQUIRES_ACTION


async def test_a_hosted_page_escapes_what_it_shows(mock: Mock) -> None:
    """The owner's email and the return URL are escaped into the page."""
    created = await mock.reap.create_enrollment(
        CreateExternalEnrollmentRequest(
            owner=ClientReferenceOwner(id="o", email="a.b@example.com"),
            presentation=Presentation(return_url="https://example.com/r?a=1&b=<2>"),
        ),
        idempotency_key=_key(),
    )
    page = await mock.http.get(_card_entry(created))
    assert "&lt;2&gt;" in page.text
    assert "<2>" not in page.text


async def test_hosted_pages_need_no_api_key_and_404_an_unknown_id(mock: Mock) -> None:
    """A browser opens them without Reap's headers; an unknown id is a 404 page."""
    for path in ("/hosted/enrollments/missing", "/hosted/checkouts/missing"):
        response = await mock.http.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")


async def test_a_checkout_page_cannot_approve_twice(mock: Mock) -> None:
    """Once approved, the page shows the status and a second approval is refused."""
    enrollment_id = await _active_enrollment(mock)
    quote = await mock.reap.create_quote(_quote_request(), idempotency_key=_key())
    created = await mock.reap.create_checkout(
        _checkout_request(quote.id, enrollment_id), idempotency_key=_key()
    )
    assert created.next_action is not None
    first = await mock.http.post(f"{created.next_action.url}/approve")
    assert first.status_code == 303
    second = await mock.http.post(f"{created.next_action.url}/approve")
    assert second.status_code == 400
    assert "PROCESSING" in second.text
    page = await mock.http.get(created.next_action.url)
    assert "PROCESSING" in page.text
    assert "<form" not in page.text


async def test_field_names_are_read_by_their_wire_names_only(mock: Mock) -> None:
    """Reap ignores fields it does not know, so a snake_case name is a missing field: 422."""
    body = _quote_request().to_wire()
    body["items"] = [{"variant_id": "var-northwind-nvme-1tb", "quantity": 1}]
    quote = await mock.http.post(
        "/agentic/quotes", json=body, headers=HEADERS | {"Idempotency-Key": _key()}
    )
    assert quote.status_code == 422
    assert [e["path"] for e in _error(quote)["detail"]["errors"]] == ["items.0.variantId"]
    listed = await mock.http.get("/agentic/enrollments?owner_id=o", headers=HEADERS)
    assert listed.status_code == 422

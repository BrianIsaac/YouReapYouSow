"""Tests for the agentic half of the HTTP client over ``httpx.MockTransport``.

Every agentic operation is sent at least once and checked for its path, Reap's headers, its
idempotency key and its body. Responses are Reap's own examples from
``tests/reap/agentic_examples.py``.
"""

import json
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, TypeAdapter

from tests.reap import agentic_examples as ex
from youreapyousow.reap.client import (
    SG_SANDBOX_URL,
    CheckoutPollTimeoutError,
    ReapError,
    ReapHttpClient,
    ReapResponseError,
    ReapTransportError,
    RetryPolicy,
)
from youreapyousow.reap.models import (
    REAP_VERSION,
    Checkout,
    CheckoutCreated,
    CheckoutStatus,
    CreateCheckoutRequest,
    CreateEnrollmentRequest,
    CreateQuoteRequest,
    Enrollment,
    ExternalEnrollmentCreated,
    Page,
    ProductDetailsRequest,
    ProductDetailsResponse,
    ProductSearchRequest,
    ProductSearchResponse,
    Quote,
    ResolveVariantRequest,
    SelectShippingOptionRequest,
    Variant,
)

pytestmark = pytest.mark.anyio

QUOTE_ID = "3f1c2b9a-7d4e-4c1a-9b2f-0e6d5a4c3b21"
ENROLLMENT_ID = "8a6e0f3d-2c1b-4e9a-8f7d-6c5b4a3e2d10"


@dataclass
class FakeTime:
    """A monotonic clock that only moves when the client sleeps.

    Attributes:
        now: Seconds since the start.
        slept: Every wait the client asked for, in order.
    """

    now: float = 0.0
    slept: list[float] = field(default_factory=list[float])

    def monotonic(self) -> float:
        """Return the current time.

        Returns:
            Seconds since the start.
        """
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Record the wait and move time on by it.

        Args:
            seconds: How long the client asked to wait.
        """
        self.slept.append(seconds)
        self.now += seconds


type Handler = Callable[[httpx.Request], httpx.Response]


def _client(
    handler: Handler, time: FakeTime | None = None, retry: RetryPolicy | None = None
) -> ReapHttpClient:
    time = time or FakeTime()
    return ReapHttpClient(
        base_url=SG_SANDBOX_URL,
        api_key=SecretStr("sk_test"),
        backend="sandbox",
        transport=httpx.MockTransport(handler),
        retry=retry or RetryPolicy(),
        sleep=time.sleep,
        monotonic=time.monotonic,
    )


def _error(
    status: int, code: str, *, retry_after: str | None = None, detail: dict[str, str] | None = None
) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    body = {"error": {"code": code, "message": code.lower(), "detail": detail}}
    return httpx.Response(status, json=body, headers=headers)


def _replies(*responses: httpx.Response) -> tuple[Handler, list[httpx.Request]]:
    """Answer each request with the next response, recording what was sent."""
    seen: list[httpx.Request] = []
    queue: Iterator[httpx.Response] = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(queue)

    return handler, seen


def _checkout(status: str, **overrides: object) -> dict[str, Any]:
    body = ex.load(ex.CHECKOUT_RESPONSE) | {"status": status}
    if status != "COMPLETED":
        body |= {"orderId": None}
    return body | overrides


def _quote_request() -> CreateQuoteRequest:
    return TypeAdapter[CreateQuoteRequest](CreateQuoteRequest).validate_python(
        ex.load(ex.QUOTE_ITEMS_REQUEST)
    )


def _checkout_request() -> CreateCheckoutRequest:
    raw = ex.load(ex.CHECKOUT_REQUEST) | {"quoteId": QUOTE_ID, "enrollmentId": ENROLLMENT_ID}
    return CreateCheckoutRequest.model_validate(raw)


@dataclass(frozen=True)
class Operation:
    """One agentic call and what it must put on the wire.

    Attributes:
        name: Test id.
        call: Sends the request through a client.
        method: Expected HTTP method.
        path: Expected path.
        key: Expected ``Idempotency-Key``, or None for none.
        body: Expected JSON body, or None for none.
        response: The example the mock answers with.
        result: The type the client must return.
    """

    name: str
    call: Callable[[ReapHttpClient], Awaitable[object]]
    method: str
    path: str
    key: str | None
    body: dict[str, Any] | None
    response: str
    result: type


_ENROLLMENT_REQUEST = TypeAdapter[CreateEnrollmentRequest](CreateEnrollmentRequest)

OPERATIONS = [
    Operation(
        "create_enrollment",
        lambda c: c.create_enrollment(
            _ENROLLMENT_REQUEST.validate_python(ex.load(ex.ENROLLMENT_EXTERNAL_REQUEST)),
            idempotency_key="enr:obj-1",
        ),
        "POST",
        "/agentic/enrollments",
        "enr:obj-1",
        ex.load(ex.ENROLLMENT_EXTERNAL_REQUEST),
        ex.ENROLLMENT_EXTERNAL_RESPONSE,
        ExternalEnrollmentCreated,
    ),
    Operation(
        "get_enrollment",
        lambda c: c.get_enrollment(ENROLLMENT_ID),
        "GET",
        f"/agentic/enrollments/{ENROLLMENT_ID}",
        None,
        None,
        ex.ENROLLMENT_RESPONSE,
        Enrollment,
    ),
    Operation(
        "list_enrollments",
        lambda c: c.list_enrollments("cust-1"),
        "GET",
        "/agentic/enrollments",
        None,
        None,
        ex.ENROLLMENT_LIST_RESPONSE,
        Page,
    ),
    Operation(
        "revoke_enrollment",
        lambda c: c.revoke_enrollment(ENROLLMENT_ID),
        "POST",
        f"/agentic/enrollments/{ENROLLMENT_ID}/revoke",
        None,
        None,
        ex.ENROLLMENT_REVOKED_RESPONSE,
        Enrollment,
    ),
    Operation(
        "search_products",
        lambda c: c.search_products(
            ProductSearchRequest.model_validate(ex.load(ex.SEARCH_REQUEST))
        ),
        "POST",
        "/agentic/products/search",
        None,
        ex.load(ex.SEARCH_REQUEST),
        ex.SEARCH_RESPONSE,
        ProductSearchResponse,
    ),
    Operation(
        "product_details",
        lambda c: c.product_details(
            ProductDetailsRequest.model_validate(ex.load(ex.DETAILS_REQUEST))
        ),
        "POST",
        "/agentic/products/details",
        None,
        ex.load(ex.DETAILS_REQUEST),
        ex.DETAILS_RESPONSE,
        ProductDetailsResponse,
    ),
    Operation(
        "resolve_variant",
        lambda c: c.resolve_variant(
            ResolveVariantRequest.model_validate(ex.load(ex.VARIANT_REQUEST))
        ),
        "POST",
        "/agentic/products/variant",
        None,
        ex.load(ex.VARIANT_REQUEST),
        ex.VARIANT_RESPONSE,
        Variant,
    ),
    Operation(
        "create_quote",
        lambda c: c.create_quote(_quote_request(), idempotency_key="quo:need-1:var_123:1"),
        "POST",
        "/agentic/quotes",
        "quo:need-1:var_123:1",
        ex.load(ex.QUOTE_ITEMS_REQUEST),
        ex.QUOTE_RESPONSE,
        Quote,
    ),
    Operation(
        "get_quote",
        lambda c: c.get_quote(QUOTE_ID),
        "GET",
        f"/agentic/quotes/{QUOTE_ID}",
        None,
        None,
        ex.QUOTE_RESPONSE,
        Quote,
    ),
    Operation(
        "select_shipping_option",
        lambda c: c.select_shipping_option(
            QUOTE_ID,
            SelectShippingOptionRequest.model_validate(ex.load(ex.SHIPPING_OPTION_REQUEST)),
            idempotency_key=f"shp:{QUOTE_ID}:std",
        ),
        "POST",
        f"/agentic/quotes/{QUOTE_ID}/shipping-option",
        f"shp:{QUOTE_ID}:std",
        ex.load(ex.SHIPPING_OPTION_REQUEST),
        ex.SHIPPING_OPTION_RESPONSE,
        Quote,
    ),
    Operation(
        "create_checkout",
        lambda c: c.create_checkout(_checkout_request(), idempotency_key="pur:intent-1:claim-1"),
        "POST",
        "/agentic/checkouts",
        "pur:intent-1:claim-1",
        _checkout_request().to_wire(),
        ex.CHECKOUT_CREATED_RESPONSE,
        CheckoutCreated,
    ),
    Operation(
        "get_checkout",
        lambda c: c.get_checkout("chk-1"),
        "GET",
        "/agentic/checkouts/chk-1",
        None,
        None,
        ex.CHECKOUT_RESPONSE,
        Checkout,
    ),
]


@pytest.mark.parametrize("op", OPERATIONS, ids=[op.name for op in OPERATIONS])
async def test_every_agentic_operation_sends_its_documented_request(op: Operation) -> None:
    """Path, bearer key, ``Reap-Version``, the idempotency key on creates, and the body."""
    handler, seen = _replies(httpx.Response(200, json=ex.load(op.response)))
    result = await op.call(_client(handler))
    assert isinstance(result, op.result)
    (request,) = seen
    assert (request.method, request.url.path) == (op.method, op.path)
    assert request.url.host == httpx.URL(SG_SANDBOX_URL).host
    assert request.headers["Authorization"] == "Bearer sk_test"
    assert request.headers["Reap-Version"] == REAP_VERSION == "2025-02-14"
    assert request.headers.get("Idempotency-Key") == op.key
    assert "X-Simulate-Checkout" not in request.headers
    if op.body is None:
        assert request.content == b""
    else:
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == op.body


async def test_list_enrollments_sends_reaps_query_names() -> None:
    """``ownerId`` always; ``ownerType``, ``limit`` and ``cursor`` only when given."""
    handler, seen = _replies(
        httpx.Response(200, json=ex.load(ex.ENROLLMENT_LIST_RESPONSE)),
        httpx.Response(200, json=ex.load(ex.ENROLLMENT_LIST_RESPONSE)),
    )
    client = _client(handler)
    page = await client.list_enrollments("cust-1")
    await client.list_enrollments("u-1", owner_type="REAP_USER", limit=20, cursor="c2")
    assert page.items[0].status == "ACTIVE"
    assert dict(seen[0].url.params) == {"ownerId": "cust-1"}
    assert dict(seen[1].url.params) == {
        "ownerId": "u-1",
        "ownerType": "REAP_USER",
        "limit": "20",
        "cursor": "c2",
    }


async def test_simulate_checkout_header_is_sent_only_when_asked() -> None:
    """``X-Simulate-Checkout: COMPLETED`` goes on the checkout create when the caller asks."""
    handler, seen = _replies(
        httpx.Response(200, json=ex.load(ex.CHECKOUT_CREATED_RESPONSE)),
    )
    await _client(handler).create_checkout(
        _checkout_request(), idempotency_key="pur:i:c", simulate="COMPLETED"
    )
    assert seen[0].headers["X-Simulate-Checkout"] == "COMPLETED"
    assert seen[0].headers["Idempotency-Key"] == "pur:i:c"


@pytest.mark.parametrize("key", ["", "k" * 256])
async def test_idempotency_key_must_be_1_to_255_characters(key: str) -> None:
    """Reap's bound on the key is checked before anything is sent."""
    handler, seen = _replies()
    with pytest.raises(ValueError, match="Idempotency-Key"):
        await _client(handler).create_checkout(_checkout_request(), idempotency_key=key)
    assert seen == []


async def test_error_body_maps_to_reap_error_with_its_detail() -> None:
    """Code, message and detail are carried, so callers branch on ``code``, not status."""
    handler, _ = _replies(_error(400, "CHECKOUT_URL_INVALID", detail={"reason": "EXPIRED"}))
    with pytest.raises(ReapError) as error:
        await _client(handler).create_quote(_quote_request(), idempotency_key="k")
    assert (error.value.status, error.value.code) == (400, "CHECKOUT_URL_INVALID")
    assert error.value.detail == {"reason": "EXPIRED"}
    assert error.value.retry_after_s is None


async def test_rate_limit_waits_retry_after_and_resends_the_same_key() -> None:
    """A 429 is not cached, so the same request and key go again after ``Retry-After``."""
    time = FakeTime()
    handler, seen = _replies(
        _error(429, "RATE_LIMIT_EXCEEDED", retry_after="2"),
        httpx.Response(200, json=ex.load(ex.CHECKOUT_CREATED_RESPONSE)),
    )
    created = await _client(handler, time).create_checkout(
        _checkout_request(), idempotency_key="k1"
    )
    assert created.status is CheckoutStatus.REQUIRES_ACTION
    assert time.slept == [2.0]
    assert [r.headers["Idempotency-Key"] for r in seen] == ["k1", "k1"]
    assert seen[0].content == seen[1].content


async def test_rate_limit_retries_are_bounded() -> None:
    """After the policy's retries the 429 surfaces as ``ReapError`` with its wait."""
    time = FakeTime()
    handler, seen = _replies(*[_error(429, "RATE_LIMIT_EXCEEDED", retry_after="1")] * 4)
    with pytest.raises(ReapError) as error:
        await _client(handler, time, RetryPolicy(rate_limit_retries=3)).get_checkout("chk-1")
    assert error.value.code == "RATE_LIMIT_EXCEEDED"
    assert error.value.retry_after_s == 1.0
    assert (len(seen), time.slept) == (4, [1.0, 1.0, 1.0])


async def test_a_wait_longer_than_the_bound_is_not_taken() -> None:
    """A ``Retry-After`` beyond ``max_wait_s`` surfaces at once for the caller to decide."""
    time = FakeTime()
    handler, seen = _replies(_error(429, "RATE_LIMIT_EXCEEDED", retry_after="3600"))
    with pytest.raises(ReapError) as error:
        await _client(handler, time, RetryPolicy(max_wait_s=10)).get_quote(QUOTE_ID)
    assert error.value.retry_after_s == 3600.0
    assert (len(seen), time.slept) == (1, [])


@pytest.mark.parametrize("header", [None, "soon", "-5"])
async def test_a_429_without_a_usable_retry_after_waits_the_default(header: str | None) -> None:
    """No header, or one that is not a number of seconds, waits ``default_wait_s``."""
    time = FakeTime()
    handler, _ = _replies(
        _error(429, "RATE_LIMIT_EXCEEDED", retry_after=header),
        httpx.Response(200, json=ex.load(ex.QUOTE_RESPONSE)),
    )
    await _client(handler, time, RetryPolicy(default_wait_s=1.5)).get_quote(QUOTE_ID)
    assert time.slept == [1.5]


async def test_quote_temporarily_unavailable_retries_under_a_fresh_key() -> None:
    """The 503 is cached under its key, so each retry waits ``Retry-After`` and mints a new key."""
    time = FakeTime()
    handler, seen = _replies(
        _error(503, "QUOTE_TEMPORARILY_UNAVAILABLE", retry_after="3"),
        httpx.Response(200, json=ex.load(ex.QUOTE_RESPONSE)),
    )
    minted = iter(["quo:n:v:2"])
    quote = await _client(handler, time).create_quote(
        _quote_request(), idempotency_key="quo:n:v:1", retry_key=lambda: next(minted)
    )
    assert quote.amount_breakdown.final_amount.amount == 142
    assert time.slept == [3.0]
    assert [r.headers["Idempotency-Key"] for r in seen] == ["quo:n:v:1", "quo:n:v:2"]


async def test_quote_retries_are_bounded() -> None:
    """After ``quote_retries`` fresh attempts the 503 surfaces as ``ReapError``."""
    time = FakeTime()
    handler, seen = _replies(*[_error(503, "QUOTE_TEMPORARILY_UNAVAILABLE", retry_after="1")] * 3)
    keys = iter(["k2", "k3"])
    with pytest.raises(ReapError) as error:
        await _client(handler, time, RetryPolicy(quote_retries=2)).create_quote(
            _quote_request(), idempotency_key="k1", retry_key=lambda: next(keys)
        )
    assert error.value.code == "QUOTE_TEMPORARILY_UNAVAILABLE"
    assert [r.headers["Idempotency-Key"] for r in seen] == ["k1", "k2", "k3"]
    assert time.slept == [1.0, 1.0]


async def test_quote_without_a_retry_key_surfaces_the_503_and_its_wait() -> None:
    """Without a way to mint a new key the client cannot retry; it says how long to wait."""
    time = FakeTime()
    handler, seen = _replies(_error(503, "QUOTE_TEMPORARILY_UNAVAILABLE", retry_after="4"))
    with pytest.raises(ReapError) as error:
        await _client(handler, time).create_quote(_quote_request(), idempotency_key="k1")
    assert (error.value.retry_after_s, len(seen), time.slept) == (4.0, 1, [])


async def test_checkout_temporarily_unavailable_is_never_retried_in_the_client() -> None:
    """It needs a fresh quote and a new claim, which only the control plane can make."""
    time = FakeTime()
    handler, seen = _replies(_error(503, "CHECKOUT_TEMPORARILY_UNAVAILABLE", retry_after="2"))
    with pytest.raises(ReapError) as error:
        await _client(handler, time).create_checkout(_checkout_request(), idempotency_key="k")
    assert (error.value.code, error.value.retry_after_s) == (
        "CHECKOUT_TEMPORARILY_UNAVAILABLE",
        2.0,
    )
    assert (len(seen), time.slept) == (1, [])


@pytest.mark.parametrize(
    ("exc", "maybe_sent"),
    [
        (httpx.ConnectError("refused"), False),
        (httpx.ConnectTimeout("slow"), False),
        (httpx.ReadTimeout("no answer"), True),
        (httpx.RemoteProtocolError("reset"), True),
    ],
)
async def test_checkout_transport_failures_say_whether_money_may_have_moved(
    exc: httpx.HTTPError, maybe_sent: bool
) -> None:
    """A connect failure never reached Reap; a read timeout after sending may have."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(ReapTransportError) as error:
        await _client(handler).create_checkout(_checkout_request(), idempotency_key="k")
    assert error.value.maybe_sent is maybe_sent


async def test_an_unreadable_success_is_an_unknown_outcome() -> None:
    """A 2xx body the models cannot read was certainly sent; its outcome is unknown."""
    handler, _ = _replies(httpx.Response(200, json={"id": "chk-1"}))
    with pytest.raises(ReapResponseError) as error:
        await _client(handler).create_checkout(_checkout_request(), idempotency_key="k")
    assert isinstance(error.value, ReapTransportError)
    assert error.value.maybe_sent is True


@pytest.mark.parametrize("final", ["COMPLETED", "FAILED", "EXPIRED", "REQUIRES_ACTION"])
async def test_poll_checkout_returns_on_each_of_its_four_exits(final: str) -> None:
    """Three final statuses and ``REQUIRES_ACTION`` end the poll; ``PROCESSING`` does not."""
    time = FakeTime()
    handler, seen = _replies(
        httpx.Response(200, json=_checkout("PROCESSING")),
        httpx.Response(200, json=_checkout("PROCESSING")),
        httpx.Response(200, json=_checkout(final)),
    )
    checkout = await _client(handler, time).poll_checkout("chk-1", every_s=1, deadline_s=120)
    assert checkout.status == final
    assert len(seen) == 3
    assert time.slept == [1.0, 2.0]


async def test_poll_checkout_reads_straight_away() -> None:
    """A checkout already completed is returned without a wait."""
    time = FakeTime()
    handler, _ = _replies(httpx.Response(200, json=_checkout("COMPLETED")))
    checkout = await _client(handler, time).poll_checkout("chk-1", every_s=1, deadline_s=120)
    assert (checkout.order_id, time.slept) == ("<merchant-order-id>", [])


async def test_poll_checkout_backs_off_to_its_ceiling_and_times_out_at_the_deadline() -> None:
    """Still ``PROCESSING`` at the deadline: a typed timeout carrying the last read."""
    time = FakeTime()
    reads = iter([_error(503, "AGENTIC_SERVICE_UNAVAILABLE")])

    def handler(_: httpx.Request) -> httpx.Response:
        return next(reads, httpx.Response(200, json=_checkout("PROCESSING")))

    with pytest.raises(CheckoutPollTimeoutError) as timeout:
        await _client(handler, time).poll_checkout("chk-1", every_s=1, deadline_s=20, max_every_s=5)
    assert time.slept == [1.0, 2.0, 4.0, 5.0, 5.0, 3.0]
    assert time.now == 20.0
    assert timeout.value.checkout_id == "chk-1"
    assert timeout.value.last is not None
    assert timeout.value.last.status is CheckoutStatus.PROCESSING
    assert timeout.value.__cause__ is None


async def test_poll_checkout_rides_out_transient_read_failures() -> None:
    """A dropped read or ``AGENTIC_SERVICE_UNAVAILABLE`` is retried until the deadline."""
    time = FakeTime()
    responses = iter(
        [
            _error(503, "AGENTIC_SERVICE_UNAVAILABLE"),
            None,
            httpx.Response(200, json=_checkout("COMPLETED")),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        response = next(responses)
        if response is None:
            raise httpx.ReadTimeout("no answer")
        return response

    checkout = await _client(handler, time).poll_checkout("chk-1", every_s=1, deadline_s=60)
    assert checkout.status is CheckoutStatus.COMPLETED
    assert time.slept == [1.0, 2.0]


async def test_poll_checkout_times_out_with_no_read_when_reap_never_answers() -> None:
    """If no read ever succeeded the timeout says so, and carries the last error."""
    time = FakeTime()

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(CheckoutPollTimeoutError) as timeout:
        await _client(handler, time).poll_checkout("chk-1", every_s=2, deadline_s=5)
    assert timeout.value.last is None
    assert isinstance(timeout.value.__cause__, ReapTransportError)


async def test_poll_checkout_lets_a_definite_error_through() -> None:
    """``CHECKOUT_NOT_FOUND`` is not transient; polling stops and it surfaces."""
    handler, _ = _replies(_error(404, "CHECKOUT_NOT_FOUND"))
    with pytest.raises(ReapError) as error:
        await _client(handler).poll_checkout("chk-1", every_s=1, deadline_s=60)
    assert error.value.code == "CHECKOUT_NOT_FOUND"


@pytest.mark.parametrize(
    ("every_s", "deadline_s", "max_every_s"), [(0, 10, 5), (1, -1, 5), (2, 10, 1)]
)
async def test_poll_checkout_refuses_nonsense_timing(
    every_s: float, deadline_s: float, max_every_s: float
) -> None:
    """A zero interval, a negative deadline or a ceiling below the interval is a caller bug."""
    handler, seen = _replies()
    with pytest.raises(ValueError, match="poll"):
        await _client(handler).poll_checkout(
            "chk-1", every_s=every_s, deadline_s=deadline_s, max_every_s=max_every_s
        )
    assert seen == []

"""Tests for the HTTP client: headers, errors, transport failures and backends."""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from tests.conftest import START
from tests.reap.conftest import Funded
from youreapyousow.clock import ManualClock
from youreapyousow.reap.client import (
    SG_SANDBOX_URL,
    ReapError,
    ReapHttpClient,
    ReapMock,
    ReapSandbox,
    ReapTransportError,
)
from youreapyousow.reap.mock.agentic import AgenticMockEngine, bundled_catalogue
from youreapyousow.reap.models import (
    REAP_VERSION,
    CreateAccountRequest,
    ProductSearchRequest,
    ScopeType,
    SimulateAuthorizationRequest,
)

pytestmark = pytest.mark.anyio


def _client(handler: httpx.MockTransport) -> ReapHttpClient:
    return ReapHttpClient(
        base_url=SG_SANDBOX_URL, api_key=SecretStr("sk_test"), backend="sandbox", transport=handler
    )


async def test_sends_reap_headers_and_idempotency_key() -> None:
    """Every request carries the bearer key and version; money-moving ones the key."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "a",
                "status": "ACTIVE",
                "ownerType": "USER",
                "ownerId": "u",
                "chainAddresses": [],
                "createdAt": "x",
                "updatedAt": "x",
            },
        )

    client = _client(httpx.MockTransport(handler))
    account = await client.create_account(CreateAccountRequest(owner_id="u"), idempotency_key="k-1")
    assert account.id == "a"
    request = seen[0]
    assert request.url == httpx.URL(f"{SG_SANDBOX_URL}/accounts/")
    assert request.headers["Authorization"] == "Bearer sk_test"
    assert request.headers["Reap-Version"] == REAP_VERSION
    assert request.headers["Idempotency-Key"] == "k-1"
    assert json.loads(request.content) == {"ownerId": "u"}


async def test_error_body_maps_to_reap_error() -> None:
    """Reap's error envelope becomes a typed exception; garbage bodies still map."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/freeze"):
            return httpx.Response(502, text="<html>bad gateway</html>")
        return httpx.Response(
            404,
            json={"error": {"code": "CARD_NOT_FOUND", "message": "Card not found", "detail": None}},
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ReapError) as error:
        await client.get_card("c")
    assert (error.value.status, error.value.code) == (404, "CARD_NOT_FOUND")
    with pytest.raises(ReapError) as garbled:
        await client.freeze_card("c")
    assert garbled.value.code == "UNPARSEABLE_ERROR"


@pytest.mark.parametrize(
    ("exc", "maybe_sent"),
    [
        (httpx.ConnectError("refused"), False),
        (httpx.ConnectTimeout("slow"), False),
        (httpx.ReadTimeout("no answer"), True),
        (httpx.RemoteProtocolError("reset"), True),
    ],
)
async def test_transport_failures_say_whether_money_may_have_moved(
    exc: httpx.HTTPError, maybe_sent: bool
) -> None:
    """Only a failure before connecting is known not to have reached Reap."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ReapTransportError) as error:
        await client.simulate_authorization(
            SimulateAuthorizationRequest(card_id="c", amount=Decimal(1)), idempotency_key="k"
        )
    assert error.value.maybe_sent is maybe_sent


async def test_query_parameters_use_reap_names() -> None:
    """Effective policies and activities send Reap's camelCase query names."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        body: dict[str, object] = {"items": []}
        if "activities" in request.url.path:
            body["nextCursor"] = None
        return httpx.Response(200, json=body)

    client = _client(httpx.MockTransport(handler))
    await client.effective_policies(ScopeType.CARD, "c1")
    await client.list_activities(card_id="c1", cursor="20", limit=10)
    assert dict(seen[0].params) == {"scopeType": "CARD", "scopeId": "c1"}
    assert dict(seen[1].params) == {"limit": "10", "cardId": "c1", "cursor": "20"}


async def test_backends_are_labelled() -> None:
    """The dashboard can tell the mock from the sandbox."""
    sandbox = ReapSandbox(api_key=SecretStr("sk"))
    mock = ReapMock()
    assert (sandbox.backend, mock.backend) == ("sandbox", "mock")
    await sandbox.aclose()
    await mock.aclose()


async def test_mock_activities_page_through_card_history(funded: Funded) -> None:
    """The activity feed pages newest first with a cursor."""
    for amount in ("1", "2", "3"):
        await funded.reap.simulate_authorization(
            SimulateAuthorizationRequest(card_id=funded.card.id, amount=Decimal(amount)),
            idempotency_key=None,
        )
    first = await funded.reap.list_activities(card_id=funded.card.id, cursor=None, limit=2)
    assert len(first.items) == 2
    assert first.next_cursor == "2"
    second = await funded.reap.list_activities(
        card_id=funded.card.id, cursor=first.next_cursor, limit=2
    )
    assert len(second.items) == 1
    assert second.next_cursor is None
    assert all(a.type == "CARD_TRANSACTION" for a in [*first.items, *second.items])
    everything = await funded.reap.list_activities(card_id=None, cursor=None, limit=100)
    assert {a.type for a in everything.items} == {"CARD_TRANSACTION", "FIAT_DEPOSIT"}


async def test_the_mock_backend_exposes_its_agentic_engine() -> None:
    """The app and the demo reach the agentic mock's hosted steps through ``ReapMock``."""
    clock = ManualClock(START)
    default = ReapMock(clock=clock)
    assert isinstance(default.agentic, AgenticMockEngine)
    found = await default.search_products(ProductSearchRequest(query="1TB NVMe M.2 2280 SSD"))
    assert found.products
    await default.aclose()

    engine = AgenticMockEngine(bundled_catalogue("headphones"), clock=clock)
    chosen = ReapMock(clock=clock, agentic=engine)
    assert chosen.agentic is engine
    found = await chosen.search_products(ProductSearchRequest(query="1TB NVMe M.2 2280 SSD"))
    assert found.products == []
    await chosen.aclose()

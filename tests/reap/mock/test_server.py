"""Tests for the mock server's header, idempotency and error behaviour over raw HTTP."""

from collections.abc import AsyncIterator

import httpx
import pytest

from youreapyousow.clock import ManualClock
from youreapyousow.reap.mock.engine import MockReapEngine
from youreapyousow.reap.mock.server import create_mock_app
from youreapyousow.reap.models import REAP_VERSION

pytestmark = pytest.mark.anyio

HEADERS = {"Authorization": "Bearer k", "Reap-Version": REAP_VERSION}


@pytest.fixture
async def http(clock: ManualClock) -> AsyncIterator[httpx.AsyncClient]:
    """A raw HTTP client on the mock app.

    Yields:
        The client.
    """
    app = create_mock_app(MockReapEngine(clock=clock), clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        yield client


async def _user(http: httpx.AsyncClient) -> str:
    response = await http.post(
        "/users/", json={"email": "a@example.com", "phoneNumber": "+65"}, headers=HEADERS
    )
    return str(response.json()["id"])


async def test_bearer_key_and_version_are_required(http: httpx.AsyncClient) -> None:
    """Requests without a key or the pinned version are rejected in Reap's error shape."""
    no_key = await http.get("/users/x", headers={"Reap-Version": REAP_VERSION})
    assert no_key.status_code == 401
    assert no_key.json()["error"]["code"] == "UNAUTHORIZED"
    wrong_version = await http.get("/users/x", headers={"Authorization": "Bearer k"})
    assert wrong_version.status_code == 400
    assert set(wrong_version.json()["error"]) == {"code", "message", "detail"}


async def test_idempotency_key_required_replayed_and_conflicting(http: httpx.AsyncClient) -> None:
    """Accounts need a key; a replay returns the same account; a changed body conflicts."""
    user_id = await _user(http)
    body = {"ownerId": user_id, "ownerType": "USER"}
    missing = await http.post("/accounts/", json=body, headers=HEADERS)
    assert missing.status_code == 400

    keyed = HEADERS | {"Idempotency-Key": "k1"}
    first = await http.post("/accounts/", json=body, headers=keyed)
    replay = await http.post("/accounts/", json=body, headers=keyed)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()

    conflict = await http.post("/accounts/", json=body | {"ownerType": "COMPANY"}, headers=keyed)
    assert conflict.status_code == 409


async def test_idempotency_expires_after_24_hours(
    http: httpx.AsyncClient, clock: ManualClock
) -> None:
    """After 24 hours the same key creates a new resource."""
    user_id = await _user(http)
    keyed = HEADERS | {"Idempotency-Key": "k"}
    body = {"ownerId": user_id}
    first = (await http.post("/accounts/", json=body, headers=keyed)).json()
    clock.advance(hours=24, seconds=1)
    second = (await http.post("/accounts/", json=body, headers=keyed)).json()
    assert first["id"] != second["id"]


async def test_validation_and_not_found_errors(http: httpx.AsyncClient) -> None:
    """Bad bodies are 400 and unknown resources 404, both in Reap's error shape."""
    bad = await http.post("/users/", json={"email": "x"}, headers=HEADERS)
    assert (bad.status_code, bad.json()["error"]["code"]) == (400, "VALIDATION_ERROR")
    not_json = await http.post("/users/", content=b"{", headers=HEADERS)
    assert not_json.status_code == 400
    missing = await http.get("/cards/nope", headers=HEADERS)
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "CARD_NOT_FOUND")
    deleted = await http.delete("/cards/nope", headers=HEADERS)
    assert deleted.status_code == 404
    bad_scope = await http.get("/policies/effective?scopeType=GALAXY", headers=HEADERS)
    assert bad_scope.status_code == 400


async def test_simulated_application_returns_204(http: httpx.AsyncClient) -> None:
    """The KYC simulator answers 204 with no body, as documented."""
    user_id = await _user(http)
    response = await http.post(
        f"/simulation/users/{user_id}/application", json={"status": "APPROVED"}, headers=HEADERS
    )
    assert (response.status_code, response.content) == (204, b"")
    user = (await http.get(f"/users/{user_id}", headers=HEADERS)).json()
    assert user["application"]["status"] == "APPROVED"

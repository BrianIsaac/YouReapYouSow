"""Tests for the HTTP surface: status, webhook delivery and external authorisation."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from youreapyousow.api.app import (
    AUTHORISE_PATH,
    NOTIFY_PATH,
    Runtime,
    build_runtime,
    create_app,
    serve,
)
from youreapyousow.authority.lifecycle import LifecycleRules
from youreapyousow.config import Settings
from youreapyousow.control import GrantTerms
from youreapyousow.domain import Comparator, Constraint, IntentState, ObjectiveKind, ResourceSpec
from youreapyousow.ledger.events import EventType
from youreapyousow.market.service import MarketMode
from youreapyousow.purchase import PURCHASE_CONFIG_DIR, load_purchase
from youreapyousow.reap.client import ReapMock
from youreapyousow.reap.mock.engine import AuthorizationMode
from youreapyousow.reap.models import (
    DeclinedCardTransaction,
    SimulateAuthorizationRequest,
    SimulatedMerchant,
)
from youreapyousow.reap.webhooks import SIGNATURE_HEADER, sign

pytestmark = pytest.mark.anyio

TERMS = GrantTerms(
    allowed_providers=("vast", "runpod", "shadeform"),
    per_transaction_cap_usd=Decimal(5),
    daily_cap_usd=Decimal(20),
    ttl=timedelta(hours=4),
)

PART = load_purchase(PURCHASE_CONFIG_DIR / "purchase-part.yaml")


@dataclass
class Running:
    """A running app and its runtime.

    Attributes:
        http: Client on the app.
        runtime: The app's runtime.
    """

    http: httpx.AsyncClient
    runtime: Runtime


def _settings(tmp_path: Path, mode: AuthorizationMode = AuthorizationMode.MANAGED) -> Settings:
    return Settings.model_validate(
        {
            "database_path": tmp_path / "cp.db",
            "snapshot_path": tmp_path / "snap.json",
            "market_mode": MarketMode.MOCK,
            "reap_authorization_mode": mode,
        }
    )


@asynccontextmanager
async def _running(settings: Settings) -> AsyncGenerator[Running]:
    async def factory() -> Runtime:
        return build_runtime(settings)

    app: FastAPI = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://cp") as http:
            runtime: Runtime = app.state.runtime
            yield Running(http, runtime)


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[Running]:
    """The app on the mock stack, inside its lifespan.

    Yields:
        The running app.
    """
    async with _running(_settings(tmp_path)) as value:
        yield value


async def _purchase(runtime: Runtime) -> IntentState:
    cp = runtime.control
    objective = await cp.create_objective(
        kind=ObjectiveKind.SERVICE_FLEET,
        statement="p95 < 500 ms",
        constraints=[
            Constraint(metric="p95_latency_ms", comparator=Comparator.LT, threshold=Decimal(500))
        ],
        budget_usd=Decimal(25),
        grant=TERMS,
        lifecycle=LifecycleRules(),
    )
    offers = await cp.discover_resources(objective.id, ResourceSpec(min_vram_gb=24))
    quote = cp.request_quote(objective.id, offers[0], Decimal(1))
    intent, _ = cp.propose_purchase(
        quote_id=quote.id,
        provider=offers[0].provider,
        offer_id=offers[0].offer_id,
        amount_usd=quote.amount_usd,
        rationale="cheapest",
        options_considered=[o.key for o in offers[:3]],
    )
    return (await cp.execute_purchase(intent.id)).state


async def test_health_and_status_show_the_active_backend(running: Running) -> None:
    """The dashboard can see that Reap is mocked and where quotes come from."""
    assert (await running.http.get("/health")).json() == {"ok": True}
    response = await running.http.get("/status")
    assert response.text.startswith('{\n  "reap": {\n')
    status = response.json()
    assert status["reap"]["backend"] == "mock"
    assert status["reap"]["real_reap"] is False
    assert status["reap"]["authorization_mode"] == "MANAGED"
    assert status["market"]["mode"] == "mock"
    assert status["ledger"]["chain_intact"] is True


async def test_status_names_every_connector_on_the_mock_market(running: Running) -> None:
    """From the first request, each connector is listed with its source, never empty."""
    connectors = (await running.http.get("/status")).json()["market"]["connectors"]
    assert {"vast", "runpod", "shadeform", "akash", "ornn", "openrouter"} <= set(connectors)
    assert {c["source"] for c in connectors.values()} == {"mock"}
    assert connectors["vast"]["error"] == "market mode is mock"


async def test_mock_webhooks_reach_the_receiver_and_land_on_the_ledger(running: Running) -> None:
    """A purchase on the mock is delivered, verified and recorded against its objective."""
    assert await _purchase(running.runtime) == IntentState.AUTHORISED
    received = running.runtime.control.ledger.events(types=[EventType.REAP_WEBHOOK_RECEIVED])
    tx_events = [e for e in received if e.payload["type"] == "CARD_TRANSACTION_CREATED"]
    assert len(tx_events) == 1
    assert tx_events[0].objective_id is not None
    assert "reap_transaction" in tx_events[0].refs


async def test_receiver_rejects_bad_signatures_and_deduplicates(running: Running) -> None:
    """Forged deliveries are refused; a redelivered event is recorded once."""
    body = b'{"id":"evt_1","type":"CARD_STATUS_UPDATED","data":{"id":"card_x"}}'
    forged = await running.http.post(
        NOTIFY_PATH, content=body, headers={SIGNATURE_HEADER: "t=1,v1=00"}
    )
    assert forged.status_code == 401
    secret = running.runtime.secrets[NOTIFY_PATH].get_secret_value()
    header = {SIGNATURE_HEADER: sign(secret, body, int(running.runtime.clock().timestamp()))}
    first = await running.http.post(NOTIFY_PATH, content=body, headers=header)
    again = await running.http.post(NOTIFY_PATH, content=body, headers=header)
    assert first.json() == {"received": True, "duplicate": False}
    assert again.json() == {"received": True, "duplicate": True}


async def test_authorisation_endpoint_absent_in_managed_mode(running: Running) -> None:
    """Without External mode there is no REQUEST endpoint to call."""
    assert (await running.http.post(AUTHORISE_PATH, content=b"{}")).status_code == 404


async def test_external_mode_routes_reap_decisions_through_the_gate(tmp_path: Path) -> None:
    """In External mode Reap asks the gate: the claimed purchase passes, a rogue charge fails."""
    async with _running(_settings(tmp_path, AuthorizationMode.EXTERNAL)) as app:
        status = (await app.http.get("/status")).json()
        assert status["reap"]["authorization_mode"] == "EXTERNAL"
        assert await _purchase(app.runtime) == IntentState.AUTHORISED

        binding = app.runtime.control.repos.objectives.list()[0].reap
        assert binding is not None
        assert isinstance(app.runtime.reap, ReapMock)
        rogue = await app.runtime.reap.simulate_authorization(
            SimulateAuthorizationRequest(
                card_id=binding.card_id,
                amount=Decimal(1),
                merchant=SimulatedMerchant(name="Unknown GPU Co", mcc_code="7372"),
            ),
            idempotency_key=None,
        )
        assert isinstance(rogue, DeclinedCardTransaction)
        assert "declined by the authorisation endpoint" in rogue.decline_reason.message

        secret = app.runtime.secrets[AUTHORISE_PATH].get_secret_value()
        junk = b'{"id":"e","type":"CARD_AUTHORIZATION_REQUEST","data":{"nope":1}}'
        header = {SIGNATURE_HEADER: sign(secret, junk, int(app.runtime.clock().timestamp()))}
        answer = await app.http.post(AUTHORISE_PATH, content=junk, headers=header)
        assert answer.json() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}


async def test_sandbox_runtime_uses_secrets_from_settings(tmp_path: Path) -> None:
    """With the sandbox selected, the receiver verifies with the configured secrets."""
    settings = _settings(tmp_path).model_copy(
        update={
            "reap_backend": "sandbox",
            "reap_api_key": SecretStr("sk"),
            "reap_webhook_secret": SecretStr("whsec_n"),
            "reap_authorization_secret": SecretStr("whsec_a"),
        }
    )
    runtime = build_runtime(settings)
    try:
        assert runtime.reap.backend == "sandbox"
        assert runtime.secrets[NOTIFY_PATH].get_secret_value() == "whsec_n"
        assert set(runtime.secrets) == {NOTIFY_PATH, AUTHORISE_PATH}
    finally:
        await runtime.aclose()


def test_serve_refuses_an_incomplete_sandbox_in_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agentic sandbox without its key stops before uvicorn starts, saying why."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("REAP_BACKEND", "sandbox")
    monkeypatch.chdir(Path(__file__).parent)
    with pytest.raises(SystemExit) as stopped:
        serve()
    message = str(stopped.value)
    assert message.startswith("Refusing to start: REAP_BACKEND=sandbox needs REAP_API_KEY")
    assert "operations.md" in message
    assert "\n" not in message


def test_serve_refuses_the_card_path_on_the_sandbox_without_its_webhook_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the dormant card path the notification webhook's secret is still required."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("REAP_BACKEND", "sandbox")
    monkeypatch.setenv("REAP_API_KEY", "not-a-key")
    monkeypatch.setenv("REAP_PURCHASE_PATH", "card")
    monkeypatch.chdir(Path(__file__).parent)
    with pytest.raises(SystemExit) as stopped:
        serve()
    message = str(stopped.value)
    assert message.startswith(
        "Refusing to start: REAP_BACKEND=sandbox with REAP_PURCHASE_PATH=card needs "
        "REAP_WEBHOOK_SECRET"
    )
    assert "\n" not in message


def test_serve_refuses_a_malformed_setting_in_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value that does not parse, such as a mistyped enrolment id, is named in one line."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("REAP_ENROLLMENT_ID", "not-an-enrolment")
    monkeypatch.chdir(Path(__file__).parent)
    with pytest.raises(SystemExit) as stopped:
        serve()
    message = str(stopped.value)
    assert message.startswith("Refusing to start: REAP_ENROLLMENT_ID ")
    assert "must be a UUID" in message
    assert "not-an-enrolment" not in message
    assert "\n" not in message


def test_serve_builds_the_app_on_the_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default configuration passes the check and yields an app."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(Path(__file__).parent)
    assert serve().title == "YouReapYouSow"


async def test_the_runtime_buys_on_the_agentic_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app's control plane judges rule 10, enrols on the mock and completes the order."""
    monkeypatch.setenv("REAP_PURCHASE_PATH", "agentic")
    runtime = build_runtime(_settings(tmp_path))
    try:
        assert isinstance(runtime.reap, ReapMock)
        control = runtime.control
        objective = await control.create_objective(
            kind=ObjectiveKind.SERVICE_FLEET,
            statement="Buy the prize",
            constraints=[],
            budget_usd=PART.grant.budget_usd,
            grant=PART.terms,
            lifecycle=LifecycleRules(),
        )
        assert objective.enrolment is not None
        assert objective.enrolment.is_active
        need = control.raise_need(objective.id, PART.need_spec(), reason="drive", refs={})
        quotes = await control.gather_quotes(need.id)
        cheapest = min(quotes, key=lambda q: q.final_amount.amount)
        intent, decision = control.propose_purchase(
            quote_id=cheapest.id,
            provider=cheapest.merchant,
            offer_id=cheapest.variant.id,
            amount_usd=cheapest.final_amount.amount,
            rationale="test",
            options_considered=[q.id for q in quotes],
        )
        checks = {c.rule: c.passed for c in decision.checks}
        assert checks["item.matches_need"]
        done = await control.execute_purchase(intent.id)
        assert done.state == IntentState.COMPLETED
        assert done.order_id is not None
    finally:
        await runtime.aclose()

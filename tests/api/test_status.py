"""What ``/status`` says about Reap: the path, the enrolment, the catalogue and the checkout."""

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from tests.agentic_replay import agentic_stack, part_objective
from tests.conftest import START
from tests.kwal_stand_in import stand_in_kwal
from youreapyousow.api.app import Runtime, build_runtime, create_app
from youreapyousow.api.status import MOCK_ASSUMPTIONS, reap_status
from youreapyousow.clock import ManualClock
from youreapyousow.config import Settings
from youreapyousow.control import PurchasePath
from youreapyousow.kwal.client import KwalClient
from youreapyousow.kwal.mock import Route
from youreapyousow.kwal.models import KwalSetup, SetupState
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.market.service import MarketMode
from youreapyousow.purchase import PURCHASE_CONFIG_DIR
from youreapyousow.reap.client import MOCK_BASE_URL, ReapHttpClient
from youreapyousow.reap.mock.agentic import TEST_CARDS, TEST_OTP, AgenticMockEngine
from youreapyousow.reap.mock.engine import MockReapEngine
from youreapyousow.reap.mock.server import create_mock_app
from youreapyousow.reap.models import (
    ClientReferenceOwner,
    CreateExternalEnrollmentRequest,
    Presentation,
)
from youreapyousow.store import Database

pytestmark = pytest.mark.anyio

ABSENT = "0b6a3c1e-1d2f-4e5a-8b7c-9d0e1f2a3b4c"


def _settings(tmp_path: Path, **update: object) -> Settings:
    return Settings.model_validate(
        {
            "database_path": tmp_path / "cp.db",
            "snapshot_path": tmp_path / "snap.json",
            "market_mode": MarketMode.MOCK,
            **update,
        }
    )


def _sandbox_client(engine: AgenticMockEngine, clock: ManualClock) -> ReapHttpClient:
    """The real client labelled ``sandbox``, answered by the agentic mock: no call leaves."""
    app = create_mock_app(MockReapEngine(clock=clock), clock, agentic=engine)
    return ReapHttpClient(
        base_url=MOCK_BASE_URL,
        api_key=SecretStr("mock-key"),
        backend="sandbox",
        transport=httpx.ASGITransport(app=app),
    )


async def _enrolled(reap: ReapHttpClient, engine: AgenticMockEngine) -> str:
    created = await reap.create_enrollment(
        CreateExternalEnrollmentRequest(
            owner=ClientReferenceOwner(id="operator", email="op@example.com"),
            presentation=Presentation(return_url="https://example.invalid/return"),
        ),
        idempotency_key="enr:operator:1",
    )
    number, (cvc, expiry) = next(iter(TEST_CARDS.items()))
    engine.submit_card(created.id, number=number, cvc=cvc, expiry=expiry, otp=TEST_OTP)
    return created.id


async def test_the_card_path_keeps_its_authorisation_mode(tmp_path: Path) -> None:
    """On the dormant card path ``/status`` carries no agentic panel."""
    runtime = build_runtime(_settings(tmp_path))
    try:
        reap = await reap_status(
            runtime.settings, runtime.reap, runtime.control.ledger, PurchasePath.CARD
        )
    finally:
        await runtime.aclose()
    assert reap["backend"] == "mock"
    assert reap["purchase_path"] == "card"
    assert reap["authorization_mode"] == "MANAGED"
    assert "enrolment" not in reap
    assert "catalogue" not in reap


async def test_the_agentic_path_on_the_mock_names_catalogue_checkout_and_purchase(
    tmp_path: Path,
) -> None:
    """Before any objective: the mock's catalogue, the header in use, the file in force."""
    runtime = build_runtime(_settings(tmp_path))
    try:
        reap = await reap_status(
            runtime.settings, runtime.reap, runtime.control.ledger, PurchasePath.AGENTIC
        )
    finally:
        await runtime.aclose()
    assert reap["purchase_path"] == "agentic"
    assert "authorization_mode" not in reap
    assert reap["catalogue"] == {
        "source": "mock",
        "note": "the mock's bundled catalogues (compute, parts, headphones): fictional "
        "merchants, illustrative prices",
    }
    assert reap["checkout_mode"] == "simulated-complete"
    assert reap["purchase"] == {
        "config": "configs/purchase-prize.yaml",
        "shape": "part",
        "route": "catalogue",
        "what": "Keychron B40 Wireless Keyboard",
        "merchants": ["Keychron"],
    }
    assert reap["enrolment"] == {
        "id": None,
        "status": None,
        "active": False,
        "source": None,
        "note": "none yet: the mock enrols its own published test card when an objective starts",
    }
    assert reap["mock_assumptions"] == list(MOCK_ASSUMPTIONS)


async def test_the_purchase_file_in_force_and_its_checkout_mode(tmp_path: Path) -> None:
    """``PURCHASE_CONFIG`` decides the shape shown; a file without the header is hosted approval."""
    hosted = tmp_path / "purchase-hosted.yaml"
    hosted.write_text(
        (PURCHASE_CONFIG_DIR / "purchase-part.yaml")
        .read_text()
        .replace("simulate_completed_when_allowed: true", "simulate_completed_when_allowed: false")
    )
    settings = _settings(tmp_path, purchase_config=hosted)
    ledger = Ledger(Database())
    stack = agentic_stack(ManualClock(START))
    try:
        reap = await reap_status(settings, stack.reap, ledger, PurchasePath.AGENTIC)
    finally:
        await stack.aclose()
    assert reap["checkout_mode"] == "hosted-approval"
    purchase = reap["purchase"]
    assert isinstance(purchase, dict)
    assert purchase["config"] == str(hosted)
    assert (purchase["shape"], purchase["what"]) == ("part", "1TB NVMe M.2 2280 SSD")


async def test_a_purchase_file_that_does_not_load_is_named_not_raised(tmp_path: Path) -> None:
    """A broken file in force is shown on ``/status`` so the operator sees it before the run."""
    broken = tmp_path / "broken.yaml"
    broken.write_text("purchase:\n  shape: part\n")
    stack = agentic_stack(ManualClock(START))
    try:
        reap = await reap_status(
            _settings(tmp_path, purchase_config=broken),
            stack.reap,
            Ledger(Database()),
            PurchasePath.AGENTIC,
        )
    finally:
        await stack.aclose()
    purchase = reap["purchase"]
    assert isinstance(purchase, dict)
    assert purchase["config"] == str(broken)
    assert str(purchase["error"]).startswith(f"{broken}: ")
    assert reap["checkout_mode"] is None


async def test_the_latest_objective_enrolment_as_last_read(tmp_path: Path) -> None:
    """Without a configured enrolment, the ledger's latest one is shown with where it came from."""
    stack = agentic_stack(ManualClock(START))
    try:
        objective_id = await part_objective(stack)
        reap = await reap_status(
            _settings(tmp_path), stack.reap, stack.cp.ledger, PurchasePath.AGENTIC
        )
    finally:
        await stack.aclose()
    enrolment = reap["enrolment"]
    assert isinstance(enrolment, dict)
    assert enrolment["status"] == "ACTIVE"
    assert enrolment["active"] is True
    assert enrolment["network"] == "VISA"
    assert enrolment["last4"] == next(iter(TEST_CARDS))[-4:]
    assert str(enrolment["source"]).startswith(f"objective {objective_id}, as last read at ")


async def test_the_operators_sandbox_enrolment_is_read_from_reap(tmp_path: Path) -> None:
    """On the sandbox, ``REAP_ENROLLMENT_ID`` is read from Reap each time ``/status`` is asked."""
    clock = ManualClock(START)
    engine = AgenticMockEngine(clock=clock)
    reap_client = _sandbox_client(engine, clock)
    try:
        enrollment_id = await _enrolled(reap_client, engine)
        settings = _settings(
            tmp_path,
            reap_backend="sandbox",
            reap_api_key=SecretStr("k"),
            reap_enrollment_id=enrollment_id,
        )
        reap = await reap_status(settings, reap_client, Ledger(Database()), PurchasePath.AGENTIC)
        missing = await reap_status(
            settings.model_copy(update={"reap_enrollment_id": ABSENT}),
            reap_client,
            Ledger(Database()),
            PurchasePath.AGENTIC,
        )
        unset = await reap_status(
            settings.model_copy(update={"reap_enrollment_id": None}),
            reap_client,
            Ledger(Database()),
            PurchasePath.AGENTIC,
        )
    finally:
        await reap_client.aclose()
    assert reap["backend"] == "sandbox"
    assert reap["catalogue"] == {"source": "sandbox", "note": "Reap's sandbox catalogue"}
    assert "mock_assumptions" not in reap
    assert reap["enrolment"] == {
        "id": enrollment_id,
        "status": "ACTIVE",
        "active": True,
        "network": "VISA",
        "last4": next(iter(TEST_CARDS))[-4:],
        "source": "REAP_ENROLLMENT_ID, read from Reap just now",
    }
    assert missing["enrolment"] == {
        "id": ABSENT,
        "status": None,
        "active": False,
        "source": "REAP_ENROLLMENT_ID, read from Reap just now",
        "error": "ENROLLMENT_NOT_FOUND",
    }
    unset_enrolment = unset["enrolment"]
    assert isinstance(unset_enrolment, dict)
    assert unset_enrolment["id"] is None
    assert "swap_check --enrol" in str(unset_enrolment["note"])
    assert "REAP_ENROLLMENT_ID" in str(unset_enrolment["note"])


async def test_an_unreachable_reap_is_shown_not_raised(tmp_path: Path) -> None:
    """``/status`` answers when Reap does not: the enrolment says why it could not be read."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    reap_client = ReapHttpClient(
        base_url=MOCK_BASE_URL,
        api_key=SecretStr("mock-key"),
        backend="sandbox",
        transport=httpx.MockTransport(refuse),
    )
    settings = _settings(
        tmp_path, reap_backend="sandbox", reap_api_key=SecretStr("k"), reap_enrollment_id=ABSENT
    )
    try:
        reap = await reap_status(settings, reap_client, Ledger(Database()), PurchasePath.AGENTIC)
    finally:
        await reap_client.aclose()
    enrolment = reap["enrolment"]
    assert isinstance(enrolment, dict)
    assert enrolment["active"] is False
    assert str(enrolment["error"]).startswith("Reap not reached: ")


async def test_the_app_serves_the_agentic_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /status`` carries the agentic panel on the path the control plane runs."""
    monkeypatch.setenv("REAP_PURCHASE_PATH", "agentic")
    settings = _settings(tmp_path)

    async def factory() -> Runtime:
        return build_runtime(settings)

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://cp") as http:
            status = (await http.get("/status")).json()
    assert status["reap"]["backend"] == "mock"
    assert status["reap"]["purchase_path"] == "agentic"
    assert status["reap"]["catalogue"]["source"] == "mock"
    assert status["reap"]["checkout_mode"] == "simulated-complete"
    assert status["market"]["mode"] == "mock"
    assert status["ledger"]["chain_intact"] is True


async def test_kwal_shows_the_participant_s_card_vault_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Kwal: the card's setup and what it can spend, read live; never an address."""
    clock = ManualClock(START)
    engine, session = stand_in_kwal(monkeypatch, clock, tmp_path)
    settings = _settings(tmp_path, reap_backend="kwal", pws_credentials_file=session)
    kwal = KwalClient.from_settings(settings)
    try:
        reap = await reap_status(settings, kwal, Ledger(Database()), PurchasePath.AGENTIC)
        engine.set_setup(KwalSetup(state=SetupState.PENDING, step="vault_deployment"))
        pending = await reap_status(settings, kwal, Ledger(Database()), PurchasePath.AGENTIC)
    finally:
        await kwal.aclose()
    assert (reap["backend"], reap["real_reap"]) == ("kwal", True)
    assert reap["catalogue"] == {
        "source": "kwal",
        "note": "Reap's agentic catalogue through Kwal's participant gateway",
    }
    assert reap["checkout_mode"] == "card-spend"
    assert reap["enrolment"] == {
        "id": kwal.enrolment_id,
        "status": "ACTIVE",
        "active": True,
        "network": None,
        "last4": None,
        "source": "the Kwal participant's set-up card, read from Kwal just now",
    }
    assert reap["kwal"] == {
        "session": {"file": str(session), "expires_at": "2037-10-16T00:00:00+00:00"},
        "setup": {"state": "READY", "step": None, "card": "ACTIVE", "deposit_observed": True},
        "funding": {"state": "READY", "card_spendable_usdc": "500.000000"},
    }
    assert "0x" not in str(reap)
    pending_kwal = pending["kwal"]
    assert isinstance(pending_kwal, dict)
    assert pending_kwal["setup"] == {
        "state": "PENDING",
        "step": "vault_deployment",
        "card": None,
        "deposit_observed": False,
    }
    assert pending["enrolment"]["status"] == "REQUIRES_ACTION"  # pyright: ignore[reportIndexIssue, reportArgumentType, reportCallIssue, reportOptionalSubscript]


async def test_an_unreachable_kwal_is_shown_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway that refuses is named on ``/status``; the page still answers."""
    clock = ManualClock(START)
    engine, session = stand_in_kwal(monkeypatch, clock, tmp_path)
    settings = _settings(tmp_path, reap_backend="kwal", pws_credentials_file=session)
    engine.inject(Route.STATUS, 503, "ParticipantUnavailable", times=2)
    engine.inject(Route.FUNDING, 503, "ParticipantUnavailable")
    kwal = KwalClient.from_settings(settings)
    try:
        reap = await reap_status(settings, kwal, Ledger(Database()), PurchasePath.AGENTIC)
    finally:
        await kwal.aclose()
    shown = reap["kwal"]
    assert isinstance(shown, dict)
    assert shown["setup"] == {"error": "AGENTIC_SERVICE_UNAVAILABLE"}
    assert shown["funding"] == {"error": "AGENTIC_SERVICE_UNAVAILABLE"}
    assert reap["enrolment"]["error"] == "AGENTIC_SERVICE_UNAVAILABLE"  # pyright: ignore[reportIndexIssue, reportArgumentType, reportCallIssue, reportOptionalSubscript]

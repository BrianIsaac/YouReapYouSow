"""The sandbox swap check: the agentic steps on the mock, part-way and done, and the card path."""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from scripts import swap_check
from scripts.swap_check import EnrolmentRead, Observed, PurchaseRead, SearchRead, Stored
from tests.conftest import START
from tests.kwal_stand_in import save_session, stand_in_kwal
from youreapyousow.clock import ManualClock
from youreapyousow.config import Settings
from youreapyousow.control import PurchasePath
from youreapyousow.ledger.events import EventType
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.purchase import PURCHASE_CONFIG_DIR, load_purchase
from youreapyousow.reap.client import MOCK_BASE_URL, ReapHttpClient, ReapMock
from youreapyousow.reap.mock.agentic import TEST_CARDS, TEST_OTP, AgenticMockEngine
from youreapyousow.reap.mock.engine import MockReapEngine
from youreapyousow.reap.mock.server import create_mock_app
from youreapyousow.reap.models import EnrollmentStatus
from youreapyousow.repos import ReapOperator, Repositories
from youreapyousow.store import Database

FEATHERLESS = "FEATHERLESS_API_KEY=fl_do_not_print_me\n"
MOCK_ENV = "MARKET_MODE=mock\nREAP_BACKEND=mock\n" + FEATHERLESS
KEY = "sk_test_do_not_print_me"
SECRET = "whsec_do_not_print_me"
AUTH_SECRET = "whsec_request_do_not_print_me"
ENROLMENT = "6f1c2b9e-8a4d-4c3b-9e2f-1a2b3c4d5e6f"
SANDBOX_ENV = f"REAP_BACKEND=sandbox\nREAP_API_KEY={KEY}\n" + FEATHERLESS
CARD_ENV = SANDBOX_ENV + f"REAP_PURCHASE_PATH=card\nREAP_WEBHOOK_SECRET={SECRET}\n"
SANDBOX_STATUS = {"reap": {"backend": "sandbox", "purchase_path": "agentic"}}
MOCK_STATUS = {"reap": {"backend": "mock", "purchase_path": "agentic"}}
PURCHASE = PurchaseRead(
    "configs/purchase-compute.yaml", "shape compute, catalogue route", None, ships=True
)
MOCK_SEARCH = SearchRead("mock", "GPU compute credits", products=3, merchants=2)
SANDBOX_SEARCH = SearchRead("sandbox", "GPU compute credits", products=5, merchants=3)
ACTIVE = EnrolmentRead(ENROLMENT, "ACTIVE", "VISA ending 1111")

pytestmark = pytest.mark.anyio


def observed(
    dotenv: str,
    *,
    environ: Mapping[str, str] | None = None,
    external: bool | None = None,
    tunnel: bool = True,
    status: Mapping[str, Any] | None = None,
    stored: Stored | None = None,
    purchase: PurchaseRead = PURCHASE,
    enrolment: EnrolmentRead | None = None,
    search: SearchRead | None = None,
    kwal: swap_check.KwalRead | None = None,
) -> Observed:
    """Build what the check would see on this machine."""
    return Observed(
        dotenv=swap_check.parse_env(dotenv),
        environ=environ or {},
        external=external,
        cloudflared=True,
        tunnel_running=tunnel,
        env_ignored=True,
        status=status,
        stored=stored,
        purchase=purchase,
        enrolment=enrolment,
        search=search,
        kwal=kwal,
    )


def states(obs: Observed) -> dict[str, str]:
    """Map each step number to its state."""
    return {s.number: s.state for s in swap_check.assess(obs)}


def step(obs: Observed, number: str) -> swap_check.Step:
    """Find one judged step."""
    return next(s for s in swap_check.assess(obs) if s.number == number)


def test_parse_env_reads_like_the_settings_loader() -> None:
    """Comments, blank lines, quotes, ``export`` and trailing comments are handled."""
    text = "# c\n\nexport REAP_BACKEND=sandbox\nREAP_API_KEY='k=1'\nMARKET_MODE=mock  # x\nBAD\n"
    assert swap_check.parse_env(text) == {
        "REAP_BACKEND": "sandbox",
        "REAP_API_KEY": "k=1",
        "MARKET_MODE": "mock",
    }


def test_on_the_mock_only_the_key_and_the_backend_are_left() -> None:
    """The acceptance: before the swap, the key and the backend are the only steps to do."""
    obs = observed(MOCK_ENV, tunnel=False, status=MOCK_STATUS, search=MOCK_SEARCH)
    assert states(obs) == {
        "0": "done",
        "1": "TODO",
        "2": "TODO",
        "3": "done",
        "4": "done",
        "5": "done",
        "6": "done",
        "7": "done",
        "8": "n/a",
        "9": "done",
        "C1": "n/a",
        "C2": "n/a",
        "C3": "n/a",
    }
    rendered = swap_check.render(swap_check.assess(obs))
    assert rendered.endswith("Not done: step 1, 2.")
    assert "[ n/a] 8  Enrolment in .env and ACTIVE: the mock enrols its own test card" in rendered
    assert '"GPU compute credits": 3 products from 2 merchants (mock)' in rendered


def test_after_the_swap_the_enrolment_is_what_is_left() -> None:
    """Key in, backend switched, restarted: the operator's enrolment is the next step."""
    obs = observed(SANDBOX_ENV, status=SANDBOX_STATUS, search=SANDBOX_SEARCH)
    assert [s.number for s in swap_check.assess(obs) if s.state == "TODO"] == ["8"]
    assert "swap_check --enrol" in step(obs, "8").detail


def test_the_enrolment_must_be_active() -> None:
    """Set but unfinished, unknown to Reap, or not read: each says what to do next."""
    text = SANDBOX_ENV + f"REAP_ENROLLMENT_ID={ENROLMENT}\n"
    waiting = EnrolmentRead(ENROLMENT, "REQUIRES_ACTION", "no card yet")
    obs = observed(text, status=SANDBOX_STATUS, search=SANDBOX_SEARCH, enrolment=waiting)
    assert step(obs, "8").state == "TODO"
    assert "REQUIRES_ACTION: finish Reap's hosted card page" in step(obs, "8").detail
    unknown = EnrolmentRead(ENROLMENT, None, "ENROLLMENT_NOT_FOUND")
    obs = observed(text, status=SANDBOX_STATUS, search=SANDBOX_SEARCH, enrolment=unknown)
    assert "not read: ENROLLMENT_NOT_FOUND" in step(obs, "8").detail
    obs = observed(text, status=SANDBOX_STATUS, search=SANDBOX_SEARCH, enrolment=ACTIVE)
    assert step(obs, "8") == swap_check.Step(
        "8",
        "Enrolment in .env and ACTIVE",
        "done",
        f"REAP_ENROLLMENT_ID {ENROLMENT} is ACTIVE (VISA ending 1111); bound to each new objective",
    )


def test_a_complete_agentic_swap_is_all_done() -> None:
    """Every agentic step done once the enrolment is active; the card steps stay n/a."""
    text = SANDBOX_ENV + f"REAP_ENROLLMENT_ID={ENROLMENT}\n"
    obs = observed(text, status=SANDBOX_STATUS, search=SANDBOX_SEARCH, enrolment=ACTIVE)
    steps = swap_check.assess(obs)
    assert [s.state for s in steps] == ["done"] * 10 + ["n/a"] * 3
    assert swap_check.render(steps).endswith("All steps done.")


def test_a_search_that_fails_is_named() -> None:
    """A refused search shows Reap's code: ``AGENTIC_PAYMENTS_NOT_ENABLED`` is go or no-go 1."""
    refused = SearchRead("sandbox", "GPU compute credits", error="AGENTIC_PAYMENTS_NOT_ENABLED")
    obs = observed(SANDBOX_ENV, status=SANDBOX_STATUS, search=refused)
    assert step(obs, "9").state == "TODO"
    assert step(obs, "9").detail == '"GPU compute credits" refused: AGENTIC_PAYMENTS_NOT_ENABLED'
    obs = observed(SANDBOX_ENV.replace(f"REAP_API_KEY={KEY}\n", ""), status=SANDBOX_STATUS)
    assert step(obs, "9").detail == "not searched: the sandbox needs REAP_API_KEY first"
    empty = SearchRead("sandbox", "GPU compute credits", products=0, merchants=0)
    obs = observed(SANDBOX_ENV, status=SANDBOX_STATUS, search=empty)
    assert step(obs, "9").state == "TODO"
    assert "edit the query" in step(obs, "9").detail


def test_the_checkout_url_route_has_nothing_to_search() -> None:
    """The third route quotes a cart; there is no catalogue search to make."""
    cart = PurchaseRead("configs/purchase-checkout-url.yaml", "shape part, checkout_url", None)
    obs = observed(
        MOCK_ENV, status=MOCK_STATUS, purchase=cart, search=SearchRead("mock", query=None)
    )
    assert step(obs, "9").state == "n/a"


def test_a_purchase_file_that_does_not_load_stops_the_swap() -> None:
    """The file in force is validated before the server is trusted with it."""
    broken = PurchaseRead("configs/x.yaml", None, "configs/x.yaml has no purchase: block")
    obs = observed(MOCK_ENV, status=MOCK_STATUS, purchase=broken)
    assert step(obs, "4").state == "TODO"
    assert step(obs, "4").detail == "configs/x.yaml has no purchase: block"
    assert step(obs, "9").state == "TODO"


def test_the_model_provider_is_checked_as_u5b_set_it() -> None:
    """Featherless named without its key is a step to do; the key itself is never shown."""
    obs = observed("REAP_BACKEND=mock\nMODEL_PROVIDER=featherless\n", status=MOCK_STATUS)
    assert step(obs, "5").state == "TODO"
    assert "needs FEATHERLESS_API_KEY" in step(obs, "5").detail
    assert step(obs, "6").state == "TODO"
    obs = observed(MOCK_ENV, status=MOCK_STATUS)
    assert step(obs, "5").detail == (
        "featherless: FEATHERLESS_API_KEY set in .env; AGENT_STRATEGY=deterministic (unset)"
    )
    obs = observed("REAP_BACKEND=mock\n", status=MOCK_STATUS)
    assert step(obs, "5").state == "done"
    assert step(obs, "5").detail.startswith("automatic: the local server if it answers")
    obs = observed("MODEL_PROVIDER=none\n", status=MOCK_STATUS)
    assert step(obs, "5").detail == (
        "none: the deterministic ranking decides; AGENT_STRATEGY=deterministic (unset)"
    )
    obs = observed(MOCK_ENV, environ={"AGENT_STRATEGY": "model"}, status=MOCK_STATUS)
    assert step(obs, "5").detail.endswith("; AGENT_STRATEGY=model")


def test_a_server_on_other_values_must_be_restarted() -> None:
    """``/status`` must show the backend and path that ``.env`` now names."""
    obs = observed(SANDBOX_ENV, status=MOCK_STATUS, search=SANDBOX_SEARCH)
    assert step(obs, "7").state == "TODO"
    assert step(obs, "7").detail == (
        "/status shows backend mock, path agentic; .env names sandbox, agentic: restart"
    )
    assert step(observed(SANDBOX_ENV, status=None), "7").detail == (
        "server not answering on /status: start it"
    )


def test_the_card_path_still_needs_its_tunnel_and_webhooks() -> None:
    """On the dormant card path the old steps return, and the agentic reads are n/a."""
    card_status = {"reap": {"backend": "sandbox", "purchase_path": "card"}}
    obs = observed(CARD_ENV, external=False, status=card_status)
    assert {n: s for n, s in states(obs).items() if n in {"8", "9", "C1", "C2", "C3"}} == {
        "8": "n/a",
        "9": "n/a",
        "C1": "done",
        "C2": "done",
        "C3": "n/a",
    }
    assert all(s.state != "TODO" for s in swap_check.assess(obs))
    no_secret = CARD_ENV.replace(f"REAP_WEBHOOK_SECRET={SECRET}\n", "")
    obs = observed(no_secret, external=True, tunnel=False, status=card_status)
    assert {n: s for n, s in states(obs).items() if s == "TODO"} == {
        "6": "TODO",
        "C1": "TODO",
        "C2": "TODO",
        "C3": "TODO",
    }
    assert "REAP_PURCHASE_PATH=card needs REAP_WEBHOOK_SECRET" in step(obs, "6").detail
    with_request = CARD_ENV + f"REAP_AUTHORIZATION_SECRET={AUTH_SECRET}\n"
    assert states(observed(with_request, external=True, status=card_status))["C3"] == "done"


def test_a_shell_export_that_overrides_env_is_flagged() -> None:
    """The process environment wins over .env, so a stale export must be caught."""
    obs = observed(SANDBOX_ENV, environ={"REAP_BACKEND": "mock"}, status=MOCK_STATUS)
    assert step(obs, "2").state == "TODO"
    assert "the shell overrides .env for REAP_BACKEND" in step(obs, "2").detail


def test_no_secret_is_ever_printed() -> None:
    """The rendered check names variables, never their values."""
    text = CARD_ENV + f"REAP_AUTHORIZATION_SECRET={AUTH_SECRET}\n"
    for obs in (
        observed(text, external=True),
        observed(SANDBOX_ENV, status=SANDBOX_STATUS, search=SANDBOX_SEARCH, enrolment=ACTIVE),
    ):
        rendered = swap_check.render(swap_check.assess(obs))
        for secret in (KEY, SECRET, AUTH_SECRET, "fl_do_not_print_me"):
            assert secret not in rendered


def test_a_var_left_from_the_mock_must_go_before_the_sandbox() -> None:
    """Enrolments or a card operator from another backend would mislead the run: rm -rf var/."""
    mock_left = Stored(operator=False, backends=frozenset({"mock"}))
    obs = observed(SANDBOX_ENV, stored=mock_left)
    assert step(obs, "3").state == "TODO"
    assert "var/ holds Reap state from mock: rm -rf var/ before restarting" in step(obs, "3").detail
    fresh = Stored(operator=False, backends=frozenset({"sandbox"}))
    assert states(observed(SANDBOX_ENV, stored=fresh))["3"] == "done"
    assert step(observed(SANDBOX_ENV), "3").detail == "no database yet"


def test_read_stored_reads_a_real_database(tmp_path: Path) -> None:
    """The card operator, a card's backend and an enrolment's backend come from the schema."""
    database = tmp_path / "cp.db"
    assert swap_check.read_stored(database) is None
    db = Database(database)
    Repositories(db).reap_operator.insert(
        ReapOperator(user_id="u", account_id="a"), record_id="operator", objective_id=None
    )
    ledger = Ledger(db)
    ledger.append(
        EventType.REAP_CARD_ISSUED,
        subject_id="card_1",
        objective_id="obj_1",
        refs={},
        payload={"last4": "7797", "backend": "mock"},
    )
    ledger.append(
        EventType.REAP_ENROLLED,
        subject_id=ENROLMENT,
        objective_id="obj_2",
        refs={"objective": "obj_2"},
        payload={"status": "ACTIVE", "backend": "sandbox"},
    )
    db.close()
    assert swap_check.read_stored(database) == Stored(True, frozenset({"mock", "sandbox"}))
    (tmp_path / "junk.db").write_text("not a database")
    unreadable = swap_check.read_stored(tmp_path / "junk.db")
    assert unreadable is not None
    assert unreadable.backends == frozenset({"an unreadable database"})


def _sandbox_labelled(engine: AgenticMockEngine) -> ReapHttpClient:
    """The real client labelled ``sandbox`` and answered by the agentic mock: nothing leaves."""
    clock = ManualClock(START)
    app = create_mock_app(MockReapEngine(clock=clock), clock, agentic=engine)
    return ReapHttpClient(
        base_url=MOCK_BASE_URL,
        api_key=SecretStr("mock-key"),
        backend="sandbox",
        transport=httpx.ASGITransport(app=app),
    )


async def test_the_search_reads_both_shapes_from_the_mock() -> None:
    """The first search answers from the mock's catalogues for each shipped file."""
    reap = ReapMock()
    try:
        compute = await swap_check.read_search(
            reap, load_purchase(PURCHASE_CONFIG_DIR / "purchase-compute.yaml")
        )
        part = await swap_check.read_search(
            reap, load_purchase(PURCHASE_CONFIG_DIR / "purchase-part.yaml")
        )
        cart = await swap_check.read_search(
            reap, load_purchase(PURCHASE_CONFIG_DIR / "purchase-checkout-url.yaml")
        )
    finally:
        await reap.aclose()
    assert compute.backend == "mock"
    assert (compute.query, compute.error) == ("GPU compute credits", None)
    assert compute.products > 0
    assert compute.merchants == 2
    assert (part.query, part.products, part.merchants) == ("1TB NVMe M.2 2280 SSD", 4, 2)
    assert cart == SearchRead("mock", None)


async def test_enrol_creates_one_enrolment_per_attempt_and_reads_it() -> None:
    """``--enrol`` creates the hosted step once per attempt; a rerun replays the same page."""
    engine = AgenticMockEngine(clock=ManualClock(START))
    reap = _sandbox_labelled(engine)
    settings = Settings.from_values({"OPERATOR_EMAIL": "op@example.com"})
    purchase = load_purchase(PURCHASE_CONFIG_DIR / "purchase-part.yaml")
    try:
        first = await swap_check.enrol(reap, settings, purchase, attempt=1)
        again = await swap_check.enrol(reap, settings, purchase, attempt=1)
        second = await swap_check.enrol(reap, settings, purchase, attempt=2)
        assert first.id == again.id
        assert first.url == again.url
        assert second.id != first.id
        assert first.url is not None
        assert f"/hosted/enrollments/{first.id}" in first.url
        assert first.status == "REQUIRES_ACTION"
        before = await swap_check.read_enrolment(reap, first.id)
        number, (cvc, expiry) = next(iter(TEST_CARDS.items()))
        engine.submit_card(first.id, number=number, cvc=cvc, expiry=expiry, otp=TEST_OTP)
        after = await swap_check.read_enrolment(reap, first.id)
        missing = await swap_check.read_enrolment(reap, "0b6a3c1e-1d2f-4e5a-8b7c-9d0e1f2a3b4c")
    finally:
        await reap.aclose()
    assert before == EnrolmentRead(first.id, "REQUIRES_ACTION", "no card yet")
    assert after == EnrolmentRead(first.id, "ACTIVE", f"VISA ending {number[-4:]}")
    assert missing.status is None
    assert missing.detail == "ENROLLMENT_NOT_FOUND"


def _stub_machine(monkeypatch: pytest.MonkeyPatch, status: dict[str, Any] | None) -> None:
    """Stub the tunnel, git and ``/status`` probes: no process, no network."""

    def ignored(_: Path) -> bool:
        return True

    def answer(_: str) -> dict[str, Any] | None:
        return status

    monkeypatch.setattr(swap_check, "_tunnel_running", lambda: False)
    monkeypatch.setattr(swap_check, "_env_ignored", ignored)
    monkeypatch.setattr(swap_check, "_status", answer)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_main_on_the_mock_shows_the_key_and_backend_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end on a file: the search runs on the in-process mock, the rest is stubbed."""
    _stub_machine(monkeypatch, dict(MOCK_STATUS))
    env = tmp_path / ".env"
    env.write_text(MOCK_ENV)
    assert swap_check.main(["--env-file", str(env)]) == 1
    out = capsys.readouterr().out
    assert "Not done: step 1, 2." in out
    assert "[done] 9  First catalogue search answers: " in out
    assert "(mock)" in out
    assert "fl_do_not_print_me" not in out

    assert swap_check.main(["--env-file", str(tmp_path / "missing")]) == 1
    assert "[TODO] 0  .env exists and git ignores it: .env is missing or empty" in (
        capsys.readouterr().out
    )


def test_main_names_a_malformed_value_in_one_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value that does not parse is the startup check's refusal, and the reads are skipped."""
    _stub_machine(monkeypatch, None)
    env = tmp_path / ".env"
    env.write_text(SANDBOX_ENV + "REAP_ENROLLMENT_ID=enr_123\n")
    assert swap_check.main(["--env-file", str(env)]) == 1
    out = capsys.readouterr().out
    assert "[TODO] 6  Server would start on these values: it would refuse: " in out
    assert "REAP_ENROLLMENT_ID must be a UUID" in out
    assert KEY not in out


def test_main_enrol_refuses_the_mock_and_an_enrolment_already_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--enrol`` is for the sandbox, once: never on the mock, never over a set enrolment."""
    _stub_machine(monkeypatch, None)
    env = tmp_path / ".env"
    env.write_text(MOCK_ENV)
    assert swap_check.main(["--env-file", str(env), "--enrol"]) == 2
    assert "the mock enrols its own published test card" in capsys.readouterr().out
    env.write_text(SANDBOX_ENV + f"REAP_ENROLLMENT_ID={ENROLMENT}\n")
    assert swap_check.main(["--env-file", str(env), "--enrol"]) == 2
    assert "REAP_ENROLLMENT_ID is already set" in capsys.readouterr().out
    env.write_text(SANDBOX_ENV.replace(f"REAP_API_KEY={KEY}\n", ""))
    assert swap_check.main(["--env-file", str(env), "--enrol"]) == 2
    assert "needs REAP_API_KEY" in capsys.readouterr().out


def test_main_enrol_prints_the_hosted_page_and_the_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On the sandbox, ``--enrol`` prints the page to open and the line to add to .env."""
    _stub_machine(monkeypatch, None)
    engine = AgenticMockEngine(clock=ManualClock(START))

    def sandbox(_: Settings) -> ReapHttpClient:
        return _sandbox_labelled(engine)

    monkeypatch.setattr(swap_check, "reap_for", sandbox)
    env = tmp_path / ".env"
    env.write_text(SANDBOX_ENV)
    assert swap_check.main(["--env-file", str(env), "--enrol"]) == 0
    out = capsys.readouterr().out
    found = re.search(r"REAP_ENROLLMENT_ID=(\S+)", out)
    assert found is not None
    created = found.group(1)
    assert engine.get_enrollment(created).status == EnrollmentStatus.REQUIRES_ACTION
    assert f"Enrolment {created} (key enr:operator:1): REQUIRES_ACTION." in out
    assert f"/hosted/enrollments/{created}" in out
    assert f"REAP_ENROLLMENT_ID={created}" in out
    assert KEY not in out


def test_only_the_swap_variables_count_as_shell_overrides() -> None:
    """A shell ``DATABASE_PATH`` is the operator's choice; a shell ``REAP_API_KEY`` is not."""
    obs = observed(SANDBOX_ENV, environ={"DATABASE_PATH": "/tmp/x.db"}, status=SANDBOX_STATUS)
    assert step(obs, "2").detail == "REAP_BACKEND=sandbox"
    obs = observed(SANDBOX_ENV, environ={"REAP_API_KEY": "sk_other"}, status=SANDBOX_STATUS)
    assert "the shell overrides .env for REAP_API_KEY" in step(obs, "2").detail
    assert step(observed("A=1\n"), "0").detail == ".env has 1 value; gitignored"


def test_one_product_from_one_merchant_reads_in_the_singular() -> None:
    """The search line is read aloud at 15:45; it says what it means."""
    one = SearchRead("sandbox", "120mm case fan", products=1, merchants=1)
    obs = observed(SANDBOX_ENV, status=SANDBOX_STATUS, search=one)
    assert step(obs, "9").detail == '"120mm case fan": 1 product from 1 merchant (sandbox)'


KWAL_ENV = "REAP_BACKEND=kwal\nREAP_PURCHASE_PATH=agentic\nPWS_CREDENTIALS_FILE={path}\n"
KWAL_STATUS = {"reap": {"backend": "kwal", "purchase_path": "agentic"}}
KWAL_SEARCH = SearchRead("kwal", "GPU compute credits", products=3, merchants=2)
KWAL_READY = swap_check.KwalRead(
    session="credentials at /home/op/.config/pws/agent-payment/credentials.json, "
    "expires 2037-10-15T09:00:00+00:00",
    session_ok=True,
    setup="READY",
    setup_detail="card ACTIVE, deposit observed",
    funding="READY",
    spendable="25.000000",
)


def _kwal_session(tmp_path: Path) -> Path:
    """Save a Kwal session as the skill does, outside any checkout."""
    return save_session(tmp_path / "pws")


def test_kwal_needs_no_reap_key_and_shows_its_own_steps(tmp_path: Path) -> None:
    """On Kwal: no Reap key, no Reap enrolment; the session, the setup and the vault instead."""
    env = KWAL_ENV.format(path=_kwal_session(tmp_path)) + FEATHERLESS
    obs = observed(env, status=KWAL_STATUS, search=KWAL_SEARCH, kwal=KWAL_READY)
    assert states(obs) == {
        "0": "done",
        "1": "n/a",
        "2": "done",
        "3": "done",
        "4": "done",
        "5": "done",
        "6": "done",
        "7": "done",
        "8": "n/a",
        "9": "done",
        "K1": "done",
        "K2": "done",
        "K3": "done",
        "C1": "n/a",
        "C2": "n/a",
        "C3": "n/a",
    }
    rendered = swap_check.render(swap_check.assess(obs))
    assert "[done] 2  Backend switched to kwal: REAP_BACKEND=kwal" in rendered
    assert "[done] K3  Kwal vault funded: the card can spend 25.000000 USDC" in rendered
    assert rendered.endswith("All steps done.")


@pytest.mark.parametrize(
    ("read", "number", "says"),
    [
        (
            swap_check.KwalRead(session="no Kwal credentials at /x: register", session_ok=False),
            "K1",
            "no Kwal credentials at /x: register",
        ),
        (
            swap_check.KwalRead(
                session="ok", session_ok=True, setup="PENDING", setup_detail="at vault_deployment"
            ),
            "K2",
            "PENDING at vault_deployment: resume it with the skill: python3 scripts/register.py "
            "setup",
        ),
        (
            swap_check.KwalRead(
                session="ok",
                session_ok=True,
                setup="NEEDS_OPERATOR",
                setup_detail="at card_creation",
            ),
            "K2",
            "NEEDS_OPERATOR at card_creation: ask Kwal's operator; do not register again",
        ),
        (
            swap_check.KwalRead(
                session="ok",
                session_ok=True,
                setup="READY",
                setup_detail="card ACTIVE, deposit observed",
                funding="FUNDS_NEEDED",
                spendable="0.000000",
            ),
            "K3",
            "FUNDS_NEEDED: the card can spend 0.000000 USDC; send test USDC to the vault "
            "(python3 scripts/register.py funding)",
        ),
    ],
)
def test_each_kwal_step_says_what_to_do_next(
    tmp_path: Path, read: swap_check.KwalRead, number: str, says: str
) -> None:
    """A missing session, an unfinished setup, a stop for Kwal's operator, an empty vault."""
    env = KWAL_ENV.format(path=_kwal_session(tmp_path)) + FEATHERLESS
    obs = observed(env, status=KWAL_STATUS, search=KWAL_SEARCH, kwal=read)
    assert step(obs, number).state == "TODO"
    assert step(obs, number).detail == says


def test_the_kwal_steps_stay_off_the_reap_swap() -> None:
    """The sandbox swap at K shows no Kwal step."""
    obs = observed(SANDBOX_ENV, status=SANDBOX_STATUS, search=SANDBOX_SEARCH)
    assert not [s for s in swap_check.assess(obs) if s.number.startswith("K")]


async def test_the_kwal_reads_come_from_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session, the setup and the vault are read from Kwal; no address is kept."""
    _, session = stand_in_kwal(monkeypatch, ManualClock(START), tmp_path)
    settings = Settings(
        reap_backend="kwal", reap_purchase_path=PurchasePath.AGENTIC, pws_credentials_file=session
    )
    read = await swap_check.read_kwal(settings)
    assert (read.session_ok, read.setup, read.funding, read.spendable) == (
        True,
        "READY",
        "READY",
        "500.000000",
    )
    assert "0x" not in repr(read)


def test_on_kwal_the_purchase_file_must_name_a_delivery_address(tmp_path: Path) -> None:
    """Kwal's gateway refuses a quote without an address: step 4 says so."""
    env = KWAL_ENV.format(path=_kwal_session(tmp_path)) + FEATHERLESS
    bare = PurchaseRead(
        "configs/purchase-compute.yaml", "shape compute, catalogue route", None, ships=False
    )
    obs = observed(env, status=KWAL_STATUS, search=KWAL_SEARCH, kwal=KWAL_READY, purchase=bare)
    assert step(obs, "4").state == "TODO"
    assert step(obs, "4").detail == (
        "configs/purchase-compute.yaml: Kwal quotes every item to an address: set "
        "shipping_address in the file (the gateway refuses a quote without one)"
    )
    sandbox = observed(SANDBOX_ENV, status=SANDBOX_STATUS, search=SANDBOX_SEARCH, purchase=bare)
    assert step(sandbox, "4").state == "done"


def test_a_setup_waiting_for_its_deposit_points_at_the_funding(tmp_path: Path) -> None:
    """At ``deposit_observation`` the vault and card exist; the deposit is what is missing."""
    env = KWAL_ENV.format(path=_kwal_session(tmp_path)) + FEATHERLESS
    waiting = swap_check.KwalRead(
        session="ok",
        session_ok=True,
        setup="PENDING",
        setup_detail="at deposit_observation",
        funding="FUNDS_NEEDED",
        spendable="0.000000",
    )
    obs = observed(env, status=KWAL_STATUS, search=KWAL_SEARCH, kwal=waiting)
    assert step(obs, "K2").detail == (
        "PENDING at deposit_observation: the vault and card exist and wait for the first "
        "deposit: fund the vault (K3), then python3 scripts/register.py setup"
    )

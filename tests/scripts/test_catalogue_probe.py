"""The catalogue probe against the agentic mock: both shapes' candidates, quotes, snapshots."""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from scripts import catalogue_probe
from scripts.catalogue_probe import Probe
from tests.conftest import START
from tests.kwal_stand_in import stand_in_kwal
from youreapyousow.clock import ManualClock
from youreapyousow.procure.need import ValueMatch, ValueOption
from youreapyousow.purchase import PURCHASE_CONFIG_DIR, PurchaseConfig, load_purchase
from youreapyousow.reap.client import MOCK_BASE_URL, ReapHttpClient
from youreapyousow.reap.mock.agentic import AgenticMockEngine
from youreapyousow.reap.mock.engine import MockReapEngine
from youreapyousow.reap.mock.server import create_mock_app

pytestmark = pytest.mark.anyio

COMPUTE = load_purchase(PURCHASE_CONFIG_DIR / "purchase-compute.yaml")
FOUR_TIMES = COMPUTE.model_copy(
    update={
        "match": ValueMatch(
            value_from=ValueOption(option="Amount"), covers_estimate=True, max_multiple=Decimal(4)
        )
    }
)
PART = load_purchase(PURCHASE_CONFIG_DIR / "purchase-part.yaml")
CART = load_purchase(PURCHASE_CONFIG_DIR / "purchase-checkout-url.yaml")


class Recording(httpx.AsyncBaseTransport):
    """Pass requests to the mock, keeping each method and path."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        """Wrap a transport.

        Args:
            inner: The transport that answers.
        """
        self.inner = inner
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record the call and pass it on.

        Args:
            request: The request.

        Returns:
            The mock's response.
        """
        self.calls.append((request.method, request.url.path))
        return await self.inner.handle_async_request(request)


@pytest.fixture
def engine() -> AgenticMockEngine:
    """The agentic mock over every bundled catalogue.

    Returns:
        The engine.
    """
    return AgenticMockEngine(clock=ManualClock(START))


@pytest.fixture
def recording(engine: AgenticMockEngine) -> Recording:
    """The mock app behind a recording transport.

    Returns:
        The transport.
    """
    clock = ManualClock(START)
    app = create_mock_app(MockReapEngine(clock=clock), clock, agentic=engine)
    return Recording(httpx.ASGITransport(app=app))


@pytest.fixture
async def reap(recording: Recording) -> ReapHttpClient:
    """The real client on the mock.

    Returns:
        The client.
    """
    return ReapHttpClient(
        base_url=MOCK_BASE_URL,
        api_key=SecretStr("mock-key"),
        backend="mock",
        transport=recording,
    )


async def _probe(
    reap: ReapHttpClient,
    purchase: PurchaseConfig,
    *,
    quote: bool = False,
    estimate: Decimal | None = None,
) -> Probe:
    return await catalogue_probe.probe_purchase(
        reap, purchase, label="file", estimate_usd=estimate, quote=quote, run_id="t1"
    )


def _variants(probe: Probe) -> dict[str, tuple[bool | None, bool | None]]:
    return {
        c.variant.id: (c.fills, c.in_scope)
        for product in probe.products
        for c in product.candidates
    }


async def test_both_shapes_candidates_come_from_the_mock_catalogues(
    reap: ReapHttpClient, recording: Recording
) -> None:
    """The acceptance: each shipped file's search yields its shape's candidates, read-only."""
    part = await _probe(reap, PART)
    compute = await _probe(reap, COMPUTE)
    assert part.error is None
    assert _variants(part) == {
        "var-northwind-nvme-1tb": (True, True),
        "var-northwind-nvme-2tb": (True, True),
        "var-kestrel-nvme-1tb": (True, True),
        "var-kestrel-pro-nvme-1tb": (True, True),
    }
    valued = _variants(compute)
    assert valued["var-northwind-gpu-credits-100"] == (None, True)
    assert valued["var-kestrel-gpu-credits-100"] == (None, True)
    assert valued["var-kestrel-inference-pro"] == (False, True)
    assert {c.merchant for p in compute.products for c in p.candidates} == {
        "Northwind Cloud",
        "Kestrel Compute",
    }
    assert {method for method, _ in recording.calls} == {"POST"}
    assert {path for _, path in recording.calls} == {
        "/agentic/products/search",
        "/agentic/products/details",
        "/agentic/products/variant",
    }


async def test_the_rendering_names_merchants_variants_and_rule_10(reap: ReapHttpClient) -> None:
    """What the operator reads at 16:00: merchant, scope, options, price and the judgement."""
    text = catalogue_probe.render(await _probe(reap, PART), PART)
    assert text.splitlines()[0] == "== file"
    assert 'Search "1TB NVMe M.2 2280 SSD" (SG, USD): 4 products from 2 merchants' in text
    assert "Northwind Components [in scope]" in text
    assert "  Northwind Components [in scope]  Northwind 1TB NVMe M.2 2280 SSD  USD 69.00  " in text
    assert "    Capacity: 1 TB\n    Interface: NVMe\n    Form factor: M.2 2280" in text
    assert "var-northwind-nvme-1tb" in text
    assert "rule 10: pass" in text
    assert "Candidates passing rule 10: 4 of 4, 4 from the grant's merchants." in text


async def test_a_merchant_outside_the_grant_is_shown_so_the_scope_can_be_edited(
    reap: ReapHttpClient,
) -> None:
    """At 16:15 the merchants are edited to the real catalogue's; the probe shows who is out."""
    narrowed = PART.model_copy(update={"merchants": ("Northwind Components",)})
    probe = await _probe(reap, narrowed)
    assert _variants(probe)["var-kestrel-nvme-1tb"] == (True, False)
    text = catalogue_probe.render(probe, narrowed)
    assert "Kestrel Parts [not in the grant's merchants]" in text
    assert "Candidates passing rule 10: 4 of 4, 2 from the grant's merchants." in text


async def test_a_value_is_judged_against_an_estimate_only_when_given(
    reap: ReapHttpClient,
) -> None:
    """Shape (a)'s rule 10 needs the live estimate; without one the credit is valued only."""
    plain = await _probe(reap, COMPUTE)
    hundred = next(
        c
        for p in plain.products
        for c in p.candidates
        if c.variant.id == "var-northwind-gpu-credits-100"
    )
    assert hundred.fills is None
    assert hundred.judgement.endswith("not judged: pass --estimate-usd for the live estimate")
    judged = await _probe(reap, FOUR_TIMES, estimate=Decimal(30))
    verdicts = _variants(judged)
    assert verdicts["var-northwind-gpu-credits-100"] == (True, True)
    assert verdicts["var-northwind-gpu-credits-250"] == (False, True)


async def test_quote_lands_the_final_amount_and_reads_it_against_the_grant(
    reap: ReapHttpClient, recording: Recording
) -> None:
    """With ``--quote`` each passing candidate is quoted; nothing is ever checked out."""
    probe = await _probe(reap, PART, quote=True)
    finals = {
        c.variant.id: c.quote.amount_breakdown.final_amount.amount
        for p in probe.products
        for c in p.candidates
        if c.quote is not None
    }
    assert finals == {
        "var-northwind-nvme-1tb": Decimal("77.00"),
        "var-kestrel-nvme-1tb": Decimal("79.00"),
        "var-northwind-nvme-2tb": Decimal("107.00"),
        "var-kestrel-pro-nvme-1tb": Decimal("194.00"),
    }
    assert not any("checkouts" in path for _, path in recording.calls)
    text = catalogue_probe.render(probe, PART)
    assert "final USD 77.00" in text
    assert "allowed under this grant (cap 150, approval above 120)" in text
    assert "refused under this grant: above the per-purchase cap 150" in text


async def test_the_checkout_url_route_quotes_its_cart_only_when_asked(
    reap: ReapHttpClient,
) -> None:
    """The third route has no search; ``--quote`` lands the cart's quote."""
    plain = await _probe(reap, CART)
    assert plain.request is None
    assert (
        plain.note == "the checkout_url route has nothing to search; --quote lands the cart's quote"
    )
    quoted = await _probe(reap, CART, quote=True)
    assert quoted.cart_quote is not None
    assert "cart https://merchant.example/cart/var_123:1: final USD" in catalogue_probe.render(
        quoted, CART
    )


async def test_a_refused_search_is_named(reap: ReapHttpClient, engine: AgenticMockEngine) -> None:
    """A project without the Agentic module answers 403: go or no-go 1 reads it here."""
    engine.enabled = False
    probe = await _probe(reap, PART)
    assert probe.error == "AGENTIC_PAYMENTS_NOT_ENABLED"
    assert "refused: AGENTIC_PAYMENTS_NOT_ENABLED" in catalogue_probe.render(probe, PART)


async def test_a_free_search_shows_products_and_their_default_variants(
    reap: ReapHttpClient,
) -> None:
    """Free searches for products named by hand: no file, so no match and no scope."""
    probe = await catalogue_probe.probe_query(reap, "120mm case fan", country="SG", currency="USD")
    assert probe.label == 'free search "120mm case fan"'
    candidates = [c for p in probe.products for c in p.candidates]
    assert candidates
    assert {c.in_scope for c in candidates} == {None}
    assert {c.fills for c in candidates} == {None}
    text = catalogue_probe.render(probe, None)
    assert "Northwind Components" in text
    assert "Size:" in text


async def test_the_snapshot_keeps_every_raw_answer(reap: ReapHttpClient, tmp_path: Path) -> None:
    """The 16:00 read is kept under ``var/`` as Reap answered it, dated, for the contract tests."""
    probes = [await _probe(reap, PART, quote=True), await _probe(reap, COMPUTE)]
    path = catalogue_probe.snapshot(
        probes, backend="mock", out_dir=tmp_path / "var" / "catalogue-probe", taken_at=START
    )
    assert path == tmp_path / "var" / "catalogue-probe" / "probe-20371009T084203Z-mock.json"
    saved = json.loads(path.read_text())
    assert saved["backend"] == "mock"
    assert saved["taken_at"] == "2037-10-09T08:42:03+00:00"
    first = saved["probes"][0]
    assert first["request"]["query"] == "1TB NVMe M.2 2280 SSD"
    assert len(first["search"]["products"]) == 4
    assert first["details"]["products"]
    variant = first["candidates"][0]
    assert {"product_id", "merchant", "in_scope", "variant", "rule_10", "quote"} <= set(variant)
    assert variant["quote"]["amountBreakdown"]["finalAmount"]["currency"] == "USD"


def test_main_on_the_mock_prints_both_shapes_and_snapshots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: the three shipped files on the default backend, the snapshot under var/."""
    assert catalogue_probe.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Catalogue probe on mock: read-only (search, details, variant)")
    assert "== configs/purchase-compute.yaml: shape compute, catalogue route" in out
    assert "== configs/purchase-part.yaml: shape part, catalogue route" in out
    assert "== configs/purchase-checkout-url.yaml: shape part, checkout_url route" in out
    assert "var-northwind-gpu-credits-100" in out
    assert "var-northwind-nvme-1tb" in out
    saved = list((tmp_path / "var" / "catalogue-probe").glob("probe-*-mock.json"))
    assert len(saved) == 1
    assert f"Snapshot: {saved[0].relative_to(tmp_path)}" in out


def test_main_runs_free_queries_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--query`` alone searches only what was asked."""
    assert catalogue_probe.main(["--query", "650W power supply", "--no-snapshot"]) == 0
    out = capsys.readouterr().out
    assert '== free search "650W power supply"' in out
    assert "purchase-part.yaml" not in out
    assert not (tmp_path / "var").exists()


def test_main_refuses_the_sandbox_without_its_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sandbox is probed only with the key from .env; nothing is sent without it."""
    (tmp_path / ".env").write_text("REAP_BACKEND=sandbox\n")
    assert catalogue_probe.main([]) == 2
    assert "REAP_BACKEND=sandbox needs REAP_API_KEY" in capsys.readouterr().out


async def test_unjudged_credits_are_counted_apart_from_failures(reap: ReapHttpClient) -> None:
    """Without an estimate a credit is valued, a plan with no Amount still fails rule 10."""
    probe = await _probe(reap, COMPUTE, quote=True)
    text = catalogue_probe.render(probe, COMPUTE)
    assert "states no Amount)" in text
    assert "Candidates passing rule 10: 0 of 8, 0 from the grant's merchants; 6 valued, " in text
    assert "(items USD 100.00, shipping -, tax USD 8.26 incl., Service fee USD 2.50)" in text


MACHINE = """\
machine:
  name: Storage node sn-01
bom:
  drive_failing:
    part: 1 TB NVMe M.2 2280 SSD
    query: "1TB NVMe M.2 2280 SSD"
    price: {min: "40", max: "200"}
    accept:
      Capacity: ["1 TB"]
      Interface: ["NVMe"]
  fan_stopped:
    part: 120 mm case fan
    query: "120mm case fan"
    accept:
      Size: ["120 mm"]
"""


async def test_the_machines_parts_are_searched_under_the_part_file(
    reap: ReapHttpClient, tmp_path: Path
) -> None:
    """Shape (b)'s parts live in the machine's bill of materials; each is probed as bought."""
    machine = tmp_path / "machine.yaml"
    machine.write_text(MACHINE)
    parts = catalogue_probe.bom_purchases(machine, PART)
    assert [label for label, _ in parts] == [
        f"{machine}: drive_failing, 1 TB NVMe M.2 2280 SSD",
        f"{machine}: fan_stopped, 120 mm case fan",
    ]
    drive, fan = (purchase for _, purchase in parts)
    assert drive.search is not None
    assert fan.search is not None
    assert drive.merchants == PART.merchants
    assert fan.search.price is None
    assert fan.search.context == PART.search.context if PART.search else False
    probe = await _probe(reap, drive)
    assert _variants(probe)["var-northwind-nvme-2tb"] == (False, True)
    fans = await _probe(reap, fan)
    assert "var-northwind-fan-120-premium" in _variants(fans)
    assert all(fills for fills, _ in _variants(fans).values())


def test_a_machine_file_that_does_not_load_is_named(tmp_path: Path) -> None:
    """A broken bill of materials is reported, not raised."""
    machine = tmp_path / "machine.yaml"
    machine.write_text("bom: {drive_failing: {part: x}}\n")
    with pytest.raises(catalogue_probe.ProbeError, match=f"^{machine}: "):
        catalogue_probe.bom_purchases(machine, PART)


def test_main_also_probes_the_machine_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``AGENT_MACHINE_CONFIG`` names the machine the live session reads; its parts are probed."""
    machine = tmp_path / "sn-01.yaml"
    machine.write_text(MACHINE)
    monkeypatch.setenv("AGENT_MACHINE_CONFIG", str(machine))
    monkeypatch.setenv("PURCHASE_CONFIG", str(PURCHASE_CONFIG_DIR / "purchase-part.yaml"))
    assert catalogue_probe.main(["--no-snapshot"]) == 0
    out = capsys.readouterr().out
    assert f"== {machine}: fan_stopped, 120 mm case fan" in out
    assert 'Search "120mm case fan" (SG, USD): 1 product from 1 merchant; ' in out


def test_main_reads_the_catalogue_through_kwal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On ``REAP_BACKEND=kwal`` the same reads go through the participant gateway."""
    monkeypatch.setenv("REAP_PURCHASE_PATH", "agentic")
    _, session = stand_in_kwal(monkeypatch, ManualClock(START), tmp_path)
    (tmp_path / ".env").write_text(f"REAP_BACKEND=kwal\nPWS_CREDENTIALS_FILE={session}\n")
    part = str(PURCHASE_CONFIG_DIR / "purchase-part.yaml")
    assert catalogue_probe.main(["--config", part, "--no-snapshot"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Catalogue probe on kwal: read-only (search, details, variant)")
    assert "var-northwind-nvme-1tb" in out


def test_main_refuses_kwal_without_its_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Kwal is probed only with the session the skill saved; nothing is sent without it."""
    monkeypatch.setenv("REAP_PURCHASE_PATH", "agentic")
    (tmp_path / ".env").write_text(f"REAP_BACKEND=kwal\nPWS_CREDENTIALS_FILE={tmp_path}/none\n")
    assert catalogue_probe.main([]) == 2
    assert "REAP_BACKEND=kwal: no Kwal credentials at" in capsys.readouterr().out

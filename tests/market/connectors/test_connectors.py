"""Tests for every connector's request and its parser against the recorded responses."""

import json
from datetime import UTC, datetime
from decimal import Decimal

from youreapyousow.domain import Availability, BillingGranularity, ResourceSpec, Source
from youreapyousow.market.connectors.base import FetchMeta, sha256
from youreapyousow.market.connectors.openrouter import OpenRouterConnector
from youreapyousow.market.connectors.references import AkashConnector, OrnnConnector
from youreapyousow.market.connectors.runpod import RunPodConnector
from youreapyousow.market.connectors.shadeform import ShadeformConnector
from youreapyousow.market.connectors.vast import VastConnector

AT = datetime(2037, 9, 27, 2, 0, tzinfo=UTC)


def _meta(raw: bytes) -> FetchMeta:
    return FetchMeta(Source.MOCK, AT, None, sha256(raw))


def test_vast_parses_recorded_offers() -> None:
    """Vast's dollars per hour and MiB become the common shape."""
    connector = VastConnector()
    raw = connector.fixture_bytes()
    offers = connector.parse(json.loads(raw), _meta(raw))
    assert len(offers) == 10
    first = offers[0]
    assert (first.provider, first.offer_id, first.gpu_name) == ("vast", "54420292", "Tesla V100")
    assert first.vram_gb == 32
    assert first.price_usd_per_hour == Decimal("0.06296296296296297")
    assert first.billing_granularity == BillingGranularity.PER_SECOND
    assert first.region == "California, US"
    assert first.raw_ref == f"sha256:{sha256(raw)}#0"
    assert all(o.availability == Availability.AVAILABLE for o in offers)


def test_vast_request_pushes_the_spec_down() -> None:
    """The spec's count, memory and price ceiling become Vast filters."""
    spec = ResourceSpec(min_vram_gb=24, max_price_usd_per_hour=Decimal("1.00"))
    request = VastConnector().request(spec)
    body = json.loads(request.content)
    assert request.method == "POST"
    assert body["num_gpus"] == {"eq": 1}
    assert body["gpu_ram"] == {"gte": 24576}
    assert body["dph_total"] == {"lte": 1.0}
    assert body["order"] == [["dph_total", "asc"]]
    assert body["limit"] == 10
    assert "dph_total" not in json.loads(VastConnector().request(ResourceSpec()).content)


def test_runpod_parses_priced_types_and_stock() -> None:
    """Only priced GPU types become offers; stock maps to availability."""
    connector = RunPodConnector()
    raw = connector.fixture_bytes()
    offers = {o.gpu_name: o for o in connector.parse(json.loads(raw), _meta(raw))}
    rtx = offers["RTX 4090"]
    assert (rtx.offer_id, rtx.vram_gb, rtx.price_usd_per_hour) == (
        "NVIDIA GeForce RTX 4090",
        24,
        Decimal("0.34"),
    )
    assert rtx.availability == Availability.LOW
    assert offers["A40"].availability == Availability.AVAILABLE
    assert "A100 PCIe" not in offers
    assert connector.cache_key(ResourceSpec()) == connector.cache_key(ResourceSpec(min_vram_gb=80))


def test_shadeform_parses_cents_regions_and_boot_time() -> None:
    """Cents become dollars; the first open region names the offer's location."""
    connector = ShadeformConnector()
    raw = connector.fixture_bytes()
    offers = connector.parse(json.loads(raw), _meta(raw))
    first = offers[0]
    assert (first.offer_id, first.gpu_name, first.vram_gb) == ("hyperstack:A6000", "A6000", 48)
    assert first.price_usd_per_hour == Decimal("0.50")
    assert first.region == "CA, Montreal"
    assert first.boot_seconds == 480
    assert first.billing_granularity == BillingGranularity.PER_HOUR
    request = connector.request(ResourceSpec())
    assert dict(request.url.params) == {"num_gpus": "1", "available": "true", "sort": "price"}


def test_akash_and_ornn_are_references_not_offers() -> None:
    """Reference sources yield prices to compare against, never offers to buy."""
    akash = AkashConnector()
    raw = akash.fixture_bytes()
    refs = akash.parse(json.loads(raw), _meta(raw))
    assert refs
    assert all(r.statistic == "weighted_average_bid" for r in refs)
    a100 = next(r for r in refs if r.gpu == "nvidia a100 80Gi")
    assert a100.usd_per_gpu_hour == Decimal("1.33")

    ornn = OrnnConnector()
    raw = ornn.fixture_bytes()
    index = {r.gpu: r for r in ornn.parse(json.loads(raw), _meta(raw))}
    assert index["H100 SXM"].usd_per_gpu_hour == Decimal("2.52")
    assert index["H100 SXM"].as_of == datetime(2026, 10, 5, 20, 0, tzinfo=UTC)


def test_openrouter_converts_per_token_strings_and_skips_routers() -> None:
    """Per-token string prices become per-million decimals; ``-1`` routers are skipped."""
    connector = OpenRouterConnector()
    raw = connector.fixture_bytes()
    models = {m.model_id: m for m in connector.parse(json.loads(raw), _meta(raw))}
    assert "typesafe/jev-router" not in models
    deepseek = models["deepseek/deepseek-chat-v3.1"]
    assert deepseek.prompt_usd_per_mtok == Decimal("0.25")
    assert deepseek.completion_usd_per_mtok == Decimal("0.95")
    assert any(m.prompt_usd_per_mtok == 0 for m in models.values())

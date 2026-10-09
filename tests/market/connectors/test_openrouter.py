"""Tests for OpenRouter's per-model endpoints connector against the recorded response."""

import json
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import JsonValue

from youreapyousow.domain import Availability, ResourceSpec, Source
from youreapyousow.market.connectors.base import FetchMeta, sha256
from youreapyousow.market.connectors.openrouter import OpenRouterEndpointsConnector

AT = datetime(2037, 9, 27, 4, 29, tzinfo=UTC)
LLAMA = "meta-llama/llama-3.3-70b-instruct"


def _parse(connector: OpenRouterEndpointsConnector) -> list[object]:
    raw = connector.fixture_bytes()
    return list(connector.parse(json.loads(raw), FetchMeta(Source.MOCK, AT, None, sha256(raw))))


def test_request_and_cache_key_are_per_model() -> None:
    """Each model has its own URL and its own snapshot."""
    connector = OpenRouterEndpointsConnector(LLAMA)
    request = connector.request(ResourceSpec())
    assert str(request.url) == f"https://openrouter.ai/api/v1/models/{LLAMA}/endpoints"
    assert connector.cache_key(ResourceSpec()) == f"openrouter_endpoints:{LLAMA}"
    other = OpenRouterEndpointsConnector("openai/gpt-oss-120b")
    assert other.cache_key(ResourceSpec()) != connector.cache_key(ResourceSpec())


def test_parses_each_provider_at_its_own_price() -> None:
    """Every serving provider becomes an endpoint priced per million tokens."""
    connector = OpenRouterEndpointsConnector(LLAMA)
    raw = connector.fixture_bytes()
    meta = FetchMeta(Source.MOCK, AT, None, sha256(raw))
    endpoints = connector.parse(json.loads(raw), meta)
    first = endpoints[0]
    assert (first.venue, first.model_id, first.serving_provider, first.tag) == (
        "openrouter",
        LLAMA,
        "DeepInfra",
        "deepinfra/turbo",
    )
    assert first.prompt_usd_per_mtok == Decimal("0.1")
    assert first.completion_usd_per_mtok == Decimal("0.32")
    assert first.raw_ref == f"sha256:{sha256(raw)}#0"
    assert first.first_token_latency_ms is None
    assert len({e.tag for e in endpoints}) == len(endpoints)


def test_degraded_endpoints_are_marked_low() -> None:
    """A negative status marks the endpoint degraded rather than dropping it."""
    payload: JsonValue = {
        "data": {
            "id": "m/x",
            "endpoints": [
                {
                    "provider_name": "A",
                    "tag": "a",
                    "pricing": {"prompt": "1e-7", "completion": "2e-7"},
                    "status": -2,
                },
                {
                    "provider_name": "A",
                    "tag": "a",
                    "pricing": {"prompt": "1e-7", "completion": "2e-7"},
                    "status": 0,
                },
                {"provider_name": "R", "tag": "r", "pricing": {"prompt": "-1", "completion": "-1"}},
                {
                    "provider_name": "B",
                    "tag": "b",
                    "pricing": {"prompt": "3e-7", "completion": "4e-7"},
                    "latency_last_30m": 412.5,
                    "throughput_last_30m": 88.2,
                },
            ],
        }
    }
    endpoints = OpenRouterEndpointsConnector("m/x").parse(
        payload, FetchMeta(Source.LIVE, AT, 120, "abc")
    )
    assert [e.tag for e in endpoints] == ["a", "b"]
    assert endpoints[0].availability == Availability.LOW
    assert endpoints[1].first_token_latency_ms == Decimal("412.5")
    assert endpoints[1].throughput_tps == Decimal("88.2")


def test_unrecorded_model_has_an_empty_fixture() -> None:
    """A model missing from the combined fixture parses to no endpoints, not an error."""
    assert _parse(OpenRouterEndpointsConnector("nobody/nothing")) == []

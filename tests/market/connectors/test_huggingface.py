"""Tests for the Hugging Face router connector against the recorded response."""

import json
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import JsonValue

from youreapyousow.domain import Availability, ResourceSpec, Source
from youreapyousow.market.connectors.base import FetchMeta, sha256
from youreapyousow.market.connectors.huggingface import URL, HuggingFaceRouterConnector

AT = datetime(2037, 9, 27, 4, 29, tzinfo=UTC)


def test_parses_priced_providers_with_their_speed() -> None:
    """Each priced provider carries its price, first-token latency and throughput."""
    connector = HuggingFaceRouterConnector()
    raw = connector.fixture_bytes()
    endpoints = connector.parse(json.loads(raw), FetchMeta(Source.MOCK, AT, None, sha256(raw)))
    oss = [e for e in endpoints if e.model_id == "openai/gpt-oss-120b"]
    groq = next(e for e in oss if e.serving_provider == "groq")
    assert (groq.venue, groq.tag) == ("huggingface", "groq")
    assert groq.prompt_usd_per_mtok == Decimal("0.15")
    assert groq.first_token_latency_ms is not None
    assert groq.throughput_tps is not None
    assert groq.raw_ref.startswith(f"sha256:{sha256(raw)}#")
    assert all(e.prompt_usd_per_mtok >= 0 for e in endpoints)


def test_unpriced_providers_are_skipped_and_status_mapped() -> None:
    """A provider with no price cannot be bought; a non-live one is marked low."""
    payload: JsonValue = {
        "data": [
            {
                "id": "org/model",
                "providers": [
                    {"provider": "free", "status": "live"},
                    {
                        "provider": "slow",
                        "status": "staging",
                        "pricing": {"input": 0.1, "output": 0.2},
                    },
                ],
            }
        ]
    }
    endpoints = HuggingFaceRouterConnector().parse(payload, FetchMeta(Source.LIVE, AT, 9, "abc"))
    assert [e.serving_provider for e in endpoints] == ["slow"]
    assert endpoints[0].availability == Availability.LOW
    assert endpoints[0].raw_ref == "sha256:abc#0.1"
    assert endpoints[0].first_token_latency_ms is None


def test_request_is_constant() -> None:
    """The router lists every model in one call."""
    connector = HuggingFaceRouterConnector()
    assert str(connector.request(ResourceSpec()).url) == URL
    assert connector.cache_key(ResourceSpec(min_vram_gb=80)) == "huggingface"

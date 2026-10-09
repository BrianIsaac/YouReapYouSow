"""Tests for the LiteLLM price-file connector against the recorded file."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import JsonValue

from youreapyousow.domain import ResourceSpec, Source
from youreapyousow.market.connectors.base import FetchMeta, sha256
from youreapyousow.market.connectors.litellm import URL, LiteLLMPricesConnector

AT = datetime(2037, 9, 27, 4, 29, tzinfo=UTC)


def test_reads_only_priced_openrouter_entries() -> None:
    """``openrouter/`` entries become model offers; other providers are ignored."""
    connector = LiteLLMPricesConnector()
    raw = connector.fixture_bytes()
    offers = {
        o.model_id: o
        for o in connector.parse(json.loads(raw), FetchMeta(Source.MOCK, AT, None, sha256(raw)))
    }
    assert "gpt-4o-mini" not in offers
    assert "sample_spec" not in offers
    llama = offers["meta-llama/llama-3.3-70b-instruct"]
    assert llama.provider == "openrouter"
    assert (llama.prompt_usd_per_mtok, llama.completion_usd_per_mtok) == (
        Decimal("0.1"),
        Decimal("0.32"),
    )
    assert llama.context_length == 131072
    assert llama.source == Source.MOCK


def test_request_is_constant() -> None:
    """The file is one download whatever the spec."""
    connector = LiteLLMPricesConnector()
    assert str(connector.request(ResourceSpec()).url) == URL
    assert connector.cache_key(ResourceSpec(min_vram_gb=80)) == "litellm"


def test_malformed_entries_are_skipped_and_a_non_object_refused() -> None:
    """A wrongly typed entry is skipped; a file that is not an object is an error."""
    meta = FetchMeta(Source.LIVE, AT, 50, "abc")
    payload: JsonValue = {
        "openrouter/a/b": {"litellm_provider": "openrouter", "input_cost_per_token": "x"},
        "openrouter/c/d": {"litellm_provider": "openrouter", "input_cost_per_token": 1e-6},
    }
    assert LiteLLMPricesConnector().parse(payload, meta) == []
    with pytest.raises(ValueError, match="not a JSON object"):
        LiteLLMPricesConnector().parse([], meta)

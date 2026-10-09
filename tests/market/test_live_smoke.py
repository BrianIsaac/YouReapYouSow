"""Opt-in tests against the real keyless endpoints: ``uv run pytest -m live``.

Excluded from the default run so the suite never depends on the network. The first test
checks that each live response still parses into the shape the fixtures encode. The
second records the GPU market's fixtures (the compute connectors and the two market
references) once, from the live answers to the default spec, and runs only with
``RECORD_FIXTURES=1``: ``RECORD_FIXTURES=1 uv run pytest -m live tests/market``. The
model sources have their own recorder in ``tests/ml/test_market_live.py``.
"""

import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from youreapyousow.clock import utc_now
from youreapyousow.domain import ResourceSpec, Source
from youreapyousow.market import fixtures
from youreapyousow.market.connectors.base import FetchMeta, sha256
from youreapyousow.market.service import DEFAULT_SPEC, MarketService

pytestmark = [pytest.mark.anyio, pytest.mark.live]

FIXTURES = Path(fixtures.__file__).parent


async def test_live_endpoints_still_parse() -> None:
    """Every connector answers live and parses into at least one item."""
    async with httpx.AsyncClient() as http:
        service = MarketService(http, timeout_s=10.0)
        for connector in service.compute:
            result = await service.fetch(connector, ResourceSpec(min_vram_gb=24))
            assert result.source == Source.LIVE, (connector.name, result.error)
            assert result.items, connector.name
        for connector in service.references:
            assert (await service.fetch(connector, ResourceSpec())).source == Source.LIVE
        assert (await service.fetch(service.models, ResourceSpec())).source == Source.LIVE


async def test_record_the_gpu_market_fixtures() -> None:
    """Record each GPU market fixture from its live answer, once it is seen to parse."""
    if os.environ.get("RECORD_FIXTURES") != "1":
        pytest.skip("set RECORD_FIXTURES=1 to re-record the GPU market fixtures")
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
        service = MarketService(http)
        recorded: dict[str, bytes] = {}
        for connector in [*service.compute, *service.references]:
            raw = (await http.send(connector.request(DEFAULT_SPEC))).raise_for_status().content
            payload: JsonValue = json.loads(raw)
            items = connector.parse(payload, FetchMeta(Source.LIVE, utc_now(), None, sha256(raw)))
            assert items, connector.name
            recorded[connector.fixture] = (json.dumps(payload, indent=1) + "\n").encode()
    for name, body in recorded.items():
        (FIXTURES / name).write_bytes(body)

"""Tests for discovery's live, cached and mock fallbacks."""

import asyncio
import json
from collections.abc import Callable, Coroutine
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from youreapyousow.clock import ManualClock
from youreapyousow.domain import ResourceSpec, Source
from youreapyousow.market.connectors.runpod import RunPodConnector
from youreapyousow.market.connectors.shadeform import ShadeformConnector
from youreapyousow.market.connectors.vast import VastConnector
from youreapyousow.market.service import MarketMode, MarketService

pytestmark = pytest.mark.anyio

FIXTURES = {
    "console.vast.ai": VastConnector().fixture_bytes(),
    "api.runpod.io": RunPodConnector().fixture_bytes(),
    "api.shadeform.ai": ShadeformConnector().fixture_bytes(),
}
SPEC = ResourceSpec(min_vram_gb=24, max_price_usd_per_hour=Decimal("1.00"))

type Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


def _service(
    handler: Handler,
    clock: ManualClock,
    *,
    mode: MarketMode = MarketMode.LIVE,
    snapshot_path: Path | None = None,
) -> MarketService:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MarketService(http, mode=mode, timeout_s=0.2, snapshot_path=snapshot_path, clock=clock)


async def _serve_fixtures(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=FIXTURES[request.url.host])


async def test_live_discovery_merges_filters_and_sorts(clock: ManualClock) -> None:
    """Three providers answer; offers are merged, filtered by the spec and sorted."""
    service = _service(_serve_fixtures, clock)
    offers = await service.discover_resources(SPEC)
    assert {o.provider for o in offers} == {"vast", "runpod", "shadeform"}
    assert all(o.source == Source.LIVE for o in offers)
    assert all(o.latency_ms is not None for o in offers)
    assert all(o.vram_gb >= 24 and o.price_usd_per_hour <= 1 for o in offers)
    prices = [o.price_usd_per_hour for o in offers]
    assert prices == sorted(prices)
    assert service.status()["vast"]["source"] == "live"


async def test_failure_falls_back_to_cached_then_mock(clock: ManualClock) -> None:
    """After one good fetch, a failing provider serves its snapshot, marked cached."""
    healthy = True

    async def flaky(request: httpx.Request) -> httpx.Response:
        if not healthy and request.url.host == "api.runpod.io":
            return httpx.Response(503)
        return await _serve_fixtures(request)

    service = _service(flaky, clock)
    await service.discover_resources(SPEC)
    healthy = False
    offers = await service.discover_resources(SPEC)
    sources = {o.provider: o.source for o in offers}
    assert sources["runpod"] == Source.CACHED
    assert sources["vast"] == Source.LIVE
    assert "HTTPStatusError" in (service.last["runpod"].error or "")

    cold = _service(flaky, clock)
    offers = await cold.discover_resources(SPEC)
    assert {o.source for o in offers if o.provider == "runpod"} == {Source.MOCK}


async def test_slow_provider_times_out_to_mock_within_budget(clock: ManualClock) -> None:
    """A provider slower than its budget does not hold discovery up."""

    async def slow_vast(request: httpx.Request) -> httpx.Response:
        if request.url.host == "console.vast.ai":
            await asyncio.sleep(5)
        return await _serve_fixtures(request)

    service = _service(slow_vast, clock)
    started = asyncio.get_running_loop().time()
    offers = await service.discover_resources(SPEC)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert {o.source for o in offers if o.provider == "vast"} == {Source.MOCK}
    assert service.last["vast"].error == "TimeoutError: no answer within 0.2 s"


async def test_garbage_body_is_not_trusted(clock: ManualClock) -> None:
    """A 200 with an unexpected shape falls back rather than producing bad offers."""

    async def garbage(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    offers = await _service(garbage, clock).discover_resources(SPEC)
    assert offers
    assert {o.source for o in offers} == {Source.MOCK}


async def test_mock_mode_never_touches_the_network(clock: ManualClock) -> None:
    """In mock mode every offer comes from the fixtures."""

    async def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("network used in mock mode")

    service = _service(forbidden, clock, mode=MarketMode.MOCK)
    offers = await service.discover_resources(SPEC)
    assert {o.source for o in offers} == {Source.MOCK}
    assert await service.market_references()
    assert await service.discover_models()


async def test_snapshot_survives_a_restart(clock: ManualClock, tmp_path: Path) -> None:
    """A snapshot written by one process is served as cached by the next."""
    path = tmp_path / "var" / "snapshot.json"
    await _service(_serve_fixtures, clock, snapshot_path=path).discover_resources(SPEC)
    assert set(json.loads(path.read_text())) >= {"runpod"}

    async def offline(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    offers = await _service(offline, clock, snapshot_path=path).discover_resources(SPEC)
    assert {o.source for o in offers} == {Source.CACHED}


async def test_corrupt_snapshot_is_ignored(clock: ManualClock, tmp_path: Path) -> None:
    """An unreadable snapshot file degrades to mock instead of crashing."""
    path = tmp_path / "snapshot.json"
    path.write_text("{not json")

    async def offline(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    offers = await _service(offline, clock, snapshot_path=path).discover_resources(SPEC)
    assert {o.source for o in offers} == {Source.MOCK}


async def test_refresh_warms_every_connector(clock: ManualClock) -> None:
    """A refresh touches compute, reference and model sources."""

    async def everything(request: httpx.Request) -> httpx.Response:
        if request.url.host in FIXTURES:
            return await _serve_fixtures(request)
        return httpx.Response(503)

    service = _service(everything, clock)
    await service.refresh()
    status = service.status()
    assert set(status) == {"vast", "runpod", "shadeform", "akash", "ornn", "openrouter"}
    assert status["akash"]["source"] == "mock"
    assert status["vast"]["items"] == 10


async def test_refresh_forever_runs_until_cancelled(clock: ManualClock) -> None:
    """The background loop refreshes on its interval and stops on cancellation."""
    service = _service(_serve_fixtures, clock, mode=MarketMode.MOCK)
    task = asyncio.create_task(service.refresh_forever(interval_s=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "vast" in service.status()

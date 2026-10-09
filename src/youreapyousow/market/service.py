"""``discover_resources``: one interface over live, cached and mock market data.

Each connector gets its own two-second budget and all run in parallel, so discovery is
bounded at about two seconds by construction. A connector that answers is stamped
``live`` and refreshes the snapshot; one that times out, errors or returns an
unparseable body falls back to its last good snapshot, stamped ``cached``, and failing
that to its recorded fixture, stamped ``mock``. The snapshot is refreshed at startup
and every five minutes, and persisted so a restart without network still has
``cached`` data.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import httpx
from pydantic import BaseModel, JsonValue, ValidationError

from youreapyousow.clock import Clock, utc_now
from youreapyousow.domain import MarketReference, ModelOffer, Offer, ResourceSpec, Source
from youreapyousow.market.connectors.base import Connector, FetchMeta, sha256
from youreapyousow.market.connectors.openrouter import OpenRouterConnector
from youreapyousow.market.connectors.references import AkashConnector, OrnnConnector
from youreapyousow.market.connectors.runpod import RunPodConnector
from youreapyousow.market.connectors.shadeform import ShadeformConnector
from youreapyousow.market.connectors.vast import VastConnector

DEFAULT_SPEC = ResourceSpec(min_vram_gb=24)


class MarketMode(StrEnum):
    """Whether the market touches the network at all."""

    LIVE = "live"
    MOCK = "mock"


class _Snapshot(BaseModel):
    raw: str
    fetched_at: datetime
    latency_ms: int | None


@dataclass(frozen=True)
class FetchResult[T]:
    """What one connector returned, and from where.

    Attributes:
        connector: The connector's name.
        items: The normalised items.
        source: Live, cached or mock.
        fetched_at: When the underlying data was fetched.
        latency_ms: Live latency, if live.
        error: Why the live call was not used, if it was not.
    """

    connector: str
    items: list[T]
    source: Source
    fetched_at: datetime
    latency_ms: int | None
    error: str | None


@dataclass(frozen=True)
class ConnectorStatus:
    """The latest fetch of one connector, for the dashboard.

    Attributes:
        source: Live, cached or mock.
        fetched_at: When the data was fetched.
        latency_ms: Live latency, if live.
        items: How many items it produced.
        error: Why the live call was not used, if it was not.
    """

    source: Source
    fetched_at: datetime
    latency_ms: int | None
    items: int
    error: str | None


class MarketService:
    """Live price discovery with a cached snapshot and a mock of identical shape."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        mode: MarketMode = MarketMode.LIVE,
        compute: list[Connector[Offer]] | None = None,
        references: list[Connector[MarketReference]] | None = None,
        models: Connector[ModelOffer] | None = None,
        timeout_s: float = 2.0,
        snapshot_path: Path | None = None,
        clock: Clock = utc_now,
    ) -> None:
        """Create the service.

        Args:
            http: Shared HTTP client (a mock transport in tests).
            mode: ``live`` tries the network; ``mock`` never does.
            compute: Offer connectors; Vast.ai, RunPod and Shadeform by default.
            references: Reference connectors; Akash and Ornn by default.
            models: Model connector; OpenRouter by default.
            timeout_s: Per-connector budget.
            snapshot_path: Where snapshots persist, if anywhere.
            clock: Time source.
        """
        self._http = http
        self.mode = mode
        self.compute = compute or [VastConnector(), RunPodConnector(), ShadeformConnector()]
        self.references = references or [AkashConnector(), OrnnConnector()]
        self.models = models or OpenRouterConnector()
        self._timeout_s = timeout_s
        self._snapshot_path = snapshot_path
        self._clock = clock
        self._snapshots: dict[str, _Snapshot] = self._load_snapshots()
        self.last: dict[str, ConnectorStatus] = {}

    def _load_snapshots(self) -> dict[str, _Snapshot]:
        if self._snapshot_path is None or not self._snapshot_path.exists():
            return {}
        try:
            data = json.loads(self._snapshot_path.read_text())
            return {k: _Snapshot.model_validate(v) for k, v in data.items()}
        except (ValueError, ValidationError):
            return {}

    def _save_snapshots(self) -> None:
        if self._snapshot_path is None:
            return
        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump(mode="json") for k, v in self._snapshots.items()}
        tmp = self._snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self._snapshot_path)

    async def _live[T](self, connector: Connector[T], spec: ResourceSpec) -> tuple[bytes, int]:
        started = time.perf_counter()
        async with asyncio.timeout(self._timeout_s):
            response = await self._http.send(connector.request(spec))
        response.raise_for_status()
        return response.content, round((time.perf_counter() - started) * 1000)

    async def fetch[T](self, connector: Connector[T], spec: ResourceSpec) -> FetchResult[T]:
        """Fetch one connector: live, else cached, else mock.

        Args:
            connector: The source.
            spec: What is wanted.

        Returns:
            Normalised items stamped with where they came from.
        """
        key = connector.cache_key(spec)
        error = "market mode is mock"
        if self.mode == MarketMode.LIVE:
            try:
                raw, latency = await self._live(connector, spec)
                now = self._clock()
                meta = FetchMeta(Source.LIVE, now, latency, sha256(raw))
                items = connector.parse(_decode(raw), meta)
                self._snapshots[key] = _Snapshot(
                    raw=raw.decode(), fetched_at=now, latency_ms=latency
                )
                self._save_snapshots()
                return self._record(
                    FetchResult(connector.name, items, Source.LIVE, now, latency, None)
                )
            except TimeoutError:
                error = f"TimeoutError: no answer within {self._timeout_s} s"
            except (httpx.HTTPError, ValueError, ValidationError) as failure:
                error = f"{type(failure).__name__}: {failure}"[:200]
        snapshot = self._snapshots.get(key)
        if snapshot is not None:
            raw = snapshot.raw.encode()
            meta = FetchMeta(Source.CACHED, snapshot.fetched_at, snapshot.latency_ms, sha256(raw))
            items = connector.parse(_decode(raw), meta)
            return self._record(
                FetchResult(connector.name, items, Source.CACHED, snapshot.fetched_at, None, error)
            )
        raw = connector.fixture_bytes()
        now = self._clock()
        items = connector.parse(_decode(raw), FetchMeta(Source.MOCK, now, None, sha256(raw)))
        return self._record(FetchResult(connector.name, items, Source.MOCK, now, None, error))

    def _record[T](self, result: FetchResult[T]) -> FetchResult[T]:
        self.last[result.connector] = ConnectorStatus(
            source=result.source,
            fetched_at=result.fetched_at,
            latency_ms=result.latency_ms,
            items=len(result.items),
            error=result.error,
        )
        return result

    async def discover_resources(self, spec: ResourceSpec) -> list[Offer]:
        """Query every compute connector in parallel and merge what fits the spec.

        Args:
            spec: What is wanted.

        Returns:
            Matching offers from every source, cheapest first, unavailable ones dropped.
        """
        results = await asyncio.gather(*(self.fetch(c, spec) for c in self.compute))
        offers = [o for r in results for o in r.items if spec.matches(o)]
        return sorted(offers, key=lambda o: (o.price_usd_per_hour, o.provider, o.offer_id))

    async def market_references(self) -> list[MarketReference]:
        """Fetch the reference prices (Akash statistics, Ornn index).

        Returns:
            Every reference figure, stamped with its source.
        """
        results = await asyncio.gather(*(self.fetch(c, DEFAULT_SPEC) for c in self.references))
        return [ref for r in results for ref in r.items]

    async def discover_models(self) -> list[ModelOffer]:
        """Fetch priced models for the ML-service scenario.

        Returns:
            Model offers, cheapest prompt price first.
        """
        result = await self.fetch(self.models, DEFAULT_SPEC)
        return sorted(result.items, key=lambda m: (m.prompt_usd_per_mtok, m.model_id))

    async def refresh(self, spec: ResourceSpec = DEFAULT_SPEC) -> None:
        """Refresh every snapshot once; used at startup and by the background loop.

        Args:
            spec: The spec whose compute responses to refresh.
        """
        await asyncio.gather(
            self.discover_resources(spec), self.market_references(), self.discover_models()
        )

    async def refresh_forever(self, interval_s: float = 300.0) -> None:
        """Refresh snapshots every ``interval_s`` seconds until cancelled.

        Args:
            interval_s: Seconds between refreshes; five minutes by default.
        """
        while True:
            await asyncio.sleep(interval_s)
            await self.refresh()

    def status(self) -> dict[str, dict[str, str | int | None]]:
        """Summarise the latest fetch per connector for the dashboard.

        Returns:
            Per connector: source badge, fetch time, latency, item count and any
            fallback reason.
        """
        return {
            name: {
                "source": r.source.value,
                "fetched_at": r.fetched_at.isoformat(),
                "latency_ms": r.latency_ms,
                "items": r.items,
                "error": r.error,
            }
            for name, r in self.last.items()
        }


def _decode(raw: bytes) -> JsonValue:
    return json.loads(raw)

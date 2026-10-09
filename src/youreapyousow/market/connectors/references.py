"""Market references: Akash bid statistics and Ornn's daily GPU index.

Neither is a purchasable offer; both put a fair-price figure beside the chosen quote in
the audit report. Akash ``GET /v1/gpu-prices`` answers in well under a second; Ornn's
daily index is slower and belongs off the critical path, fetched once.
"""

from datetime import datetime
from decimal import Decimal

import httpx
from pydantic import BaseModel, JsonValue

from youreapyousow.domain import MarketReference, ResourceSpec
from youreapyousow.market.connectors.base import Connector, FetchMeta

AKASH_URL = "https://console-api.akash.network/v1/gpu-prices"
ORNN_URL = "https://api.ornnai.com/api/daily-index/all"


class _AkashPrice(BaseModel):
    weightedAverage: float | None = None  # noqa: N815 - Akash's field name


class _AkashModel(BaseModel):
    vendor: str
    model: str
    ram: str
    price: _AkashPrice | None = None


class _AkashResponse(BaseModel):
    models: list[_AkashModel]


class AkashConnector(Connector[MarketReference]):
    """Akash marketplace bid statistics per GPU model."""

    name = "akash"
    fixture = "akash.json"

    def request(self, spec: ResourceSpec) -> httpx.Request:
        """Build the price-statistics request.

        Args:
            spec: Unused.

        Returns:
            The GET request.
        """
        return httpx.Request("GET", AKASH_URL)

    def cache_key(self, spec: ResourceSpec) -> str:
        """Return a constant key.

        Args:
            spec: Unused.

        Returns:
            The connector name.
        """
        return self.name

    def parse(self, payload: JsonValue, meta: FetchMeta) -> list[MarketReference]:
        """Take the weighted average bid per model that has one.

        Args:
            payload: The decoded response.
            meta: Provenance.

        Returns:
            One reference per priced model.
        """
        return [
            MarketReference(
                source_name=self.name,
                gpu=f"{m.vendor} {m.model} {m.ram}",
                statistic="weighted_average_bid",
                usd_per_gpu_hour=Decimal(str(m.price.weightedAverage)),
                as_of=meta.fetched_at,
                source=meta.source,
            )
            for m in _AkashResponse.model_validate(payload).models
            if m.price is not None and m.price.weightedAverage is not None
        ]


class _OrnnPoint(BaseModel):
    gpu_type: str
    index_value: float


class _OrnnResponse(BaseModel):
    date: datetime
    data: list[_OrnnPoint]


class OrnnConnector(Connector[MarketReference]):
    """Ornn's transaction-based daily compute price index."""

    name = "ornn"
    fixture = "ornn.json"

    def request(self, spec: ResourceSpec) -> httpx.Request:
        """Build the daily-index request.

        Args:
            spec: Unused.

        Returns:
            The GET request.
        """
        return httpx.Request("GET", ORNN_URL)

    def cache_key(self, spec: ResourceSpec) -> str:
        """Return a constant key.

        Args:
            spec: Unused.

        Returns:
            The connector name.
        """
        return self.name

    def parse(self, payload: JsonValue, meta: FetchMeta) -> list[MarketReference]:
        """Read the day's index values.

        Args:
            payload: The decoded response.
            meta: Provenance.

        Returns:
            One reference per indexed GPU.
        """
        parsed = _OrnnResponse.model_validate(payload)
        return [
            MarketReference(
                source_name=self.name,
                gpu=point.gpu_type,
                statistic="daily_index",
                usd_per_gpu_hour=Decimal(str(point.index_value)),
                as_of=parsed.date,
                source=meta.source,
            )
            for point in parsed.data
        ]

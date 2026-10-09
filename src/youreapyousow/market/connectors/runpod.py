"""RunPod: per-GPU-type prices and stock from the public GraphQL endpoint.

``POST https://api.runpod.io/graphql`` answers without a key. GraphQL is reported to be
retiring, so the route may close. Only single-GPU prices are asked for
(``gpuCount: 1``), so RunPod offers match only single-GPU specs. A null
``stockStatus`` is read as no stock.
"""

from decimal import Decimal

import httpx
from pydantic import BaseModel, JsonValue

from youreapyousow.domain import Availability, BillingGranularity, Offer, ResourceSpec
from youreapyousow.market.connectors.base import Connector, FetchMeta

URL = "https://api.runpod.io/graphql"
QUERY = (
    "{ gpuTypes { id displayName memoryInGb securePrice communityPrice "
    "lowestPrice(input:{gpuCount:1}) { minimumBidPrice uninterruptablePrice stockStatus } } }"
)
_STOCK = {
    "High": Availability.AVAILABLE,
    "Medium": Availability.AVAILABLE,
    "Low": Availability.LOW,
}


class _LowestPrice(BaseModel):
    uninterruptablePrice: float | None = None  # noqa: N815 - RunPod's field name
    stockStatus: str | None = None  # noqa: N815 - RunPod's field name


class _GpuType(BaseModel):
    id: str
    displayName: str  # noqa: N815 - RunPod's field name
    memoryInGb: int  # noqa: N815 - RunPod's field name
    lowestPrice: _LowestPrice | None = None  # noqa: N815 - RunPod's field name


class _Data(BaseModel):
    gpuTypes: list[_GpuType]  # noqa: N815 - RunPod's field name


class _RunPodResponse(BaseModel):
    data: _Data


class RunPodConnector(Connector[Offer]):
    """RunPod GPU types."""

    name = "runpod"
    fixture = "runpod.json"

    def request(self, spec: ResourceSpec) -> httpx.Request:
        """Build the GraphQL query; it lists every GPU type regardless of the spec.

        Args:
            spec: Unused; filtering happens after parsing.

        Returns:
            The POST request.
        """
        return httpx.Request("POST", URL, json={"query": QUERY})

    def cache_key(self, spec: ResourceSpec) -> str:
        """Return a constant key, since the request ignores the spec.

        Args:
            spec: Unused.

        Returns:
            The connector name.
        """
        return self.name

    def parse(self, payload: JsonValue, meta: FetchMeta) -> list[Offer]:
        """Normalise GPU types that carry an on-demand price.

        Args:
            payload: The decoded response.
            meta: Provenance.

        Returns:
            One single-GPU offer per priced type.
        """
        offers: list[Offer] = []
        for index, raw in enumerate(_RunPodResponse.model_validate(payload).data.gpuTypes):
            lowest = raw.lowestPrice or _LowestPrice()
            if not lowest.uninterruptablePrice:
                continue
            offers.append(
                Offer(
                    provider=self.name,
                    offer_id=raw.id,
                    gpu_name=raw.displayName,
                    gpu_count=1,
                    vram_gb=raw.memoryInGb,
                    price_usd_per_hour=Decimal(str(lowest.uninterruptablePrice)),
                    billing_granularity=BillingGranularity.PER_SECOND,
                    region=None,
                    availability=_STOCK.get(lowest.stockStatus or "", Availability.UNAVAILABLE),
                    source=meta.source,
                    fetched_at=meta.fetched_at,
                    latency_ms=meta.latency_ms,
                    raw_ref=meta.ref(index),
                )
            )
        return offers

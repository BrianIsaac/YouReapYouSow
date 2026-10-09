"""Shadeform: live availability across ten-plus clouds from one catalogue call.

``GET https://api.shadeform.ai/v1/instances/types`` answers a filtered keyless query;
the docs say a key is required, so the keyless route may close. ``hourly_price`` is in
US cents. Shadeform's metering granularity is not documented per cloud, so offers are
marked hourly, the cautious assumption for cost.
"""

from decimal import Decimal

import httpx
from pydantic import BaseModel, JsonValue

from youreapyousow.domain import Availability, BillingGranularity, Offer, ResourceSpec
from youreapyousow.market.connectors.base import Connector, FetchMeta

URL = "https://api.shadeform.ai/v1/instances/types"


class _Configuration(BaseModel):
    vram_per_gpu_in_gb: int


class _Region(BaseModel):
    region: str
    available: bool
    display_name: str | None = None


class _BootTime(BaseModel):
    max_boot_in_sec: int | None = None


class _InstanceType(BaseModel):
    cloud: str
    shade_instance_type: str
    gpu_type: str
    num_gpus: int
    hourly_price: int
    configuration: _Configuration
    availability: list[_Region]
    boot_time: _BootTime | None = None


class _ShadeformResponse(BaseModel):
    instance_types: list[_InstanceType]


class ShadeformConnector(Connector[Offer]):
    """Shadeform instance-type catalogue."""

    name = "shadeform"
    fixture = "shadeform.json"

    def request(self, spec: ResourceSpec) -> httpx.Request:
        """Ask for available types with the spec's GPU count, cheapest first.

        Args:
            spec: What is wanted.

        Returns:
            The GET request.
        """
        params = {"num_gpus": spec.gpu_count, "available": "true", "sort": "price"}
        return httpx.Request("GET", URL, params=params)

    def parse(self, payload: JsonValue, meta: FetchMeta) -> list[Offer]:
        """Normalise instance types; the first available region is the offer's region.

        Args:
            payload: The decoded response.
            meta: Provenance.

        Returns:
            One offer per instance type.
        """
        offers: list[Offer] = []
        for index, raw in enumerate(_ShadeformResponse.model_validate(payload).instance_types):
            open_regions = [r for r in raw.availability if r.available]
            region = open_regions[0] if open_regions else None
            offers.append(
                Offer(
                    provider=self.name,
                    offer_id=f"{raw.cloud}:{raw.shade_instance_type}",
                    gpu_name=raw.gpu_type,
                    gpu_count=raw.num_gpus,
                    vram_gb=raw.configuration.vram_per_gpu_in_gb,
                    price_usd_per_hour=Decimal(raw.hourly_price) / 100,
                    billing_granularity=BillingGranularity.PER_HOUR,
                    region=(region.display_name or region.region) if region else None,
                    availability=Availability.AVAILABLE if region else Availability.UNAVAILABLE,
                    source=meta.source,
                    fetched_at=meta.fetched_at,
                    latency_ms=meta.latency_ms,
                    raw_ref=meta.ref(index),
                    boot_seconds=raw.boot_time.max_boot_in_sec if raw.boot_time else None,
                )
            )
        return offers

"""Vast.ai: per-machine offers from the public bundles search.

``POST https://console.vast.ai/api/v0/bundles/`` answers without a key, although the
docs say every endpoint needs a bearer key; treat keyless access as liable to close.
``dph_total`` is USD per hour for the whole offer, ``gpu_ram`` is MiB, and billing is
per second.
"""

from decimal import Decimal

import httpx
from pydantic import BaseModel, JsonValue

from youreapyousow.domain import (
    Availability,
    BillingGranularity,
    Offer,
    ResourceSpec,
)
from youreapyousow.market.connectors.base import Connector, FetchMeta

URL = "https://console.vast.ai/api/v0/bundles/"


class _VastOffer(BaseModel):
    id: int
    gpu_name: str
    num_gpus: int
    gpu_ram: float
    dph_total: float
    geolocation: str | None = None
    rentable: bool = True


class _VastResponse(BaseModel):
    offers: list[_VastOffer]


class VastConnector(Connector[Offer]):
    """Vast.ai offer search."""

    name = "vast"
    fixture = "vast.json"

    def request(self, spec: ResourceSpec) -> httpx.Request:
        """Build the bundles search, pushing the spec's filters to Vast.

        Args:
            spec: What is wanted.

        Returns:
            The POST request.
        """
        query: dict[str, object] = {
            "num_gpus": {"eq": spec.gpu_count},
            "gpu_ram": {"gte": spec.min_vram_gb * 1024},
            "rentable": {"eq": True},
            "order": [["dph_total", "asc"]],
            "limit": 10,
            "type": "on-demand",
        }
        if spec.max_price_usd_per_hour is not None:
            query["dph_total"] = {"lte": float(spec.max_price_usd_per_hour)}
        return httpx.Request("POST", URL, json=query)

    def parse(self, payload: JsonValue, meta: FetchMeta) -> list[Offer]:
        """Normalise Vast offers.

        Args:
            payload: The decoded response.
            meta: Provenance.

        Returns:
            One offer per machine offer.
        """
        return [
            Offer(
                provider=self.name,
                offer_id=str(raw.id),
                gpu_name=raw.gpu_name,
                gpu_count=raw.num_gpus,
                vram_gb=round(raw.gpu_ram / 1024),
                price_usd_per_hour=Decimal(str(raw.dph_total)),
                billing_granularity=BillingGranularity.PER_SECOND,
                region=raw.geolocation,
                availability=Availability.AVAILABLE if raw.rentable else Availability.UNAVAILABLE,
                source=meta.source,
                fetched_at=meta.fetched_at,
                latency_ms=meta.latency_ms,
                raw_ref=meta.ref(index),
            )
            for index, raw in enumerate(_VastResponse.model_validate(payload).offers)
        ]

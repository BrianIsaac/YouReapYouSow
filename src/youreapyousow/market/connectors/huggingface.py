"""The Hugging Face inference router: the second column beside OpenRouter.

``GET https://router.huggingface.co/v1/models`` needs no key. Each model lists the
providers serving it with a price in USD per million tokens and, for most, a measured
first-token latency and throughput, which OpenRouter does not publish without a key. A
provider without a price is listed but cannot be priced, so it is skipped.
"""

import httpx
from pydantic import BaseModel, JsonValue

from youreapyousow.domain import Availability, ResourceSpec
from youreapyousow.market.connectors.base import Connector, FetchMeta
from youreapyousow.market.connectors.model_endpoints import (
    ModelEndpoint,
    decimal_or_none,
    mtok_price,
)

URL = "https://router.huggingface.co/v1/models"


class _Pricing(BaseModel):
    input: float
    output: float


class _Provider(BaseModel):
    provider: str
    status: str | None = None
    context_length: int | None = None
    pricing: _Pricing | None = None
    first_token_latency_ms: float | None = None
    throughput: float | None = None


class _Model(BaseModel):
    id: str
    providers: list[_Provider] = []


class _RouterResponse(BaseModel):
    data: list[_Model]


class HuggingFaceRouterConnector(Connector[ModelEndpoint]):
    """Hugging Face router model list with per-provider prices and speeds."""

    name = "huggingface"
    fixture = "huggingface_router.json"

    def request(self, spec: ResourceSpec) -> httpx.Request:
        """Build the model-list request.

        Args:
            spec: Unused.

        Returns:
            The GET request.
        """
        return httpx.Request("GET", URL)

    def cache_key(self, spec: ResourceSpec) -> str:
        """Return a constant key.

        Args:
            spec: Unused.

        Returns:
            The connector name.
        """
        return self.name

    def parse(self, payload: JsonValue, meta: FetchMeta) -> list[ModelEndpoint]:
        """Normalise every priced provider of every model.

        Args:
            payload: The decoded response.
            meta: Provenance; the index in the reference counts models, then providers.

        Returns:
            One endpoint per priced provider, prices already per million tokens.
        """
        endpoints: list[ModelEndpoint] = []
        for m_index, model in enumerate(_RouterResponse.model_validate(payload).data):
            for p_index, provider in enumerate(model.providers):
                if provider.pricing is None:
                    continue
                live = provider.status in (None, "live")
                endpoints.append(
                    ModelEndpoint(
                        venue="huggingface",
                        model_id=model.id,
                        serving_provider=provider.provider,
                        tag=provider.provider,
                        prompt_usd_per_mtok=mtok_price(provider.pricing.input),
                        completion_usd_per_mtok=mtok_price(provider.pricing.output),
                        context_length=provider.context_length,
                        first_token_latency_ms=decimal_or_none(provider.first_token_latency_ms),
                        throughput_tps=decimal_or_none(provider.throughput),
                        availability=Availability.AVAILABLE if live else Availability.LOW,
                        source=meta.source,
                        fetched_at=meta.fetched_at,
                        raw_ref=f"{meta.ref(m_index)}.{p_index}",
                    )
                )
        return endpoints

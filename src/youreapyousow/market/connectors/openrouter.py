"""OpenRouter: priced model discovery for the ML-service scenario.

``GET https://openrouter.ai/api/v1/models`` needs no key and is cached at Cloudflare for
120 s. Prices are USD per token as decimal strings; ``-1`` marks a router with no fixed
price, and such entries are skipped. The endpoints route then lists every provider
serving one model, each at its own price.
"""

import json
from decimal import Decimal

import httpx
from pydantic import BaseModel, JsonValue

from youreapyousow.domain import Availability, ModelOffer, ResourceSpec
from youreapyousow.market.connectors.base import Connector, FetchMeta
from youreapyousow.market.connectors.model_endpoints import (
    ModelEndpoint,
    decimal_or_none,
    per_million,
)

URL = "https://openrouter.ai/api/v1/models"
PER_MILLION = Decimal(1_000_000)


class _Pricing(BaseModel):
    prompt: str
    completion: str


class _Model(BaseModel):
    id: str
    name: str
    context_length: int | None = None
    pricing: _Pricing


class _OpenRouterResponse(BaseModel):
    data: list[_Model]


class OpenRouterConnector(Connector[ModelOffer]):
    """OpenRouter model list."""

    name = "openrouter"
    fixture = "openrouter.json"

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

    def parse(self, payload: JsonValue, meta: FetchMeta) -> list[ModelOffer]:
        """Normalise priced models to USD per million tokens.

        Args:
            payload: The decoded response.
            meta: Provenance.

        Returns:
            One offer per model with a fixed, non-negative price.
        """
        offers: list[ModelOffer] = []
        for model in _OpenRouterResponse.model_validate(payload).data:
            prompt, completion = Decimal(model.pricing.prompt), Decimal(model.pricing.completion)
            if prompt < 0 or completion < 0:
                continue
            offers.append(
                ModelOffer(
                    provider=self.name,
                    model_id=model.id,
                    name=model.name,
                    prompt_usd_per_mtok=prompt * PER_MILLION,
                    completion_usd_per_mtok=completion * PER_MILLION,
                    context_length=model.context_length,
                    source=meta.source,
                    fetched_at=meta.fetched_at,
                )
            )
        return offers


ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model_id}/endpoints"


class _EndpointPricing(BaseModel):
    prompt: str
    completion: str


class _Endpoint(BaseModel):
    provider_name: str
    tag: str
    context_length: int | None = None
    pricing: _EndpointPricing
    status: int | None = None
    latency_last_30m: float | None = None
    throughput_last_30m: float | None = None


class _EndpointsData(BaseModel):
    id: str
    endpoints: list[_Endpoint]


class _EndpointsResponse(BaseModel):
    data: _EndpointsData


class OpenRouterEndpointsConnector(Connector[ModelEndpoint]):
    """OpenRouter's per-model endpoints route: every provider serving one model.

    ``GET /api/v1/models/{author}/{slug}/endpoints`` needs no key. Without one, the
    latency and throughput fields come back null for every endpoint, so the Hugging Face
    router supplies those measurements instead. A negative
    ``status`` marks a degraded endpoint.
    """

    name = "openrouter_endpoints"
    fixture = "openrouter_endpoints.json"

    def __init__(self, model_id: str) -> None:
        """Bind the connector to one model.

        Args:
            model_id: OpenRouter's model identifier, such as ``openai/gpt-oss-120b``.
        """
        self.model_id = model_id

    def request(self, spec: ResourceSpec) -> httpx.Request:
        """Build the endpoints request for the bound model.

        Args:
            spec: Unused.

        Returns:
            The GET request.
        """
        return httpx.Request("GET", ENDPOINTS_URL.format(model_id=self.model_id))

    def cache_key(self, spec: ResourceSpec) -> str:
        """Key the snapshot by model.

        Args:
            spec: Unused.

        Returns:
            The connector name and model.
        """
        return f"{self.name}:{self.model_id}"

    def fixture_bytes(self) -> bytes:
        """Load the bound model's recorded response from the combined fixture.

        Returns:
            The model's recorded response, or an empty endpoint list if none was recorded.
        """
        recorded: dict[str, JsonValue] = json.loads(super().fixture_bytes())
        empty: JsonValue = {"data": {"id": self.model_id, "endpoints": []}}
        return json.dumps(recorded.get(self.model_id, empty)).encode()

    def parse(self, payload: JsonValue, meta: FetchMeta) -> list[ModelEndpoint]:
        """Normalise each priced endpoint, once per tag.

        Args:
            payload: The decoded response.
            meta: Provenance.

        Returns:
            One endpoint per distinct tag with a fixed, non-negative price.
        """
        data = _EndpointsResponse.model_validate(payload).data
        endpoints: list[ModelEndpoint] = []
        seen: set[str] = set()
        for index, endpoint in enumerate(data.endpoints):
            prompt = per_million(endpoint.pricing.prompt)
            completion = per_million(endpoint.pricing.completion)
            if prompt < 0 or completion < 0 or endpoint.tag in seen:
                continue
            seen.add(endpoint.tag)
            degraded = endpoint.status is not None and endpoint.status < 0
            endpoints.append(
                ModelEndpoint(
                    venue="openrouter",
                    model_id=data.id,
                    serving_provider=endpoint.provider_name,
                    tag=endpoint.tag,
                    prompt_usd_per_mtok=prompt,
                    completion_usd_per_mtok=completion,
                    context_length=endpoint.context_length,
                    first_token_latency_ms=decimal_or_none(endpoint.latency_last_30m),
                    throughput_tps=decimal_or_none(endpoint.throughput_last_30m),
                    availability=Availability.LOW if degraded else Availability.AVAILABLE,
                    source=meta.source,
                    fetched_at=meta.fetched_at,
                    raw_ref=meta.ref(index),
                )
            )
        return endpoints

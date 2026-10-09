"""LiteLLM's model price file: the fallback for OpenRouter's model list.

LiteLLM publishes ``model_prices_and_context_window.json`` on GitHub (no key, about 3
MB). Its ``openrouter/<model>`` entries mirror OpenRouter's list prices as USD per token
floats, so when OpenRouter cannot be reached the same models can still be priced. The
file is a periodically refreshed copy, not OpenRouter's live answer, which is why the
model market stamps what it reads from here ``cached`` even when the file itself was
fetched live.
"""

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from youreapyousow.domain import ModelOffer, ResourceSpec
from youreapyousow.market.connectors.base import Connector, FetchMeta
from youreapyousow.market.connectors.model_endpoints import per_million

URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
OPENROUTER_PREFIX = "openrouter/"


class _Entry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    litellm_provider: str | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    max_input_tokens: int | None = None


class LiteLLMPricesConnector(Connector[ModelOffer]):
    """LiteLLM's price file, read for its OpenRouter entries."""

    name = "litellm"
    fixture = "litellm.json"

    def request(self, spec: ResourceSpec) -> httpx.Request:
        """Build the price-file request.

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
        """Normalise the priced OpenRouter entries to USD per million tokens.

        Args:
            payload: The decoded price file, an object keyed by model.
            meta: Provenance.

        Returns:
            One offer per OpenRouter model with both prices stated.

        Raises:
            ValueError: If the file is not an object.
        """
        if not isinstance(payload, dict):
            raise ValueError("the LiteLLM price file is not a JSON object")
        offers: list[ModelOffer] = []
        for key, value in payload.items():
            if not key.startswith(OPENROUTER_PREFIX):
                continue
            try:
                entry = _Entry.model_validate(value)
            except ValidationError:
                continue
            if (
                entry.litellm_provider != "openrouter"
                or entry.input_cost_per_token is None
                or entry.output_cost_per_token is None
            ):
                continue
            model_id = key.removeprefix(OPENROUTER_PREFIX)
            offers.append(
                ModelOffer(
                    provider="openrouter",
                    model_id=model_id,
                    name=model_id,
                    prompt_usd_per_mtok=per_million(entry.input_cost_per_token),
                    completion_usd_per_mtok=per_million(entry.output_cost_per_token),
                    context_length=entry.max_input_tokens,
                    source=meta.source,
                    fetched_at=meta.fetched_at,
                )
            )
        return offers

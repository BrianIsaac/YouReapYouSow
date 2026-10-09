"""The per-provider serving endpoint of a model, shared by the model-market connectors.

A ``ModelOffer`` is one price per model, as a model list states it. The same model is
usually served by several inference providers at different prices and speeds, and both
OpenRouter's endpoints route and the Hugging Face router list them. ``ModelEndpoint``
is that finer grain: one row per model, venue and serving provider, with measured
first-token latency and throughput where the venue publishes them.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from youreapyousow.domain import Availability, Source

PER_MILLION = Decimal(1_000_000)
MICRO_USD = Decimal("0.000001")


def per_million(value: str | float) -> Decimal:
    """Convert a price in USD per token to USD per million tokens.

    Args:
        value: The per-token price as the venue states it, string or float.

    Returns:
        USD per million tokens, to a millionth of a dollar.
    """
    return mtok_price(Decimal(str(value)) * PER_MILLION)


def mtok_price(value: Decimal | float) -> Decimal:
    """Round a price already in USD per million tokens to a millionth of a dollar.

    Floats arrive with representation noise (``0.060000000000000005``); rounding here
    keeps prices exact and comparable across venues.

    Args:
        value: USD per million tokens.

    Returns:
        The rounded price.
    """
    return Decimal(str(value)).quantize(MICRO_USD).normalize()


def decimal_or_none(value: float | None, places: str = "0.1") -> Decimal | None:
    """Convert an optional float measurement to a rounded decimal.

    Args:
        value: The measurement, if published.
        places: The quantum to round to.

    Returns:
        The decimal, or None.
    """
    return None if value is None else Decimal(str(value)).quantize(Decimal(places))


class ModelEndpoint(BaseModel):
    """One provider serving one model through one venue.

    Attributes:
        venue: Who is paid: ``openrouter`` or ``huggingface``.
        model_id: The venue's identifier for the model.
        serving_provider: The inference provider behind the venue, such as ``DeepInfra``.
        tag: The venue's identifier for this endpoint, unique per model.
        prompt_usd_per_mtok: Input price per million tokens.
        completion_usd_per_mtok: Output price per million tokens.
        context_length: Maximum context, when stated.
        first_token_latency_ms: Measured time to first token, when published.
        throughput_tps: Measured output tokens per second, when published.
        availability: Normalised health of the endpoint.
        source: Live, cached or mock.
        fetched_at: When the underlying response was fetched.
        raw_ref: ``sha256:<digest>#<index>`` of the raw response, for provenance.
    """

    model_config = ConfigDict(frozen=True)

    venue: str
    model_id: str
    serving_provider: str
    tag: str
    prompt_usd_per_mtok: Decimal
    completion_usd_per_mtok: Decimal
    context_length: int | None
    first_token_latency_ms: Decimal | None = None
    throughput_tps: Decimal | None = None
    availability: Availability = Availability.AVAILABLE
    source: Source
    fetched_at: datetime
    raw_ref: str

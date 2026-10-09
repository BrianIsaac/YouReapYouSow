"""Tests for the price conversions shared by the model-market connectors."""

from decimal import Decimal

from youreapyousow.market.connectors.model_endpoints import decimal_or_none, mtok_price, per_million


def test_per_token_strings_and_floats_become_exact_per_million_prices() -> None:
    """OpenRouter's strings and LiteLLM's floats land on the same exact decimal."""
    assert per_million("0.00000025") == Decimal("0.25")
    assert per_million(2.5e-07) == Decimal("0.25")
    assert per_million("0.0000000675") == Decimal("0.0675")
    assert per_million("-1") < 0


def test_float_noise_is_rounded_away() -> None:
    """Hugging Face's binary-float noise does not survive into a price."""
    assert mtok_price(0.060000000000000005) == Decimal("0.06")
    assert mtok_price(2.5999999999999996) == Decimal("2.6")


def test_optional_measurements_round_or_stay_absent() -> None:
    """A published measurement is rounded; an absent one stays None."""
    assert decimal_or_none(613.84) == Decimal("613.8")
    assert decimal_or_none(None) is None

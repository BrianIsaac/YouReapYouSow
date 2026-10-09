"""Tests for mock provisioning and its honest notices."""

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import START
from tests.factories import make_offer
from youreapyousow.domain import BillingGranularity, DeploymentStatus
from youreapyousow.market.provisioning import GENERIC_OUTCOME, MockProvisioner, metered_usd

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("provider", "phrase"),
    [("vast", "Add Credit"), ("runpod", "24-hour"), ("shadeform", "wallet top-up")],
)
async def test_every_provider_states_the_real_world_outcome(provider: str, phrase: str) -> None:
    """Each mock deployment says what a sandbox card would meet at that provider."""
    deployment = await MockProvisioner().provision(
        make_offer(provider=provider), objective_id="o", intent_id="i", now=START
    )
    assert deployment.mode == "mock"
    assert phrase in deployment.simulation_notice
    assert deployment.status == DeploymentStatus.RUNNING


async def test_unknown_provider_gets_the_generic_notice() -> None:
    """A provider without a researched outcome still gets an honest label."""
    deployment = await MockProvisioner().provision(
        make_offer(provider="lambda"), objective_id="o", intent_id="i", now=START
    )
    assert deployment.simulation_notice == GENERIC_OUTCOME


async def test_release_stops_and_refuses_protected_or_repeat() -> None:
    """Release records why and when; protected or released capacity is refused."""
    provisioner = MockProvisioner()
    deployment = await provisioner.provision(
        make_offer(), objective_id="o", intent_id="i", now=START
    )
    released = await provisioner.release(deployment, reason="demand.normalised", now=START)
    assert (released.status, released.release_reason) == (
        DeploymentStatus.RELEASED,
        "demand.normalised",
    )
    with pytest.raises(ValueError, match="already released"):
        await provisioner.release(released, reason="again", now=START)
    with pytest.raises(ValueError, match="protected"):
        await provisioner.release(
            deployment.model_copy(update={"protected": True}), reason="x", now=START
        )


@pytest.mark.parametrize(
    ("granularity", "seconds", "expected"),
    [
        (BillingGranularity.PER_SECOND, 2160, "0.44"),
        (BillingGranularity.PER_SECOND, 0, "0.00"),
        (BillingGranularity.PER_MINUTE, 61, "0.02"),
        (BillingGranularity.PER_HOUR, 2160, "0.73"),
        (BillingGranularity.PER_HOUR, 3601, "1.46"),
    ],
)
def test_metered_cost_follows_provider_granularity(
    granularity: BillingGranularity, seconds: int, expected: str
) -> None:
    """36 minutes at $0.73/h costs $0.44 per second, a whole hour per hour."""
    offer = make_offer(price="0.73").model_copy(update={"billing_granularity": granularity})
    assert metered_usd(offer, START, START + timedelta(seconds=seconds)) == Decimal(expected)

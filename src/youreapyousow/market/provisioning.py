"""Provisioning and release: mocked for every provider, and honest about why.

No provider takes a card at rent time. Each one charges a Stripe top-up into a prepaid
balance, so a Reap sandbox card (off-network by design) would fail at the top-up page,
before any rent call. Every mock deployment carries that failure in its
``simulation_notice``, so the dashboard and the audit report never present a simulated
machine as a real one.
"""

import math
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from youreapyousow.domain import BillingGranularity, Deployment, DeploymentStatus, Offer, usd
from youreapyousow.ids import new_id

SANDBOX_CARD_OUTCOMES: dict[str, str] = {
    "vast": (
        "Mock provisioning. At a real Vast.ai account a Reap sandbox card is declined at "
        "the Stripe Add Credit checkout ('Your card was declined'); no credit lands, and "
        "PUT /api/v0/asks/{id}/ then fails for lack of balance."
    ),
    "runpod": (
        "Mock provisioning. At a real RunPod account a Reap sandbox card is declined at "
        "the Stripe top-up, and a repeat attempt risks a 24-hour automated payment block; "
        "pod deployment needs at least one hour of credit first."
    ),
    "shadeform": (
        "Mock provisioning. Shadeform bills a prepaid wallet; a Reap sandbox card is "
        "declined at the wallet top-up, so POST /v1/instances/create would fail for lack "
        "of balance."
    ),
}
GENERIC_OUTCOME = (
    "Mock provisioning. The provider bills a prepaid balance topped up by card; a Reap "
    "sandbox card cannot complete that top-up, so no real machine exists."
)


class Provisioner(Protocol):
    """Acquire and release capacity at a provider."""

    async def provision(
        self, offer: Offer, *, objective_id: str, intent_id: str, now: datetime
    ) -> Deployment:
        """Start capacity for a purchased offer."""
        ...

    async def release(self, deployment: Deployment, *, reason: str, now: datetime) -> Deployment:
        """Stop capacity so it no longer costs money."""
        ...


class MockProvisioner:
    """Deterministic provisioning for every provider, labelled as simulated."""

    async def provision(
        self, offer: Offer, *, objective_id: str, intent_id: str, now: datetime
    ) -> Deployment:
        """Simulate starting an instance from an offer.

        Args:
            offer: The purchased offer.
            objective_id: The objective it serves.
            intent_id: The purchase that paid for it.
            now: The start time.

        Returns:
            A running deployment carrying the provider's real-world outcome.
        """
        return Deployment(
            id=new_id("dep"),
            objective_id=objective_id,
            intent_id=intent_id,
            provider=offer.provider,
            offer=offer,
            started_at=now,
            mode="mock",
            simulation_notice=SANDBOX_CARD_OUTCOMES.get(offer.provider, GENERIC_OUTCOME),
        )

    async def release(self, deployment: Deployment, *, reason: str, now: datetime) -> Deployment:
        """Simulate terminating an instance.

        Args:
            deployment: The running deployment.
            reason: Why it is released.
            now: The release time.

        Returns:
            The released deployment.

        Raises:
            ValueError: If the deployment is protected or already released.
        """
        if deployment.protected:
            raise ValueError("protected baseline capacity cannot be released")
        if deployment.status != DeploymentStatus.RUNNING:
            raise ValueError(f"deployment {deployment.id} is already released")
        return deployment.model_copy(
            update={
                "status": DeploymentStatus.RELEASED,
                "released_at": now,
                "release_reason": reason,
            }
        )


def metered_usd(offer: Offer, started_at: datetime, ended_at: datetime) -> Decimal:
    """Price a lease by the provider's metering granularity, in whole cents.

    Args:
        offer: The offer leased.
        started_at: Lease start.
        ended_at: Lease end.

    Returns:
        Hourly price times metered time: per-second exact, per-minute and per-hour
        rounded up to the next whole unit, then rounded half up to cents.
    """
    seconds = Decimal(max(0, math.ceil((ended_at - started_at).total_seconds())))
    match offer.billing_granularity:
        case BillingGranularity.PER_SECOND:
            hours = seconds / 3600
        case BillingGranularity.PER_MINUTE:
            hours = Decimal(math.ceil(seconds / 60)) / 60
        case BillingGranularity.PER_HOUR:
            hours = Decimal(math.ceil(seconds / 3600))
    return usd(offer.price_usd_per_hour * hours)

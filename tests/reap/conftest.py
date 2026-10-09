"""Fixtures for Reap tests: a mock backend with a KYC-approved, funded cardholder."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

import pytest

from youreapyousow.clock import ManualClock
from youreapyousow.reap.client import ReapMock
from youreapyousow.reap.models import (
    Card,
    CreateAccountRequest,
    CreateCardRequest,
    CreateUserRequest,
    SimulateFiatDepositRequest,
)


@dataclass
class Funded:
    """A ready-to-spend card on the mock.

    Attributes:
        reap: The mock client.
        card: The card.
    """

    reap: ReapMock
    card: Card


async def issue_card(reap: ReapMock, *, deposit: str = "100", suffix: str = "1") -> Card:
    """Create an approved user, an account, a deposit and a virtual card.

    Returns:
        The card.
    """
    user = await reap.create_user(
        CreateUserRequest(
            email=f"operator{suffix}@example.com", phone_number="+6500000000", first_name="Op"
        )
    )
    await reap.simulate_user_application(user.id, "APPROVED")
    account = await reap.create_account(
        CreateAccountRequest(owner_id=user.id, owner_type="USER"), idempotency_key=f"acct-{suffix}"
    )
    await reap.simulate_fiat_deposit(
        SimulateFiatDepositRequest(amount=Decimal(deposit), currency="USD")
    )
    return await reap.create_card(
        CreateCardRequest(user_id=user.id, account_id=account.id, type="VIRTUAL"),
        idempotency_key=f"card-{suffix}",
    )


@pytest.fixture
async def reap(clock: ManualClock) -> AsyncIterator[ReapMock]:
    """A mock Reap backend on the manual clock.

    Yields:
        The client.
    """
    client = ReapMock(clock=clock)
    yield client
    await client.aclose()


@pytest.fixture
async def funded(reap: ReapMock) -> Funded:
    """A funded card on the mock.

    Returns:
        The client and card.
    """
    return Funded(reap, await issue_card(reap))

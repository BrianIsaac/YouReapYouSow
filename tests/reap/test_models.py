"""Tests for the Reap wire models' exact shapes."""

from decimal import Decimal

from pydantic import TypeAdapter

from youreapyousow.reap.models import (
    Card,
    CardTransaction,
    CreateCardRequest,
    DeclinedCardTransaction,
    PendingCardTransaction,
    Policy,
    SimulateAuthorizationRequest,
    SimulatedMerchant,
    SpendLimitPolicy,
)

CARD_JSON = {
    "id": "c1",
    "accountId": "a1",
    "userId": "user-1",
    "type": "VIRTUAL",
    "cardDesignId": "default",
    "status": "ACTIVE",
    "blockReason": None,
    "blockLiftable": False,
    "frozen": False,
    "cardholderName": "Op",
    "last4": "1234",
    "3dsChallengeMethod": "SMS",
    "physicalCardStatus": None,
    "createdAt": "2037-10-09T08:00:00Z",
    "updatedAt": "2037-10-09T08:00:00Z",
}


def test_card_round_trips_with_required_nulls_and_the_3ds_alias() -> None:
    """Required nullable fields stay present as null; the digit-led alias survives."""
    card = Card.model_validate(CARD_JSON)
    assert card.three_ds_challenge_method == "SMS"
    assert card.to_wire() == CARD_JSON


def test_requests_omit_unset_optionals() -> None:
    """Optional request fields are left out rather than sent as null."""
    req = CreateCardRequest(user_id="u", account_id="a", type="VIRTUAL")
    assert req.to_wire() == {"userId": "u", "accountId": "a", "type": "VIRTUAL"}
    auth = SimulateAuthorizationRequest(
        card_id="c", amount=Decimal("1.46"), merchant=SimulatedMerchant(name="Vast.ai")
    )
    assert auth.to_wire() == {"cardId": "c", "amount": 1.46, "merchant": {"name": "Vast.ai"}}


def test_card_transaction_union_discriminates_on_status() -> None:
    """Each status parses into its own variant with its own amount shape."""
    base: dict[str, object] = {
        "id": "t",
        "accountId": "a",
        "cardId": "c",
        "channel": "ECOMMERCE",
        "digitalWallet": None,
        "originalCurrency": "USD",
        "currency": "USD",
        "merchant": {
            "id": "m",
            "cardAcceptorId": None,
            "name": "RunPod",
            "city": None,
            "postCode": None,
            "state": None,
            "mccCode": "7372",
            "mccCategory": "Computer",
        },
        "fees": {"atm": 0, "fx": 0},
        "events": [],
        "occurredAt": "x",
        "createdAt": "x",
        "updatedAt": "x",
    }
    adapter = TypeAdapter[CardTransaction](CardTransaction)
    pending = adapter.validate_python(
        base
        | {
            "status": "PENDING",
            "originalAmount": {"authorized": 1.46, "reversed": 0, "current": 1.46},
            "amount": {"authorized": 1.46, "reversed": 0, "current": 1.46},
            "conversionRate": 1,
        }
    )
    assert isinstance(pending, PendingCardTransaction)
    assert pending.amount.current == Decimal("1.46")
    declined = adapter.validate_python(
        base
        | {
            "status": "DECLINED",
            "originalAmount": 9.99,
            "declineReason": {"code": "SPEND_LIMIT_EXCEEDED", "message": "over"},
            "policy": {"id": "p", "type": "SPEND_LIMIT", "scopeType": "CARD", "name": "daily"},
        }
    )
    assert isinstance(declined, DeclinedCardTransaction)
    assert declined.policy is not None
    assert declined.policy.name == "daily"


def test_policy_union_parses_stored_spend_limit() -> None:
    """A stored policy parses into its typed variant."""
    policy = TypeAdapter[Policy](Policy).validate_python(
        {
            "id": "p",
            "scope": {"type": "CARD", "id": "c"},
            "status": "ACTIVE",
            "name": "n",
            "createdAt": "x",
            "updatedAt": "x",
            "type": "SPEND_LIMIT",
            "currency": "USD",
            "config": {"window": {"type": "CALENDAR", "period": "DAILY"}, "maxAmount": 20},
        }
    )
    assert isinstance(policy, SpendLimitPolicy)
    assert policy.config.max_amount == Decimal(20)

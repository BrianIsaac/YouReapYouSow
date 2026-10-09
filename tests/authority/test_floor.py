"""Tests for mirroring a grant to Reap policies."""

from decimal import Decimal

from tests.factories import make_grant
from youreapyousow.authority.floor import DENIED_MCCS, policies_for
from youreapyousow.reap.models import (
    MccMatch,
    MerchantRestrictionCreate,
    ScopeType,
    SpendLimitCreate,
    TransactionAmountLimitCreate,
)


def test_floor_mirrors_budget_daily_cap_per_tx_cap_and_mcc_denials() -> None:
    """Four card-scoped policies carry the grant's limits exactly."""
    lifetime, daily, per_tx, mcc = policies_for(
        make_grant(per_tx="5", daily="20"), budget_usd=Decimal(25), card_id="card_9"
    )

    assert isinstance(lifetime, SpendLimitCreate)
    assert lifetime.config.window.period == "LIFETIME"
    assert lifetime.config.max_amount == Decimal(25)
    assert isinstance(daily, SpendLimitCreate)
    assert daily.config.window.period == "DAILY"
    assert daily.config.max_amount == Decimal(20)
    assert isinstance(per_tx, TransactionAmountLimitCreate)
    assert per_tx.config.max_amount == Decimal(5)
    assert isinstance(mcc, MerchantRestrictionCreate)
    assert isinstance(mcc.config.match, MccMatch)
    assert mcc.config.match.codes == list(DENIED_MCCS)
    for policy in (lifetime, daily, per_tx, mcc):
        assert policy.scope.type == ScopeType.CARD
        assert policy.scope.id == "card_9"
        assert policy.name.startswith("objective obj_1: ")


def test_floor_serialises_in_reap_shape() -> None:
    """The requests serialise to the documented camelCase JSON."""
    lifetime = policies_for(make_grant(), budget_usd=Decimal(25), card_id="c")[0]
    assert lifetime.to_wire() == {
        "scope": {"type": "CARD", "id": "c"},
        "name": "objective obj_1: lifetime budget",
        "type": "SPEND_LIMIT",
        "config": {"window": {"type": "CALENDAR", "period": "LIFETIME"}, "maxAmount": 25.0},
    }

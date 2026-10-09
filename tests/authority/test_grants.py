"""Tests for grant validation and liveness."""

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import START
from tests.factories import make_grant, make_part_grant
from youreapyousow.authority.grants import AuthorityGrant, GrantError, validate_grant


def test_valid_grant_passes() -> None:
    """Consistent limits validate."""
    validate_grant(make_grant(), Decimal(25))


@pytest.mark.parametrize(
    ("grant", "budget", "message"),
    [
        (make_grant(providers=()), "25", "at least one provider"),
        (make_grant().model_copy(update={"allowed_kinds": []}), "25", "resource kind"),
        (make_grant(), "0", "budget must be positive"),
        (make_grant(per_tx="0"), "25", "caps must be positive"),
        (make_grant(per_tx="21", daily="20"), "25", "exceeds the daily cap"),
        (make_grant(daily="30"), "25", "exceeds the objective budget"),
        (make_grant(max_hourly="0"), "25", "ceiling must be positive"),
        (make_grant(ttl=timedelta(0)), "25", "expire after"),
        (make_grant(providers=(), merchants=()), "25", "at least one provider or merchant"),
        (make_grant(attempts=0), "25", "at least one attempt per need"),
        (make_grant(margin_s=-1), "25", "margin cannot be negative"),
    ],
)
def test_inconsistent_grants_are_refused(grant: AuthorityGrant, budget: str, message: str) -> None:
    """Each inconsistency is caught with a precise message."""
    with pytest.raises(GrantError, match=message):
        validate_grant(grant, Decimal(budget))


def test_is_live_honours_issue_expiry_and_revocation() -> None:
    """A grant is live only between issue and expiry, and never once revoked."""
    grant = make_grant(ttl=timedelta(hours=1))
    assert not grant.is_live(START - timedelta(seconds=1))
    assert grant.is_live(START)
    assert not grant.is_live(START + timedelta(hours=1))
    assert not grant.model_copy(update={"revoked_at": START}).is_live(START)


def test_a_catalogue_grant_needs_merchants_not_providers() -> None:
    """A grant scoped to Reap merchants alone is valid on the agentic path."""
    grant = make_part_grant()
    validate_grant(grant, Decimal(500))
    assert grant.allowed_providers == []
    assert (grant.attempts_per_need, grant.quote_margin_s) == (3, 15)


def test_a_grant_defaults_to_three_attempts_and_a_fifteen_second_margin() -> None:
    """The agentic defaults apply when a grant does not say."""
    grant = AuthorityGrant.model_validate(
        make_grant().model_dump(exclude={"attempts_per_need", "quote_margin_s"})
    )
    assert (grant.attempts_per_need, grant.quote_margin_s, grant.allowed_merchants) == (3, 15, [])

"""Delegated authority: what an objective's agent may spend, where, and until when."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from youreapyousow.domain import ResourceKind


class GrantError(ValueError):
    """Raised when a grant's limits are inconsistent with each other or the budget."""


class AuthorityGrant(BaseModel):
    """The operator's grant of spending authority for one objective.

    The in-house rules enforce every field; on the dormant card path the per-transaction
    cap, the daily cap and the objective budget are also mirrored to Reap policies as a
    floor beneath them. On the agentic path the caps apply to a quote's landed
    ``finalAmount``.

    Attributes:
        id: Identifier.
        objective_id: The objective the authority is for.
        allowed_providers: Providers the agent may lease from on the card path (Reap
            cannot express this).
        allowed_kinds: Resource kinds the agent may lease on the card path.
        per_transaction_cap_usd: Largest single purchase.
        daily_cap_usd: Largest total per UTC calendar day, matching Reap's window.
        max_price_usd_per_hour: Highest hourly rate the agent may accept, if capped.
        approval_threshold_usd: Purchases above this wait for the operator, if set.
        issued_at: When the grant starts.
        expires_at: When the grant lapses (its TTL); Reap cards have no expiry field.
        revoked_at: When the operator revoked it, if they did.
        allowed_merchants: The merchant scope on the agentic path: the Reap merchant
            names a catalogue quote may come from.
        attempts_per_need: How many attempts at one need may end failed, expired or
            declined before the need is refused.
        quote_margin_s: How long before Reap's ``expiresAt`` a catalogue quote stops
            being bought, so the checkout is not sent on a quote about to lapse.
    """

    id: str
    objective_id: str
    allowed_providers: list[str]
    allowed_kinds: list[ResourceKind] = Field(default_factory=lambda: [ResourceKind.GPU_COMPUTE])
    per_transaction_cap_usd: Decimal
    daily_cap_usd: Decimal
    max_price_usd_per_hour: Decimal | None = None
    approval_threshold_usd: Decimal | None = None
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    allowed_merchants: list[str] = Field(default_factory=list[str])
    attempts_per_need: int = 3
    quote_margin_s: int = 15

    def is_live(self, now: datetime) -> bool:
        """Report whether the grant confers authority at ``now``.

        Args:
            now: The time of the check.

        Returns:
            True when issued, unexpired and unrevoked.
        """
        return self.revoked_at is None and self.issued_at <= now < self.expires_at


def validate_grant(grant: AuthorityGrant, budget_usd: Decimal) -> None:
    """Refuse a grant whose limits cannot all hold at once.

    Args:
        grant: The grant to check.
        budget_usd: The objective's total budget.

    Raises:
        GrantError: Describing the first inconsistency found.
    """
    if not grant.allowed_providers and not grant.allowed_merchants:
        raise GrantError("a grant must allow at least one provider or merchant")
    if not grant.allowed_kinds:
        raise GrantError("a grant must allow at least one resource kind")
    if budget_usd <= 0:
        raise GrantError("the objective budget must be positive")
    if grant.per_transaction_cap_usd <= 0 or grant.daily_cap_usd <= 0:
        raise GrantError("caps must be positive")
    if grant.per_transaction_cap_usd > grant.daily_cap_usd:
        raise GrantError("the per-transaction cap exceeds the daily cap")
    if grant.daily_cap_usd > budget_usd:
        raise GrantError("the daily cap exceeds the objective budget")
    if grant.max_price_usd_per_hour is not None and grant.max_price_usd_per_hour <= 0:
        raise GrantError("the hourly price ceiling must be positive")
    if grant.expires_at <= grant.issued_at:
        raise GrantError("a grant must expire after it is issued")
    if grant.attempts_per_need < 1:
        raise GrantError("a grant must allow at least one attempt per need")
    if grant.quote_margin_s < 0:
        raise GrantError("the quote safety margin cannot be negative")

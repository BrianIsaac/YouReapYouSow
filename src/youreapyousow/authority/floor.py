"""Mirror a grant to Reap spend policies: the floor beneath the in-house engine.

If the in-house gate were bypassed or wrong, Reap still declines at authorisation and
names the policy that fired. The floor can only express what Reap can: a lifetime
budget, a per-transaction cap, a daily cap and an MCC deny list. The provider allow
list, the TTL and the hourly price ceiling stay in-house.
"""

from decimal import Decimal

from youreapyousow.authority.grants import AuthorityGrant
from youreapyousow.reap.models import (
    CalendarWindow,
    MccMatch,
    MerchantRestrictionConfig,
    MerchantRestrictionCreate,
    PolicyCreate,
    PolicyScope,
    ScopeType,
    SpendLimitConfig,
    SpendLimitCreate,
    TransactionAmountLimitConfig,
    TransactionAmountLimitCreate,
)

DENIED_MCCS: tuple[str, ...] = (
    "4829",  # wire transfers and money orders
    "6010",  # manual cash disbursements
    "6011",  # automated cash disbursements
    "6012",  # financial institution merchandise and services
    "6051",  # quasi-cash, including crypto purchases
    "6211",  # securities brokers
    "7800",  # government lotteries
    "7801",  # government-licensed online casinos
    "7802",  # government-licensed horse and dog racing
    "7995",  # betting and casino gambling
)


def policies_for(
    grant: AuthorityGrant,
    *,
    budget_usd: Decimal,
    card_id: str,
    denied_mccs: tuple[str, ...] = DENIED_MCCS,
) -> list[PolicyCreate]:
    """Build the card-scoped Reap policies that mirror a grant.

    Policies are named after the objective so a Reap decline traces back to it.

    Args:
        grant: The in-house grant.
        budget_usd: The objective's total budget, mirrored as the lifetime limit.
        card_id: The objective's Reap card.
        denied_mccs: Merchant categories no compute purchase should ever hit.

    Returns:
        Create requests for the lifetime limit, the daily limit, the per-transaction
        cap and the MCC deny list, in that order.
    """
    scope = PolicyScope(type=ScopeType.CARD, id=card_id)
    prefix = f"objective {grant.objective_id}"
    return [
        SpendLimitCreate(
            scope=scope,
            name=f"{prefix}: lifetime budget",
            config=SpendLimitConfig(
                window=CalendarWindow(period="LIFETIME"), max_amount=budget_usd
            ),
        ),
        SpendLimitCreate(
            scope=scope,
            name=f"{prefix}: daily cap",
            config=SpendLimitConfig(
                window=CalendarWindow(period="DAILY"), max_amount=grant.daily_cap_usd
            ),
        ),
        TransactionAmountLimitCreate(
            scope=scope,
            name=f"{prefix}: per-transaction cap",
            config=TransactionAmountLimitConfig(max_amount=grant.per_transaction_cap_usd),
        ),
        MerchantRestrictionCreate(
            scope=scope,
            name=f"{prefix}: denied categories",
            config=MerchantRestrictionConfig(
                match=MccMatch(dimension="MCC", codes=list(denied_mccs))
            ),
        ),
    ]

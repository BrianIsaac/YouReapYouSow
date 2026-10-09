"""Ranking landed quotes: the order a strategy reads them in.

A quote is eligible when its item fills the need (the same judgement as the gate's rule
10), its merchant is in the grant's merchant scope (rule 9) and its landed
``finalAmount`` is in the budget's currency (rule 11). Eligible quotes come first,
cheapest landed price first; the ineligible follow in the order given, each with the
reason, so a dashboard can show why a cheaper result was passed over. The gate still
decides every proposal: ranking is advice, never authority, and the caps, the runway and
the approval threshold are left to the gate so that a refusal is seen there.

A choice made here: quotes rank by lowest ``finalAmount``, and Reap's quotes carry no
delivery estimate to break a tie (shipping options have only free-form ``details``), so
equal landed prices are ordered by merchant name, then variant id, which keeps a rerun's
choice the same.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from youreapyousow.domain import BUDGET_CURRENCY, LandedQuote
from youreapyousow.procure.need import Need, judge_quote


@dataclass(frozen=True)
class RankedQuote:
    """One landed quote with its standing.

    Attributes:
        quote: The landed quote.
        eligible: Whether it may be proposed.
        reason: Why it is eligible, or the first reason it is not.
    """

    quote: LandedQuote
    eligible: bool
    reason: str


def _merchant_name(name: str) -> str:
    """Normalise a merchant name the way the gate's rule 9 does.

    Args:
        name: As Reap or the grant names it.

    Returns:
        Case-folded, with runs of white space collapsed.
    """
    return re.sub(r"\s+", " ", name).strip().casefold()


def in_scope(merchant: str, merchants: Iterable[str]) -> bool:
    """Whether a merchant is in the grant's merchant scope, matched as rule 9 matches.

    Args:
        merchant: The merchant as Reap names it.
        merchants: The scope.

    Returns:
        True when one of the scope's names is the same merchant.
    """
    return _merchant_name(merchant) in {_merchant_name(m) for m in merchants}


def _standing(quote: LandedQuote, need: Need, scope: frozenset[str]) -> RankedQuote:
    fills, detail = judge_quote(need, quote)
    if not fills:
        return RankedQuote(quote, eligible=False, reason=detail)
    if _merchant_name(quote.merchant) not in scope:
        return RankedQuote(
            quote, eligible=False, reason=f"{quote.merchant} is not in the merchant scope"
        )
    currency = quote.final_amount.currency
    if currency != BUDGET_CURRENCY:
        return RankedQuote(
            quote, eligible=False, reason=f"landed in {currency}, the budget is {BUDGET_CURRENCY}"
        )
    return RankedQuote(quote, eligible=True, reason=detail)


def rank_quotes(
    quotes: Iterable[LandedQuote], *, need: Need, merchants: Sequence[str]
) -> list[RankedQuote]:
    """Rank landed quotes for a need.

    Args:
        quotes: The landed quotes gathered for the need.
        need: The need they would fill.
        merchants: The grant's merchant scope.

    Returns:
        Eligible quotes by landed price, then merchant and variant; then the ineligible,
        in the order given.
    """
    scope = frozenset(_merchant_name(m) for m in merchants)
    standings = [_standing(q, need, scope) for q in quotes]
    eligible = sorted(
        (s for s in standings if s.eligible),
        key=lambda s: (
            s.quote.final_amount.amount,
            _merchant_name(s.quote.merchant),
            s.quote.variant.id,
        ),
    )
    return eligible + [s for s in standings if not s.eligible]


def best_quote(
    quotes: Iterable[LandedQuote], *, need: Need, merchants: Sequence[str]
) -> LandedQuote | None:
    """Return the quote a deterministic choice would propose.

    Args:
        quotes: The landed quotes gathered for the need.
        need: The need they would fill.
        merchants: The grant's merchant scope.

    Returns:
        The cheapest eligible quote, or None.
    """
    ranked = rank_quotes(quotes, need=need, merchants=merchants)
    return ranked[0].quote if ranked and ranked[0].eligible else None

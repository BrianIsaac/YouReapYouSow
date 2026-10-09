"""The ordered, pure rules that decide whether a purchase may proceed.

No model call, no I/O and no clock read happen here: everything a rule needs is in the
``GateContext``, including ``now``. The same ``evaluate`` runs when an intent is
proposed and again immediately before money moves, so what the agent was told and what
the gate enforces cannot drift apart.

Rules run in a fixed order and the first that fails decides. Every rule fails closed:
an exception inside one refuses the purchase as ``internal_error``.

There are two ordered sets. ``AGENTIC_RULES`` are seventeen rules deciding on a Reap
catalogue quote's landed ``finalAmount``. ``CARD_RULES`` are the fourteen the dormant
card path runs, with the agentic rules 6 and 9 to 11 replaced by ``provider.allowed``,
``resource_kind.allowed`` and ``rate.max_hourly``. The rules the two share read the
``PurchaseTerms`` view that both a lease and a landed quote provide, so the money limits
are one implementation.

Choices made here:

* ``quote.not_expired`` applies the grant's safety margin to catalogue quotes only; a
  lease quote is the gate's own and keeps its exact expiry.
* ``attempts.retry_limit`` counts the need's intents that ended ``failed`` (which covers
  a checkout answered ``CHECKOUT_TEMPORARILY_UNAVAILABLE``), ``expired`` or ``declined``.
  A quote answered ``QUOTE_TEMPORARILY_UNAVAILABLE`` never becomes an intent, so the
  gate cannot count it; the client bounds those retries itself (``RetryPolicy``).
* ``need.not_already_ordered`` treats a claimed intent (``executing``, whose checkout
  is being created or is ``PROCESSING``) as an open order.
* Merchant names are matched case-insensitively with runs of white space collapsed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from youreapyousow.authority.grants import AuthorityGrant
from youreapyousow.domain import (
    BUDGET_CURRENCY,
    DecisionPhase,
    Disposition,
    IntentState,
    LandedQuote,
    Objective,
    ObjectiveStatus,
    PolicyDecision,
    PurchaseIntent,
    PurchaseTerms,
    Quote,
    RuleCheck,
)

ALL_RULES_PASSED = "all_rules_passed"
INTERNAL_ERROR = "internal_error"

type RuleResult = tuple[bool, str]

type NeedJudge = Callable[[LandedQuote], RuleResult]
"""Judges whether a landed quote's item fills its need (rule 10, ``item.matches_need``).

It must be pure, like every rule: shape (a) compares the item's value with the estimated
lease, shape (b) the variant's options with the bill of materials. Its detail is
recorded on the decision as written.
"""

ENDED_ATTEMPT_STATES = frozenset({IntentState.FAILED, IntentState.EXPIRED, IntentState.DECLINED})
"""Intent states that use up one of a need's attempts."""

OPEN_ORDER_STATES = frozenset(
    {IntentState.EXECUTING, IntentState.AWAITING_APPROVAL, IntentState.COMPLETED}
)
"""Intent states in which a need already has an order, open or completed."""


@dataclass(frozen=True)
class GateContext:
    """Everything the rules may look at, gathered before evaluation.

    Attributes:
        intent: The proposed purchase.
        quote: The quote it names, if found: a lease, or a landed catalogue quote.
        objective: Its objective, if found.
        grant: The objective's current grant, if any.
        committed_usd: Spend already committed for the objective, excluding this intent.
        committed_today_usd: Of that, the part claimed on ``now``'s UTC day.
        now: The time of evaluation.
        need_judge: Judges a landed quote against its need; None refuses rule 10.
        need_intents: The other intents at the same need, in any state.
    """

    intent: PurchaseIntent
    quote: Quote | LandedQuote | None
    objective: Objective | None
    grant: AuthorityGrant | None
    committed_usd: Decimal
    committed_today_usd: Decimal
    now: datetime
    need_judge: NeedJudge | None = None
    need_intents: tuple[PurchaseIntent, ...] = ()

    @property
    def runway_before_usd(self) -> Decimal:
        """Return the budget left before this purchase.

        Returns:
            Budget minus committed spend, or 0 without an objective.
        """
        if self.objective is None:
            return Decimal(0)
        return self.objective.budget_usd - self.committed_usd


class _MissingRecordError(LookupError):
    """A rule needed a record that an earlier rule should have proved present."""


def _terms(ctx: GateContext) -> PurchaseTerms:
    if ctx.quote is None:
        raise _MissingRecordError("quote")
    return ctx.quote.terms


def _lease(ctx: GateContext) -> Quote:
    if not isinstance(ctx.quote, Quote):
        raise _MissingRecordError("lease quote")
    return ctx.quote


def _landed(ctx: GateContext) -> LandedQuote:
    if not isinstance(ctx.quote, LandedQuote):
        raise _MissingRecordError("landed quote")
    return ctx.quote


def _grant(ctx: GateContext) -> AuthorityGrant:
    if ctx.grant is None:
        raise _MissingRecordError("grant")
    return ctx.grant


def _objective(ctx: GateContext) -> Objective:
    if ctx.objective is None:
        raise _MissingRecordError("objective")
    return ctx.objective


def _usd(value: Decimal) -> str:
    """Write an amount as the check's detail shows it: cents, or every digit below a cent.

    Args:
        value: USD.

    Returns:
        For example ``$5.00`` or ``$0.0712``.
    """
    if value == value.quantize(Decimal("0.01")):
        return f"${value:,.2f}"
    return f"${value.normalize():f}"


def _money(amount: Decimal, currency: str) -> str:
    """Write an amount in its currency, as the check's detail shows it.

    Args:
        amount: The amount.
        currency: Its currency code.

    Returns:
        For example ``$142.00`` in the budget's currency, else ``142.00 SGD``.
    """
    return _usd(amount) if currency == BUDGET_CURRENCY else f"{amount:f} {currency}"


def _merchant_name(name: str) -> str:
    """Normalise a merchant's name for matching against the grant's scope.

    Args:
        name: As written in the grant or sent by Reap.

    Returns:
        Case-folded, with runs of white space collapsed.
    """
    return " ".join(name.split()).casefold()


def _utc(at: datetime) -> str:
    """Write a moment as the check's detail shows it.

    Args:
        at: An aware timestamp.

    Returns:
        For example ``2030-01-01 17:02:01 UTC``.
    """
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _well_formed(ctx: GateContext) -> RuleResult:
    intent, quote = ctx.intent, ctx.quote
    if intent.amount_usd <= 0:
        return False, f"amount {_usd(intent.amount_usd)} is not positive"
    if quote is None:
        return False, f"quote {intent.quote_id} does not exist"
    if quote.objective_id != intent.objective_id:
        return False, f"quote {intent.quote_id} belongs to another objective"
    if isinstance(quote, LandedQuote):
        if intent.need_id is None:
            return False, f"quote {quote.id} is a catalogue quote but the intent names no need"
        if quote.need_id != intent.need_id:
            return False, f"quote {quote.id} is for need {quote.need_id}, not {intent.need_id}"
    elif intent.need_id is not None:
        return False, f"quote {quote.id} is a lease but the intent names need {intent.need_id}"
    return True, "intent names an existing quote for its objective"


def _objective_active(ctx: GateContext) -> RuleResult:
    if ctx.objective is None:
        return False, f"objective {ctx.intent.objective_id} does not exist"
    if ctx.objective.status != ObjectiveStatus.ACTIVE:
        return False, f"objective is {ctx.objective.status.value}"
    return True, "objective is active"


def _grant_present(ctx: GateContext) -> RuleResult:
    if ctx.grant is None:
        return False, "no authority has been granted for this objective"
    return True, f"grant {ctx.grant.id}"


def _grant_not_revoked(ctx: GateContext) -> RuleResult:
    grant = _grant(ctx)
    if grant.revoked_at is not None:
        return False, f"grant revoked at {_utc(grant.revoked_at)}"
    return True, "grant not revoked"


def _grant_not_expired(ctx: GateContext) -> RuleResult:
    grant = _grant(ctx)
    if ctx.now < grant.issued_at:
        return False, f"grant starts at {_utc(grant.issued_at)}"
    if ctx.now >= grant.expires_at:
        return False, f"grant expired at {_utc(grant.expires_at)}"
    return True, f"grant valid until {_utc(grant.expires_at)}"


def _enrolment_active(ctx: GateContext) -> RuleResult:
    objective = _objective(ctx)
    enrolment = objective.enrolment
    if enrolment is None:
        return False, f"objective {objective.id} has no agentic enrolment"
    if not enrolment.is_active:
        return False, f"enrolment {enrolment.id} is {enrolment.status.value}, not ACTIVE"
    card = f" ({enrolment.network} {enrolment.last4})" if enrolment.last4 else ""
    return True, f"enrolment {enrolment.id} is active{card}"


def _quote_not_expired(ctx: GateContext) -> RuleResult:
    expires_at = _terms(ctx).expires_at
    margin_s = _grant(ctx).quote_margin_s if isinstance(ctx.quote, LandedQuote) else 0
    if ctx.now >= expires_at:
        return False, f"quote expired at {_utc(expires_at)}"
    if not margin_s:
        return True, f"quote valid until {_utc(expires_at)}"
    if ctx.now >= expires_at - timedelta(seconds=margin_s):
        return False, (
            f"quote expires at {_utc(expires_at)}, inside the {margin_s} s safety margin"
        )
    return True, f"quote valid until {_utc(expires_at)}, outside the {margin_s} s safety margin"


def _quote_exact_match(ctx: GateContext) -> RuleResult:
    terms, intent = _terms(ctx), ctx.intent
    lease = isinstance(ctx.quote, Quote)
    mismatches = [
        f"{name} {asked!s} != quoted {quoted!s}"
        for name, asked, quoted in (
            ("provider" if lease else "merchant", intent.provider, terms.merchant),
            ("offer" if lease else "variant", intent.offer_id, terms.item),
            ("quantity", intent.quantity, terms.quantity),
            ("amount", intent.amount_usd, terms.amount),
            ("currency", intent.currency, terms.currency),
        )
        if asked != quoted
    ]
    if mismatches:
        return False, "; ".join(mismatches)
    price = _money(terms.amount, terms.currency)
    if lease:
        return True, f"{terms.merchant}:{terms.item} at {price} exactly as quoted"
    return (
        True,
        f"{terms.quantity} x {terms.item} from {terms.merchant} at {price} exactly as quoted",
    )


def _merchant_in_scope(ctx: GateContext) -> RuleResult:
    merchant, scope = _landed(ctx).merchant, _grant(ctx).allowed_merchants
    if _merchant_name(merchant) not in {_merchant_name(m) for m in scope}:
        return False, f"{merchant} is not in the merchant scope {sorted(scope)}"
    return True, f"{merchant} is in the merchant scope"


def _item_matches_need(ctx: GateContext) -> RuleResult:
    quote = _landed(ctx)
    if ctx.need_judge is None:
        return False, f"no judgement is configured for need {quote.need_id}"
    return ctx.need_judge(quote)


def _currency_matches_budget(ctx: GateContext) -> RuleResult:
    currency = _terms(ctx).currency
    if currency != BUDGET_CURRENCY:
        return False, f"quote is in {currency}, the budget is in {BUDGET_CURRENCY}"
    return True, f"{currency} is the budget's currency"


def _retry_limit(ctx: GateContext) -> RuleResult:
    need, limit = _landed(ctx).need_id, _grant(ctx).attempts_per_need
    for other in ctx.need_intents:
        if other.state == IntentState.OUTCOME_UNKNOWN:
            return False, f"attempt {other.id} at {need} has an unknown outcome; reconcile it first"
    ended = sum(1 for other in ctx.need_intents if other.state in ENDED_ATTEMPT_STATES)
    if ended >= limit:
        return False, f"{ended} of {limit} attempts at {need} have failed"
    return True, f"{ended} of {limit} attempts at {need} have failed"


def _need_not_already_ordered(ctx: GateContext) -> RuleResult:
    need = _landed(ctx).need_id
    for other in ctx.need_intents:
        if other.state in OPEN_ORDER_STATES:
            if other.order_id is not None:
                reference = f" (order {other.order_id})"
            elif other.checkout_id is not None:
                reference = f" (checkout {other.checkout_id})"
            else:
                reference = ""
            return False, f"{need} already has {other.id} {other.state.value}{reference}"
    return True, f"{need} has no open or completed order"


def _provider_allowed(ctx: GateContext) -> RuleResult:
    provider, allowed = _lease(ctx).offer.provider, _grant(ctx).allowed_providers
    if provider not in allowed:
        return False, f"{provider} is not in the allow list {sorted(allowed)}"
    return True, f"{provider} is allowed"


def _kind_allowed(ctx: GateContext) -> RuleResult:
    kind, allowed = _lease(ctx).offer.kind, _grant(ctx).allowed_kinds
    if kind not in allowed:
        return False, f"{kind.value} is not an allowed resource kind"
    return True, f"{kind.value} is allowed"


def _per_hour(value: Decimal, places: str = "0.0001") -> str:
    """Write an hourly price to four places, as the check's detail shows it.

    Args:
        value: USD per hour.
        places: The quantum to round to.

    Returns:
        For example ``$0.0711/h`` or ``$1/h``.
    """
    return f"${value.quantize(Decimal(places), rounding=ROUND_HALF_UP).normalize():f}/h"


def _max_hourly(ctx: GateContext) -> RuleResult:
    ceiling, rate = _grant(ctx).max_price_usd_per_hour, _lease(ctx).offer.price_usd_per_hour
    if ceiling is None:
        return True, "no hourly price ceiling"
    shown, limit = _per_hour(rate), _per_hour(ceiling)
    if rate > ceiling:
        if shown == limit:
            shown = f"${rate:f}/h"
        return False, f"rate {shown} exceeds ceiling {limit}"
    return True, f"rate {shown} within ceiling {limit}"


def _per_transaction_cap(ctx: GateContext) -> RuleResult:
    cap, amount = _grant(ctx).per_transaction_cap_usd, _terms(ctx).amount
    if amount > cap:
        return False, f"amount {_usd(amount)} exceeds per-transaction cap {_usd(cap)}"
    return True, f"amount {_usd(amount)} within per-transaction cap {_usd(cap)}"


def _daily_cap(ctx: GateContext) -> RuleResult:
    cap = _grant(ctx).daily_cap_usd
    total = ctx.committed_today_usd + _terms(ctx).amount
    if total > cap:
        return False, f"today's spend would be {_usd(total)}, over the daily cap {_usd(cap)}"
    return True, f"today's spend would be {_usd(total)} of {_usd(cap)}"


def _runway(ctx: GateContext) -> RuleResult:
    _objective(ctx)
    runway, amount = ctx.runway_before_usd, _terms(ctx).amount
    if amount > runway:
        return False, f"amount {_usd(amount)} exceeds remaining runway {_usd(runway)}"
    return True, f"runway {_usd(runway)} leaves {_usd(runway - amount)} after purchase"


def _approval_threshold(ctx: GateContext) -> RuleResult:
    threshold, amount = _grant(ctx).approval_threshold_usd, _terms(ctx).amount
    if threshold is None or amount <= threshold:
        return True, "within autonomous authority"
    if ctx.intent.approved_by is not None:
        return True, f"above {_usd(threshold)}, approved by {ctx.intent.approved_by}"
    return False, f"amount {_usd(amount)} above {_usd(threshold)} needs operator approval"


@dataclass(frozen=True)
class Rule:
    """One named rule and the disposition it imposes when it fails.

    Attributes:
        id: Stable identifier, recorded on every decision.
        check: The pure check.
        on_fail: ``REFUSE`` for hard limits, ``ESCALATE`` for approval gates.
    """

    id: str
    check: Callable[[GateContext], RuleResult]
    on_fail: Disposition = Disposition.REFUSE


_WELL_FORMED = Rule("intent.well_formed", _well_formed)
_OBJECTIVE_ACTIVE = Rule("objective.active", _objective_active)
_GRANT_PRESENT = Rule("grant.present", _grant_present)
_GRANT_NOT_REVOKED = Rule("grant.not_revoked", _grant_not_revoked)
_GRANT_NOT_EXPIRED = Rule("grant.not_expired", _grant_not_expired)
_QUOTE_NOT_EXPIRED = Rule("quote.not_expired", _quote_not_expired)
_QUOTE_EXACT_MATCH = Rule("quote.exact_match", _quote_exact_match)
_PER_TRANSACTION_CAP = Rule("amount.per_transaction_cap", _per_transaction_cap)
_DAILY_CAP = Rule("amount.daily_cap", _daily_cap)
_RUNWAY = Rule("budget.runway", _runway)
_APPROVAL_THRESHOLD = Rule("approval.threshold", _approval_threshold, on_fail=Disposition.ESCALATE)

AGENTIC_RULES: tuple[Rule, ...] = (
    _WELL_FORMED,
    _OBJECTIVE_ACTIVE,
    _GRANT_PRESENT,
    _GRANT_NOT_REVOKED,
    _GRANT_NOT_EXPIRED,
    Rule("enrolment.active", _enrolment_active),
    _QUOTE_NOT_EXPIRED,
    _QUOTE_EXACT_MATCH,
    Rule("merchant.in_scope", _merchant_in_scope),
    Rule("item.matches_need", _item_matches_need),
    Rule("currency.matches_budget", _currency_matches_budget),
    _PER_TRANSACTION_CAP,
    _DAILY_CAP,
    _RUNWAY,
    Rule("attempts.retry_limit", _retry_limit),
    Rule("need.not_already_ordered", _need_not_already_ordered),
    _APPROVAL_THRESHOLD,
)
"""The gate on a quote's landed price, in order."""

CARD_RULES: tuple[Rule, ...] = (
    _WELL_FORMED,
    _OBJECTIVE_ACTIVE,
    _GRANT_PRESENT,
    _GRANT_NOT_REVOKED,
    _GRANT_NOT_EXPIRED,
    _QUOTE_NOT_EXPIRED,
    _QUOTE_EXACT_MATCH,
    Rule("provider.allowed", _provider_allowed),
    Rule("resource_kind.allowed", _kind_allowed),
    Rule("rate.max_hourly", _max_hourly),
    _PER_TRANSACTION_CAP,
    _DAILY_CAP,
    _RUNWAY,
    _APPROVAL_THRESHOLD,
)
"""The dormant card path's rules for a compute lease."""


def rules_for(ctx: GateContext) -> tuple[Rule, ...]:
    """Choose the ordered rules for what is being bought.

    Args:
        ctx: The evaluation context.

    Returns:
        ``AGENTIC_RULES`` for a landed catalogue quote, or for an intent that names a
        need when its quote is missing; ``CARD_RULES`` for a lease.
    """
    if isinstance(ctx.quote, LandedQuote) or (ctx.quote is None and ctx.intent.is_agentic):
        return AGENTIC_RULES
    return CARD_RULES


def evaluate(
    ctx: GateContext,
    *,
    phase: DecisionPhase,
    decision_id: str,
    rules: tuple[Rule, ...] | None = None,
) -> PolicyDecision:
    """Run the rules in order and record the verdict.

    Args:
        ctx: The evaluation context.
        phase: Whether this is the proposal or the apply-time re-check.
        decision_id: Identifier for the resulting decision.
        rules: The ordered rules; by default those ``rules_for`` chooses, unless a test
            substitutes others.

    Returns:
        The decision, naming the rule that fired and every check that ran.
    """
    rules = rules_for(ctx) if rules is None else rules
    checks: list[RuleCheck] = []
    disposition, deciding_rule, reason = Disposition.ALLOW, ALL_RULES_PASSED, "every rule passed"
    for rule in rules:
        try:
            passed, detail = rule.check(ctx)
        except Exception as error:  # fail closed on any defect inside a rule
            passed, detail = False, f"{type(error).__name__}: {error}"
            checks.append(RuleCheck(rule=rule.id, passed=False, detail=detail))
            disposition, deciding_rule, reason = Disposition.REFUSE, INTERNAL_ERROR, detail
            break
        checks.append(RuleCheck(rule=rule.id, passed=passed, detail=detail))
        if not passed:
            disposition, deciding_rule, reason = rule.on_fail, rule.id, detail
            break
    runway_before = ctx.runway_before_usd
    refused = disposition == Disposition.REFUSE
    runway_after = runway_before if refused else runway_before - ctx.intent.amount_usd
    return PolicyDecision(
        id=decision_id,
        intent_id=ctx.intent.id,
        objective_id=ctx.intent.objective_id,
        phase=phase,
        disposition=disposition,
        rule=deciding_rule,
        reason=reason,
        checks=checks,
        runway_before_usd=runway_before,
        runway_after_usd=runway_after,
        decided_at=ctx.now,
    )

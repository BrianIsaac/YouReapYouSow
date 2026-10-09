"""Tests for the ordered authority rules: every refusal proven, in order."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from tests.conftest import START
from tests.factories import (
    MERCHANT,
    OTHER_MERCHANT,
    VARIANT_ID,
    make_enrolment,
    make_grant,
    make_landed_quote,
    make_objective,
    make_offer,
    make_part_grant,
    make_part_objective,
    make_quote,
)
from youreapyousow.authority.grants import AuthorityGrant
from youreapyousow.authority.rules import (
    AGENTIC_RULES,
    ALL_RULES_PASSED,
    CARD_RULES,
    INTERNAL_ERROR,
    GateContext,
    NeedJudge,
    Rule,
    RuleResult,
    evaluate,
    rules_for,
)
from youreapyousow.domain import (
    DecisionPhase,
    Disposition,
    IntentState,
    LandedQuote,
    Objective,
    ObjectiveStatus,
    PurchaseIntent,
    Quote,
    ResourceKind,
)
from youreapyousow.reap.models import EnrollmentStatus


def _intent(**overrides: object) -> PurchaseIntent:
    base = PurchaseIntent(
        id="int_1",
        objective_id="obj_1",
        quote_id="quo_1",
        provider="vast",
        offer_id="52727526",
        amount_usd=Decimal("1.46"),
        rationale="cheapest 24 GB offer",
        created_at=START,
    )
    return base.model_copy(update=overrides)


class _Default:
    """Marks an argument left at its happy-path value."""


DEFAULT = _Default()


def _ctx(
    *,
    intent: PurchaseIntent | None = None,
    quote: Quote | _Default | None = DEFAULT,
    objective: Objective | _Default | None = DEFAULT,
    grant: AuthorityGrant | _Default | None = DEFAULT,
    committed_usd: Decimal = Decimal(0),
    committed_today_usd: Decimal = Decimal(0),
    now: datetime = START + timedelta(seconds=5),
) -> GateContext:
    return GateContext(
        intent=intent or _intent(),
        quote=make_quote() if isinstance(quote, _Default) else quote,
        objective=make_objective() if isinstance(objective, _Default) else objective,
        grant=make_grant() if isinstance(grant, _Default) else grant,
        committed_usd=committed_usd,
        committed_today_usd=committed_today_usd,
        now=now,
    )


def _completed_objective() -> Objective:
    return make_objective().model_copy(update={"status": ObjectiveStatus.COMPLETED})


def _decide(ctx: GateContext) -> tuple[Disposition, str]:
    decision = evaluate(ctx, phase=DecisionPhase.PROPOSAL, decision_id="dec_1")
    return decision.disposition, decision.rule


def test_happy_path_allows_and_records_every_check() -> None:
    """A purchase inside every limit is allowed, with all rules on the record."""
    decision = evaluate(_ctx(), phase=DecisionPhase.PROPOSAL, decision_id="dec_1")
    assert decision.disposition == Disposition.ALLOW
    assert decision.rule == ALL_RULES_PASSED
    assert [c.rule for c in decision.checks] == [r.id for r in CARD_RULES]
    assert all(c.passed for c in decision.checks)
    assert decision.runway_before_usd == Decimal(25)
    assert decision.runway_after_usd == Decimal("23.54")


@pytest.mark.parametrize(
    ("ctx", "rule"),
    [
        (_ctx(intent=_intent(amount_usd=Decimal(0))), "intent.well_formed"),
        (_ctx(quote=None), "intent.well_formed"),
        (_ctx(quote=make_quote(objective_id="obj_other")), "intent.well_formed"),
        (_ctx(objective=None), "objective.active"),
        (_ctx(objective=_completed_objective()), "objective.active"),
        (_ctx(grant=None), "grant.present"),
        (_ctx(grant=make_grant().model_copy(update={"revoked_at": START})), "grant.not_revoked"),
        (_ctx(now=START + timedelta(hours=4)), "grant.not_expired"),
        (_ctx(grant=make_grant(issued_at=START + timedelta(minutes=1))), "grant.not_expired"),
        (_ctx(now=START + timedelta(seconds=60)), "quote.not_expired"),
        (_ctx(intent=_intent(amount_usd=Decimal("1.45"))), "quote.exact_match"),
        (_ctx(intent=_intent(provider="runpod")), "quote.exact_match"),
        (_ctx(intent=_intent(offer_id="other")), "quote.exact_match"),
        (_ctx(grant=make_grant(providers=("runpod",))), "provider.allowed"),
        (
            _ctx(
                quote=make_quote(
                    offer=make_offer().model_copy(update={"kind": ResourceKind.MODEL_API})
                )
            ),
            "resource_kind.allowed",
        ),
        (_ctx(grant=make_grant(max_hourly="0.50")), "rate.max_hourly"),
        (_ctx(grant=make_grant(per_tx="1.00")), "amount.per_transaction_cap"),
        (_ctx(committed_today_usd=Decimal("19.00")), "amount.daily_cap"),
        (_ctx(committed_usd=Decimal("24.00")), "budget.runway"),
    ],
)
def test_each_rule_refuses(ctx: GateContext, rule: str) -> None:
    """Each hard limit refuses, and the decision names that rule."""
    assert _decide(ctx) == (Disposition.REFUSE, rule)


def test_first_failing_rule_decides() -> None:
    """With several violations, the earliest rule in the order is the one recorded."""
    ctx = _ctx(grant=make_grant(providers=("runpod",), per_tx="1.00"), committed_usd=Decimal(25))
    decision = evaluate(ctx, phase=DecisionPhase.APPLY, decision_id="dec_1")
    assert decision.rule == "provider.allowed"
    assert decision.checks[-1].rule == "provider.allowed"
    assert not decision.checks[-1].passed
    assert decision.runway_after_usd == decision.runway_before_usd


def test_approval_threshold_escalates_until_approved() -> None:
    """Above the threshold the purchase is held for the operator, not refused."""
    ctx = _ctx(grant=make_grant(approval="1.00"))
    assert _decide(ctx) == (Disposition.ESCALATE, "approval.threshold")
    approved = _ctx(grant=make_grant(approval="1.00"), intent=_intent(approved_by="operator"))
    assert _decide(approved) == (Disposition.ALLOW, ALL_RULES_PASSED)


def test_limits_are_inclusive() -> None:
    """Spending exactly the cap, the daily remainder or the runway is allowed."""
    ctx = _ctx(
        grant=make_grant(per_tx="1.46", daily="20", max_hourly="0.73"),
        committed_today_usd=Decimal("18.54"),
        committed_usd=Decimal("23.54"),
    )
    assert _decide(ctx) == (Disposition.ALLOW, ALL_RULES_PASSED)


def test_a_rule_that_raises_fails_closed() -> None:
    """A defect inside a rule refuses the purchase instead of letting it through."""

    def broken(_: GateContext) -> tuple[bool, str]:
        raise ZeroDivisionError("bad maths")

    decision = evaluate(
        _ctx(), phase=DecisionPhase.PROPOSAL, decision_id="d", rules=(Rule("broken", broken),)
    )
    assert decision.disposition == Disposition.REFUSE
    assert decision.rule == INTERNAL_ERROR
    assert "ZeroDivisionError" in decision.reason


def test_later_rules_refuse_when_an_earlier_guarantee_is_missing() -> None:
    """Rules used out of order still refuse rather than crash the gate."""
    by_id = {r.id: r for r in CARD_RULES}
    decision = evaluate(
        _ctx(grant=None),
        phase=DecisionPhase.PROPOSAL,
        decision_id="d",
        rules=(by_id["provider.allowed"],),
    )
    assert (decision.disposition, decision.rule) == (Disposition.REFUSE, INTERNAL_ERROR)


@pytest.mark.parametrize(
    ("price", "ceiling", "detail"),
    [
        ("0.07111111111111111", "1.00", "rate $0.0711/h within ceiling $1/h"),
        ("0.73", "0.50", "rate $0.73/h exceeds ceiling $0.5/h"),
        ("0.50001", "0.50", "rate $0.50001/h exceeds ceiling $0.5/h"),
    ],
)
def test_the_hourly_check_reads_at_four_places(price: str, ceiling: str, detail: str) -> None:
    """A sub-cent rate reads rounded, unless rounding would hide why it was refused."""
    ctx = _ctx(
        quote=make_quote(offer=make_offer(price=price)), grant=make_grant(max_hourly=ceiling)
    )
    rule = {r.id: r for r in CARD_RULES}["rate.max_hourly"]
    assert rule.check(ctx)[1] == detail


def test_check_details_read_as_money_and_utc() -> None:
    """Every detail on the dashboard reads as dollars and UTC times, not raw values."""
    by_id = {r.id: r for r in CARD_RULES}
    ctx = _ctx()
    details = {rule: by_id[rule].check(ctx)[1] for rule in by_id}
    assert details["grant.not_expired"] == "grant valid until 2037-10-09 12:42:03 UTC"
    assert details["quote.not_expired"].endswith(" UTC")
    assert details["amount.per_transaction_cap"] == "amount $1.46 within per-transaction cap $5.00"
    assert details["budget.runway"] == "runway $25.00 leaves $23.54 after purchase"
    over = _ctx(grant=make_grant(per_tx="1.00"))
    assert by_id["amount.per_transaction_cap"].check(over) == (
        False,
        "amount $1.46 exceeds per-transaction cap $1.00",
    )


# The agentic path: the gate on a quote's landed price.

AGENTIC_ORDER = [
    "intent.well_formed",
    "objective.active",
    "grant.present",
    "grant.not_revoked",
    "grant.not_expired",
    "enrolment.active",
    "quote.not_expired",
    "quote.exact_match",
    "merchant.in_scope",
    "item.matches_need",
    "currency.matches_budget",
    "amount.per_transaction_cap",
    "amount.daily_cap",
    "budget.runway",
    "attempts.retry_limit",
    "need.not_already_ordered",
    "approval.threshold",
]

CARD_ORDER = [
    "intent.well_formed",
    "objective.active",
    "grant.present",
    "grant.not_revoked",
    "grant.not_expired",
    "quote.not_expired",
    "quote.exact_match",
    "provider.allowed",
    "resource_kind.allowed",
    "rate.max_hourly",
    "amount.per_transaction_cap",
    "amount.daily_cap",
    "budget.runway",
    "approval.threshold",
]


def _matches(_: LandedQuote) -> RuleResult:
    return True, "1 TB NVMe matches the bill of materials"


def _part_intent(**overrides: object) -> PurchaseIntent:
    base = PurchaseIntent(
        id="int_1",
        objective_id="obj_1",
        quote_id="lq_1",
        provider=MERCHANT,
        offer_id=VARIANT_ID,
        amount_usd=Decimal("142.00"),
        rationale="cheapest landed price for the drive",
        created_at=START,
        need_id="need_1",
        attempt=1,
    )
    return base.model_copy(update=overrides)


def _attempt(n: int, state: IntentState) -> PurchaseIntent:
    return _part_intent(id=f"int_prior_{n}", quote_id=f"lq_prior_{n}", state=state)


def _actx(
    *,
    intent: PurchaseIntent | None = None,
    quote: LandedQuote | _Default | None = DEFAULT,
    objective: Objective | _Default | None = DEFAULT,
    grant: AuthorityGrant | _Default | None = DEFAULT,
    committed_usd: Decimal = Decimal(0),
    committed_today_usd: Decimal = Decimal(0),
    now: datetime = START + timedelta(seconds=5),
    need_judge: NeedJudge | None = _matches,
    need_intents: tuple[PurchaseIntent, ...] = (),
) -> GateContext:
    return GateContext(
        intent=intent or _part_intent(),
        quote=make_landed_quote() if isinstance(quote, _Default) else quote,
        objective=make_part_objective() if isinstance(objective, _Default) else objective,
        grant=make_part_grant() if isinstance(grant, _Default) else grant,
        committed_usd=committed_usd,
        committed_today_usd=committed_today_usd,
        now=now,
        need_judge=need_judge,
        need_intents=need_intents,
    )


def _rule(rule_id: str) -> Rule:
    return {r.id: r for r in AGENTIC_RULES}[rule_id]


def test_the_agentic_rules_run_in_their_order() -> None:
    """Seventeen rules, in order; the card path keeps its fourteen."""
    assert [r.id for r in AGENTIC_RULES] == AGENTIC_ORDER
    assert [r.id for r in CARD_RULES] == CARD_ORDER
    assert [r.id for r in AGENTIC_RULES if r.on_fail == Disposition.ESCALATE] == [
        "approval.threshold"
    ]


def test_a_landed_quote_inside_every_limit_is_allowed_with_every_check_listed() -> None:
    """The decision lists the agentic checks exactly, and the runway moves by finalAmount."""
    decision = evaluate(_actx(), phase=DecisionPhase.PROPOSAL, decision_id="dec_1")
    assert (decision.disposition, decision.rule) == (Disposition.ALLOW, ALL_RULES_PASSED)
    assert [c.rule for c in decision.checks] == AGENTIC_ORDER
    assert all(c.passed for c in decision.checks)
    assert decision.runway_before_usd == Decimal(500)
    assert decision.runway_after_usd == Decimal(358)


def test_the_rule_set_follows_the_quote() -> None:
    """A landed quote is judged by the agentic rules, a lease by the card path's."""
    assert rules_for(_actx()) == AGENTIC_RULES
    assert rules_for(_ctx()) == CARD_RULES
    assert rules_for(_actx(quote=None)) == AGENTIC_RULES
    assert rules_for(_ctx(quote=None)) == CARD_RULES
    decision = evaluate(_ctx(), phase=DecisionPhase.PROPOSAL, decision_id="d")
    assert [c.rule for c in decision.checks] == CARD_ORDER


def _expiry(quote: LandedQuote, seconds: float) -> datetime:
    return quote.expires_at + timedelta(seconds=seconds)


QUOTE = make_landed_quote()


@pytest.mark.parametrize(
    ("ctx", "rule"),
    [
        (_actx(intent=_part_intent(amount_usd=Decimal(0))), "intent.well_formed"),
        (_actx(quote=None), "intent.well_formed"),
        (_actx(quote=make_landed_quote(objective_id="obj_other")), "intent.well_formed"),
        (_actx(quote=make_landed_quote(need_id="need_other")), "intent.well_formed"),
        (_actx(intent=_part_intent(need_id=None)), "intent.well_formed"),
        (_actx(objective=make_objective(budget="500")), "enrolment.active"),
        (
            _actx(
                objective=make_objective(
                    budget="500",
                    enrolment=make_enrolment(status=EnrollmentStatus.REQUIRES_ACTION),
                )
            ),
            "enrolment.active",
        ),
        (
            _actx(
                objective=make_objective(
                    budget="500", enrolment=make_enrolment(status=EnrollmentStatus.REVOKED)
                )
            ),
            "enrolment.active",
        ),
        (_actx(now=_expiry(QUOTE, -15)), "quote.not_expired"),
        (_actx(now=_expiry(QUOTE, -1)), "quote.not_expired"),
        (_actx(now=_expiry(QUOTE, 0)), "quote.not_expired"),
        (_actx(intent=_part_intent(provider=OTHER_MERCHANT)), "quote.exact_match"),
        (_actx(intent=_part_intent(offer_id="var_nvme_2tb")), "quote.exact_match"),
        (_actx(intent=_part_intent(quantity=2)), "quote.exact_match"),
        (_actx(intent=_part_intent(amount_usd=Decimal("119.00"))), "quote.exact_match"),
        (_actx(intent=_part_intent(currency="SGD")), "quote.exact_match"),
        (_actx(grant=make_part_grant(merchants=(OTHER_MERCHANT,))), "merchant.in_scope"),
        (
            _actx(need_judge=lambda _: (False, "Capacity 512 GB is not 1 TB or 2 TB")),
            "item.matches_need",
        ),
        (_actx(need_judge=None), "item.matches_need"),
        (
            _actx(quote=make_landed_quote(currency="SGD"), intent=_part_intent(currency="SGD")),
            "currency.matches_budget",
        ),
        (_actx(grant=make_part_grant(per_tx="141.99")), "amount.per_transaction_cap"),
        (_actx(committed_today_usd=Decimal("158.01")), "amount.daily_cap"),
        (_actx(committed_usd=Decimal("358.01")), "budget.runway"),
        (
            _actx(
                need_intents=(
                    _attempt(1, IntentState.FAILED),
                    _attempt(2, IntentState.EXPIRED),
                    _attempt(3, IntentState.DECLINED),
                )
            ),
            "attempts.retry_limit",
        ),
        (
            _actx(
                grant=make_part_grant(attempts=1),
                need_intents=(_attempt(1, IntentState.FAILED),),
            ),
            "attempts.retry_limit",
        ),
        (
            _actx(need_intents=(_attempt(1, IntentState.OUTCOME_UNKNOWN),)),
            "attempts.retry_limit",
        ),
        (_actx(need_intents=(_attempt(1, IntentState.EXECUTING),)), "need.not_already_ordered"),
        (
            _actx(need_intents=(_attempt(1, IntentState.AWAITING_APPROVAL),)),
            "need.not_already_ordered",
        ),
        (_actx(need_intents=(_attempt(1, IntentState.COMPLETED),)), "need.not_already_ordered"),
    ],
)
def test_each_agentic_rule_refuses(ctx: GateContext, rule: str) -> None:
    """Each of the agentic hard limits refuses, and the decision names that rule."""
    decision = evaluate(ctx, phase=DecisionPhase.APPLY, decision_id="dec_1")
    assert (decision.disposition, decision.rule) == (Disposition.REFUSE, rule)
    assert [c.rule for c in decision.checks] == AGENTIC_ORDER[: AGENTIC_ORDER.index(rule) + 1]
    assert decision.runway_after_usd == decision.runway_before_usd


@pytest.mark.parametrize(
    "ctx",
    [
        _actx(now=_expiry(QUOTE, -16)),
        _actx(now=_expiry(QUOTE, -1), grant=make_part_grant(margin_s=0)),
        _actx(grant=make_part_grant(merchants=("  northwind   PARTS ",))),
        _actx(grant=make_part_grant(per_tx="142.00")),
        _actx(committed_today_usd=Decimal("158.00")),
        _actx(committed_usd=Decimal("358.00")),
        _actx(need_intents=(_attempt(1, IntentState.FAILED), _attempt(2, IntentState.EXPIRED))),
        _actx(
            need_intents=(
                _attempt(1, IntentState.REFUSED),
                _attempt(2, IntentState.ALLOWED),
                _attempt(3, IntentState.ESCALATED),
                _attempt(4, IntentState.PROPOSED),
            )
        ),
    ],
)
def test_each_agentic_limit_is_inclusive_and_passes(ctx: GateContext) -> None:
    """At the boundary, and with attempts that never ordered, the purchase is allowed."""
    decision = evaluate(ctx, phase=DecisionPhase.APPLY, decision_id="dec_1")
    assert (decision.disposition, decision.rule) == (Disposition.ALLOW, ALL_RULES_PASSED)


def test_the_landed_price_escalates_above_the_threshold_until_approved() -> None:
    """Above the approval threshold the purchase waits for the operator, rule 17."""
    held = _actx(grant=make_part_grant(approval="120"))
    decision = evaluate(held, phase=DecisionPhase.PROPOSAL, decision_id="d")
    assert (decision.disposition, decision.rule) == (Disposition.ESCALATE, "approval.threshold")
    assert [c.rule for c in decision.checks] == AGENTIC_ORDER
    assert decision.runway_after_usd == Decimal(358)
    approved = _actx(grant=make_part_grant(approval="120"), intent=_part_intent(approved_by="op"))
    assert evaluate(approved, phase=DecisionPhase.APPLY, decision_id="d").rule == ALL_RULES_PASSED


@pytest.mark.parametrize(
    ("ctx", "rule"),
    [
        (
            _actx(
                objective=make_objective(budget="500"),
                grant=make_part_grant(merchants=(OTHER_MERCHANT,), per_tx="1"),
            ),
            "enrolment.active",
        ),
        (
            _actx(
                now=_expiry(QUOTE, -1),
                intent=_part_intent(amount_usd=Decimal(1)),
                committed_usd=Decimal(500),
            ),
            "quote.not_expired",
        ),
        (
            _actx(
                grant=make_part_grant(merchants=(OTHER_MERCHANT,), per_tx="1", approval="1"),
                need_judge=None,
            ),
            "merchant.in_scope",
        ),
        (
            _actx(
                need_judge=lambda _: (False, "wrong part"),
                quote=make_landed_quote(currency="SGD"),
                intent=_part_intent(currency="SGD"),
            ),
            "item.matches_need",
        ),
        (
            _actx(
                grant=make_part_grant(per_tx="100", daily="100"),
                need_intents=(_attempt(1, IntentState.COMPLETED),),
            ),
            "amount.per_transaction_cap",
        ),
        (
            _actx(
                committed_usd=Decimal(400),
                need_intents=(_attempt(1, IntentState.OUTCOME_UNKNOWN),),
            ),
            "budget.runway",
        ),
        (
            _actx(
                need_intents=(
                    _attempt(1, IntentState.OUTCOME_UNKNOWN),
                    _attempt(2, IntentState.EXECUTING),
                ),
                grant=make_part_grant(approval="1"),
            ),
            "attempts.retry_limit",
        ),
        (
            _actx(
                need_intents=(_attempt(1, IntentState.COMPLETED),),
                grant=make_part_grant(approval="1"),
            ),
            "need.not_already_ordered",
        ),
    ],
)
def test_the_first_failing_agentic_rule_decides(ctx: GateContext, rule: str) -> None:
    """With several violations, the earliest in the agentic order is recorded."""
    decision = evaluate(ctx, phase=DecisionPhase.APPLY, decision_id="d")
    assert (decision.disposition, decision.rule) == (Disposition.REFUSE, rule)
    assert decision.checks[-1].rule == rule
    assert not decision.checks[-1].passed
    assert all(c.passed for c in decision.checks[:-1])


def test_a_need_judge_that_raises_fails_closed() -> None:
    """A defect in the need's judgement refuses the purchase."""

    def broken(_: LandedQuote) -> RuleResult:
        raise KeyError("Capacity")

    decision = evaluate(_actx(need_judge=broken), phase=DecisionPhase.APPLY, decision_id="d")
    assert (decision.disposition, decision.rule) == (Disposition.REFUSE, INTERNAL_ERROR)
    assert decision.checks[-1].rule == "item.matches_need"


def test_a_card_rule_given_a_landed_quote_fails_closed() -> None:
    """The dormant card rules cannot read a catalogue quote, so they refuse."""
    by_id = {r.id: r for r in CARD_RULES}
    decision = evaluate(
        _actx(), phase=DecisionPhase.APPLY, decision_id="d", rules=(by_id["provider.allowed"],)
    )
    assert (decision.disposition, decision.rule) == (Disposition.REFUSE, INTERNAL_ERROR)


def test_agentic_check_details_read_as_money_margins_and_names() -> None:
    """Every agentic detail on the dashboard names the figures it compared."""
    ctx = _actx()
    details = {rule: _rule(rule).check(ctx) for rule in AGENTIC_ORDER}
    assert details["intent.well_formed"] == (
        True,
        "intent names an existing quote for its objective",
    )
    assert details["enrolment.active"] == (True, "enrolment enr_1 is active (VISA 4242)")
    assert details["quote.not_expired"] == (
        True,
        "quote valid until 2037-10-09 08:44:03 UTC, outside the 15 s safety margin",
    )
    assert details["quote.exact_match"] == (
        True,
        f"1 x {VARIANT_ID} from {MERCHANT} at $142.00 exactly as quoted",
    )
    assert details["merchant.in_scope"] == (True, f"{MERCHANT} is in the merchant scope")
    assert details["item.matches_need"] == (True, "1 TB NVMe matches the bill of materials")
    assert details["currency.matches_budget"] == (True, "USD is the budget's currency")
    assert details["amount.per_transaction_cap"] == (
        True,
        "amount $142.00 within per-transaction cap $150.00",
    )
    assert details["amount.daily_cap"] == (True, "today's spend would be $142.00 of $300.00")
    assert details["budget.runway"] == (True, "runway $500.00 leaves $358.00 after purchase")
    assert details["attempts.retry_limit"] == (True, "0 of 3 attempts at need_1 have failed")
    assert details["need.not_already_ordered"] == (True, "need_1 has no open or completed order")
    assert details["approval.threshold"] == (True, "within autonomous authority")


@pytest.mark.parametrize(
    ("ctx", "rule", "detail"),
    [
        (
            _actx(quote=make_landed_quote(need_id="need_2")),
            "intent.well_formed",
            "quote lq_1 is for need need_2, not need_1",
        ),
        (
            _actx(intent=_part_intent(need_id=None)),
            "intent.well_formed",
            "quote lq_1 is a catalogue quote but the intent names no need",
        ),
        (
            _actx(objective=make_objective(budget="500")),
            "enrolment.active",
            "objective obj_1 has no agentic enrolment",
        ),
        (
            _actx(
                objective=make_objective(
                    budget="500", enrolment=make_enrolment(status=EnrollmentStatus.REVOKED)
                )
            ),
            "enrolment.active",
            "enrolment enr_1 is REVOKED, not ACTIVE",
        ),
        (
            _actx(now=_expiry(QUOTE, -10)),
            "quote.not_expired",
            "quote expires at 2037-10-09 08:44:03 UTC, inside the 15 s safety margin",
        ),
        (
            _actx(now=_expiry(QUOTE, 1)),
            "quote.not_expired",
            "quote expired at 2037-10-09 08:44:03 UTC",
        ),
        (
            _actx(intent=_part_intent(provider=OTHER_MERCHANT, quantity=2, currency="SGD")),
            "quote.exact_match",
            f"merchant {OTHER_MERCHANT} != quoted {MERCHANT}; quantity 2 != quoted 1; "
            "currency SGD != quoted USD",
        ),
        (
            _actx(grant=make_part_grant(merchants=(OTHER_MERCHANT,))),
            "merchant.in_scope",
            f"{MERCHANT} is not in the merchant scope ['{OTHER_MERCHANT}']",
        ),
        (_actx(need_judge=None), "item.matches_need", "no judgement is configured for need need_1"),
        (
            _actx(quote=make_landed_quote(currency="SGD"), intent=_part_intent(currency="SGD")),
            "currency.matches_budget",
            "quote is in SGD, the budget is in USD",
        ),
        (
            _actx(need_intents=(_attempt(1, IntentState.OUTCOME_UNKNOWN),)),
            "attempts.retry_limit",
            "attempt int_prior_1 at need_1 has an unknown outcome; reconcile it first",
        ),
        (
            _actx(need_intents=tuple(_attempt(n, IntentState.FAILED) for n in range(3))),
            "attempts.retry_limit",
            "3 of 3 attempts at need_1 have failed",
        ),
        (
            _actx(
                need_intents=(
                    _attempt(1, IntentState.COMPLETED).model_copy(update={"order_id": "ORD-9"}),
                )
            ),
            "need.not_already_ordered",
            "need_1 already has int_prior_1 completed (order ORD-9)",
        ),
        (
            _actx(need_intents=(_attempt(1, IntentState.AWAITING_APPROVAL),)),
            "need.not_already_ordered",
            "need_1 already has int_prior_1 awaiting_approval",
        ),
    ],
)
def test_agentic_refusals_say_why(ctx: GateContext, rule: str, detail: str) -> None:
    """Each refusal's detail names what was compared."""
    assert _rule(rule).check(ctx) == (False, detail)

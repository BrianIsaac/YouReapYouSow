"""Tests for the exact-action gate: hold, decide, claim once, record outcomes."""

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.factories import (
    make_enrolment,
    make_grant,
    make_landed_quote,
    make_objective,
    make_offer,
    make_part_grant,
    make_part_objective,
    make_quote,
)
from youreapyousow.authority.gate import AuthorityGate, InvalidTransitionError
from youreapyousow.authority.grants import GrantError
from youreapyousow.authority.rules import RuleResult
from youreapyousow.clock import ManualClock
from youreapyousow.domain import (
    Disposition,
    IntentState,
    LandedQuote,
    PurchaseIntent,
    Quote,
    committed_amount,
)
from youreapyousow.ledger.events import EventType, LedgerEvent
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.reap.models import Checkout, CheckoutCreated, CheckoutStatus, EnrollmentStatus
from youreapyousow.repos import Repositories
from youreapyousow.store import Database


@pytest.fixture
def repos(db: Database) -> Repositories:
    """Repositories seeded with the demo objective.

    Returns:
        The repositories.
    """
    repos = Repositories(db)
    objective = make_objective()
    repos.objectives.insert(objective, record_id=objective.id, objective_id=objective.id)
    return repos


@pytest.fixture
def gate(repos: Repositories, ledger: Ledger, clock: ManualClock) -> AuthorityGate:
    """A gate with the default grant issued.

    Returns:
        The gate.
    """
    gate = AuthorityGate(repos, ledger, clock)
    gate.issue_grant(make_grant())
    return gate


def _quote(repos: Repositories, quote: Quote | None = None) -> Quote:
    quote = quote or make_quote()
    repos.quotes.insert(quote, record_id=quote.id, objective_id=quote.objective_id)
    return quote


def _propose(
    gate: AuthorityGate, quote: Quote, *, amount_usd: Decimal | None = None
) -> PurchaseIntent:
    intent, _ = gate.propose(
        objective_id=quote.objective_id,
        quote_id=quote.id,
        provider=quote.offer.provider,
        offer_id=quote.offer.offer_id,
        amount_usd=quote.amount_usd if amount_usd is None else amount_usd,
        rationale="cheapest",
        options_considered=[quote.offer.key],
    )
    return intent


def test_propose_allows_and_ledger_records_intent_then_decision(
    gate: AuthorityGate, repos: Repositories, ledger: Ledger
) -> None:
    """A good proposal is held, decided and both steps land on the ledger."""
    intent, decision = gate.propose(
        objective_id="obj_1",
        quote_id=_quote(repos).id,
        provider="vast",
        offer_id="52727526",
        amount_usd=Decimal("1.46"),
        rationale="cheapest",
        options_considered=["vast:52727526", "runpod:rtx4090"],
    )
    assert intent.state == IntentState.ALLOWED
    assert decision.disposition == Disposition.ALLOW
    assert intent.decision_id == decision.id
    types = [e.type for e in ledger.events(objective_id="obj_1")]
    assert types == [EventType.GRANT_ISSUED, EventType.PURCHASE_PROPOSED, EventType.POLICY_DECIDED]
    decided = ledger.events(types=[EventType.POLICY_DECIDED])[0]
    assert decided.refs == {
        "intent": intent.id,
        "quote": "quo_1",
        "decision": decision.id,
        "grant": "grt_1",
    }
    assert decided.payload["rule"] == "all_rules_passed"


def test_propose_names_who_the_purchase_is_for_on_the_ledger_only(
    gate: AuthorityGate, repos: Repositories, ledger: Ledger
) -> None:
    """``on_behalf_of`` lands in the proposal's payload and changes no decision."""
    intent, decision = gate.propose(
        objective_id="obj_1",
        quote_id=_quote(repos).id,
        provider="vast",
        offer_id="52727526",
        amount_usd=Decimal("1.46"),
        rationale="cheapest",
        options_considered=["vast:52727526"],
        on_behalf_of="Alice",
    )
    assert decision.disposition == Disposition.ALLOW
    proposed = ledger.events(types=[EventType.PURCHASE_PROPOSED])[0]
    assert proposed.subject_id == intent.id
    assert proposed.payload["on_behalf_of"] == "Alice"


def test_refused_proposal_is_never_claimable(gate: AuthorityGate, repos: Repositories) -> None:
    """A refused intent cannot be claimed, so no money can move for it."""
    intent = _propose(gate, _quote(repos), amount_usd=Decimal("9.99"))
    assert intent.state == IntentState.REFUSED
    result = gate.claim(intent.id)
    assert not result.claimed
    assert result.intent.state == IntentState.REFUSED


def test_claim_mints_key_from_claim_event_and_only_once(
    gate: AuthorityGate, repos: Repositories, ledger: Ledger
) -> None:
    """The first claim wins with a key built from its ledger event; a replay does not."""
    intent = _propose(gate, _quote(repos))
    first = gate.claim(intent.id)
    assert first.claimed
    assert first.intent.state == IntentState.EXECUTING
    claim_event = ledger.events(types=[EventType.PURCHASE_CLAIMED])[0]
    assert first.intent.idempotency_key == f"{intent.id}:{claim_event.event_id}"

    second = gate.claim(intent.id)
    assert not second.claimed
    assert second.intent.idempotency_key == first.intent.idempotency_key
    assert len(ledger.events(types=[EventType.PURCHASE_CLAIMED])) == 1


def test_revocation_after_proposal_refuses_at_apply_time(
    gate: AuthorityGate, repos: Repositories
) -> None:
    """Authority is re-read immediately before money moves."""
    intent = _propose(gate, _quote(repos))
    gate.revoke_grant("grt_1", "operator stopped the objective")
    result = gate.claim(intent.id)
    assert not result.claimed
    assert result.decision is not None
    assert result.decision.rule == "grant.not_revoked"
    assert result.intent.state == IntentState.REFUSED


def test_expiry_between_proposal_and_claim_refuses(
    gate: AuthorityGate, repos: Repositories, clock: ManualClock
) -> None:
    """A quote that lapses before the claim cannot be bought."""
    intent = _propose(gate, _quote(repos))
    clock.advance(seconds=61)
    result = gate.claim(intent.id)
    assert result.decision is not None
    assert result.decision.rule == "quote.not_expired"


def test_runway_consumed_by_another_purchase_refuses_at_apply_time(
    gate: AuthorityGate, repos: Repositories
) -> None:
    """Two allowed intents that together exceed the daily cap cannot both be claimed."""
    big = make_offer(offer_id="big", price="1.00")
    first = _propose(gate, _quote(repos, make_quote(quote_id="q1", offer=big, hours="5")))
    second = _propose(gate, _quote(repos, make_quote(quote_id="q2", offer=big, hours="5")))
    third = _propose(gate, _quote(repos, make_quote(quote_id="q3", offer=big, hours="5")))
    fourth = _propose(gate, _quote(repos, make_quote(quote_id="q4", offer=big, hours="5")))
    fifth = _propose(gate, _quote(repos, make_quote(quote_id="q5", offer=big, hours="5")))
    assert all(i.state == IntentState.ALLOWED for i in (first, second, third, fourth, fifth))

    for intent in (first, second, third, fourth):
        assert gate.claim(intent.id).claimed
    late = gate.claim(fifth.id)
    assert not late.claimed
    assert late.decision is not None
    assert late.decision.rule == "amount.daily_cap"
    assert gate.runway_usd("obj_1") == Decimal(5)


def test_escalation_waits_for_operator_then_claims(
    gate: AuthorityGate, repos: Repositories, ledger: Ledger
) -> None:
    """Above the threshold the intent is held until approved; rejection refuses it."""
    gate.issue_grant(make_grant(grant_id="grt_2", approval="1.00"))
    held = _propose(gate, _quote(repos))
    assert held.state == IntentState.ESCALATED
    assert not gate.claim(held.id).claimed

    approved = gate.approve(held.id, "operator")
    assert approved.state == IntentState.ALLOWED
    assert gate.claim(held.id).claimed
    with pytest.raises(InvalidTransitionError):
        gate.approve(held.id, "operator")

    other = _propose(gate, _quote(repos, make_quote(quote_id="q2")))
    rejected = gate.reject(other.id, "operator", "not now")
    assert rejected.state == IntentState.REFUSED
    assert ledger.events(types=[EventType.PURCHASE_REJECTED])[0].payload["reason"] == "not now"


def test_outcomes_move_runway_and_follow_the_state_machine(
    gate: AuthorityGate, repos: Repositories
) -> None:
    """Authorised spend counts in full, settled spend at what cleared, declined not at all."""
    intent = _propose(gate, _quote(repos))
    with pytest.raises(InvalidTransitionError):
        gate.record_authorised(intent.id, reap_transaction_id="tx_1")
    gate.claim(intent.id)
    assert gate.runway_usd("obj_1") == Decimal("23.54")

    authorised = gate.record_authorised(intent.id, reap_transaction_id="tx_1")
    assert authorised.state == IntentState.AUTHORISED
    with pytest.raises(ValueError, match="outside"):
        gate.record_settled(intent.id, amount_usd=Decimal("1.47"))
    settled = gate.record_settled(intent.id, amount_usd=Decimal("0.44"))
    assert settled.state == IntentState.SETTLED
    assert gate.runway_usd("obj_1") == Decimal("24.56")

    declined = _propose(gate, _quote(repos, make_quote(quote_id="q2")))
    gate.claim(declined.id)
    gate.record_declined(
        declined.id, reap_transaction_id="tx_2", code="SPEND_LIMIT_EXCEEDED", policy_name="daily"
    )
    assert gate.runway_usd("obj_1") == Decimal("24.56")


def test_outcome_unknown_keeps_counting_and_can_be_reconciled(
    gate: AuthorityGate, repos: Repositories
) -> None:
    """An ambiguous transport failure is never treated as money not spent."""
    intent = _propose(gate, _quote(repos))
    gate.claim(intent.id)
    unknown = gate.record_outcome_unknown(intent.id, error="ReadTimeout")
    assert unknown.state == IntentState.OUTCOME_UNKNOWN
    assert gate.runway_usd("obj_1") == Decimal("23.54")
    assert not gate.claim(intent.id).claimed
    assert (
        gate.record_authorised(intent.id, reap_transaction_id="tx").state == IntentState.AUTHORISED
    )


def test_issue_grant_validates_against_budget(gate: AuthorityGate) -> None:
    """A grant whose daily cap exceeds the budget is refused before it is stored."""
    with pytest.raises(GrantError):
        gate.issue_grant(make_grant(grant_id="grt_bad", daily="26", per_tx="5"))
    grant = gate.current_grant("obj_1")
    assert grant is not None
    assert grant.id == "grt_1"


def test_revoke_is_idempotent(gate: AuthorityGate, clock: ManualClock) -> None:
    """Revoking twice keeps the first revocation time."""
    first = gate.revoke_grant("grt_1", "stop")
    clock.advance(seconds=5)
    assert gate.revoke_grant("grt_1", "stop again").revoked_at == first.revoked_at


def test_daily_cap_resets_on_the_next_utc_day(
    gate: AuthorityGate, repos: Repositories, clock: ManualClock
) -> None:
    """Spend claimed yesterday does not count towards today's cap."""
    big = make_offer(offer_id="big", price="1.00")
    for n in range(4):
        intent = _propose(gate, _quote(repos, make_quote(quote_id=f"d1_{n}", offer=big, hours="5")))
        assert gate.claim(intent.id).claimed
    clock.advance(days=1)
    gate.issue_grant(make_grant(grant_id="grt_day2", issued_at=clock()))
    quote = make_quote(quote_id="d2", offer=make_offer(offer_id="small"), created_at=clock())
    assert gate.claim(_propose(gate, _quote(repos, quote)).id).claimed


def test_external_authorisation_approves_only_an_exact_claimed_match(
    gate: AuthorityGate, repos: Repositories
) -> None:
    """Reap's real-time request is approved only for the purchase the gate claimed."""
    intent = _propose(gate, _quote(repos))
    assert not gate.authorise_external(
        card_id="card_1", amount_usd=Decimal("1.46"), merchant_name="Vast.ai"
    ).approve
    gate.claim(intent.id)

    ok = gate.authorise_external(
        card_id="card_1", amount_usd=Decimal("1.46"), merchant_name="Vast.ai"
    )
    assert (ok.approve, ok.intent_id) == (True, intent.id)
    for card, amount, merchant in (
        ("card_1", "1.47", "Vast.ai"),
        ("card_1", "1.46", "RunPod"),
        ("card_x", "1.46", "Vast.ai"),
    ):
        answer = gate.authorise_external(
            card_id=card, amount_usd=Decimal(amount), merchant_name=merchant
        )
        assert not answer.approve


def test_failed_purchase_releases_the_budget(gate: AuthorityGate, repos: Repositories) -> None:
    """A purchase that certainly never reached Reap stops counting against the runway."""
    intent = _propose(gate, _quote(repos))
    gate.claim(intent.id)
    failed = gate.record_failed(intent.id, code="NOT_SENT", error="connection refused")
    assert (failed.state, failed.decline_code) == (IntentState.FAILED, "NOT_SENT")
    assert gate.runway_usd("obj_1") == Decimal(25)
    with pytest.raises(InvalidTransitionError):
        gate.record_failed(intent.id, code="X", error="again")


# The agentic path: claim, checkout, approval, completion.

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


def _matches(_: LandedQuote) -> RuleResult:
    return True, "matches the bill of materials"


@pytest.fixture
def part_repos(db: Database) -> Repositories:
    """Repositories seeded with shape (b)'s objective, enrolled and with no card.

    Returns:
        The repositories.
    """
    repos = Repositories(db)
    objective = make_part_objective()
    repos.objectives.insert(objective, record_id=objective.id, objective_id=objective.id)
    return repos


@pytest.fixture
def part_gate(part_repos: Repositories, ledger: Ledger, clock: ManualClock) -> AuthorityGate:
    """A gate judging needs as matched, with shape (b)'s grant issued.

    Returns:
        The gate.
    """
    gate = AuthorityGate(part_repos, ledger, clock, need_judge=_matches)
    gate.issue_grant(make_part_grant())
    return gate


def _landed(repos: Repositories, quote: LandedQuote | None = None) -> LandedQuote:
    quote = quote or make_landed_quote()
    repos.landed_quotes.insert(quote, record_id=quote.id, objective_id=quote.objective_id)
    return quote


def _propose_part(
    gate: AuthorityGate, quote: LandedQuote, *, amount_usd: Decimal | None = None
) -> PurchaseIntent:
    terms = quote.terms
    intent, _ = gate.propose(
        objective_id=terms.objective_id,
        quote_id=terms.quote_id,
        provider=terms.merchant,
        offer_id=terms.item,
        amount_usd=terms.amount if amount_usd is None else amount_usd,
        rationale="lowest landed price for the drive",
        options_considered=[quote.variant.key],
        quantity=terms.quantity,
        currency=terms.currency,
    )
    return intent


def _claimed(gate: AuthorityGate, repos: Repositories, quote: LandedQuote | None = None) -> str:
    intent = _propose_part(gate, _landed(repos, quote))
    result = gate.claim(intent.id)
    assert result.claimed
    return intent.id


CHECKOUT_ID = "8c1e7f52-3a4b-4d9e-b0f1-6a2c9d8e7b13"


def _created(
    status: CheckoutStatus = CheckoutStatus.PROCESSING, *, checkout_id: str = CHECKOUT_ID
) -> CheckoutCreated:
    return CheckoutCreated.model_validate(
        {
            "id": checkout_id,
            "status": status.value,
            "quoteId": "5f0c2a4e-8d1b-4f6a-9c3e-2b7d1e0a9f41",
            "enrollmentId": "0d9b8c7a-6e5f-4a3b-9c2d-1e0f9a8b7c6d",
            "amount": {"amount": 142, "currency": "USD"},
            "nextAction": None,
        }
    )


def _checkout(
    status: CheckoutStatus,
    *,
    checkout_id: str = CHECKOUT_ID,
    order_id: str | None = None,
    final: str | None = None,
    currency: str = "USD",
    approval_url: str | None = None,
) -> Checkout:
    return Checkout.model_validate(
        {
            "id": checkout_id,
            "status": status.value,
            "orderId": order_id,
            "finalAmount": {"amount": final, "currency": currency} if final else None,
            "nextAction": {
                "type": "REDIRECT",
                "url": approval_url,
                "expiresAt": "2037-10-09T08:57:03Z",
            }
            if approval_url
            else None,
        }
    )


def _completed(final: str = "142.00", *, order_id: str = "ORD-1001") -> Checkout:
    return _checkout(CheckoutStatus.COMPLETED, order_id=order_id, final=final)


def test_a_catalogue_proposal_is_decided_by_the_agentic_rules_and_bound_to_its_need(
    part_gate: AuthorityGate, part_repos: Repositories, ledger: Ledger
) -> None:
    """The intent takes its need and attempt from the quote; the decision lists all 17."""
    intent, decision = part_gate.propose(
        objective_id="obj_1",
        quote_id=_landed(part_repos, make_landed_quote(attempt=2)).id,
        provider="Northwind Parts",
        offer_id="var_nvme_1tb",
        amount_usd=Decimal("142.00"),
        rationale="lowest landed price",
        options_considered=["Northwind Parts:var_nvme_1tb"],
    )
    assert (intent.state, intent.need_id, intent.attempt) == (IntentState.ALLOWED, "need_1", 2)
    assert (intent.quantity, intent.currency) == (1, "USD")
    assert [c.rule for c in decision.checks] == AGENTIC_ORDER
    proposed = ledger.events(types=[EventType.PURCHASE_PROPOSED])[0]
    assert proposed.refs == {"quote": "lq_1", "need": "need_1"}
    assert proposed.payload["need_id"] == "need_1"
    assert (proposed.payload["quantity"], proposed.payload["currency"]) == (1, "USD")
    decided = ledger.events(types=[EventType.POLICY_DECIDED])[0].payload["checks"]
    assert isinstance(decided, list)
    assert [c["rule"] for c in decided if isinstance(c, dict)] == AGENTIC_ORDER


def test_a_lease_proposal_records_what_it_always_did(
    gate: AuthorityGate, repos: Repositories, ledger: Ledger
) -> None:
    """The card path's proposal event carries no need, quantity or currency."""
    _propose(gate, _quote(repos))
    proposed = ledger.events(types=[EventType.PURCHASE_PROPOSED])[0]
    assert proposed.refs == {"quote": "quo_1"}
    assert set(proposed.payload) == {
        "provider",
        "offer_id",
        "amount_usd",
        "rationale",
        "options_considered",
    }


def test_a_catalogue_claim_happens_exactly_once(
    part_gate: AuthorityGate, part_repos: Repositories, ledger: Ledger
) -> None:
    """One claim, one key from its ledger event; a replayed claim sends nothing new."""
    intent = _propose_part(part_gate, _landed(part_repos))
    first = part_gate.claim(intent.id)
    second = part_gate.claim(intent.id)
    claims = ledger.events(types=[EventType.PURCHASE_CLAIMED])
    assert (first.claimed, second.claimed) == (True, False)
    assert len(claims) == 1
    assert first.intent.idempotency_key == f"{intent.id}:{claims[0].event_id}"
    assert second.intent.idempotency_key == first.intent.idempotency_key


def test_two_allowed_intents_at_one_need_cannot_both_be_claimed(
    part_gate: AuthorityGate, part_repos: Repositories
) -> None:
    """The second claim at the same need is refused at apply time: one order per need."""
    first = _propose_part(part_gate, _landed(part_repos))
    second = _propose_part(part_gate, _landed(part_repos, make_landed_quote(quote_id="lq_2")))
    assert first.state == second.state == IntentState.ALLOWED
    assert part_gate.claim(first.id).claimed
    late = part_gate.claim(second.id)
    assert not late.claimed
    assert late.decision is not None
    assert late.decision.rule == "need.not_already_ordered"
    assert late.intent.state == IntentState.REFUSED


def test_outcome_unknown_counts_blocks_the_need_and_is_never_retried(
    part_gate: AuthorityGate, part_repos: Repositories
) -> None:
    """Money that may have moved counts; no claim and no new attempt follow it."""
    intent_id = _claimed(part_gate, part_repos)
    unknown = part_gate.record_outcome_unknown(intent_id, error="ReadTimeout after send")
    assert unknown.state == IntentState.OUTCOME_UNKNOWN
    assert part_gate.runway_usd("obj_1") == Decimal(358)
    assert not part_gate.claim(intent_id).claimed

    retry = _propose_part(part_gate, _landed(part_repos, make_landed_quote(quote_id="lq_2")))
    assert retry.state == IntentState.REFUSED
    decision = part_repos.decisions.require(retry.decision_id or "")[0]
    assert decision.rule == "attempts.retry_limit"

    other_need = make_landed_quote(quote_id="lq_3", need_id="need_2")
    assert _propose_part(part_gate, _landed(part_repos, other_need)).state == IntentState.ALLOWED


def test_outcome_unknown_is_reconciled_by_reading_the_checkout(
    part_gate: AuthorityGate, part_repos: Repositories
) -> None:
    """Reconciliation records what Reap says happened, keeping the first checkout id."""
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_outcome_unknown(intent_id, error="ReadTimeout")
    done = part_gate.record_completed(intent_id, _completed("142.00"))
    assert (done.state, done.checkout_id, done.order_id) == (
        IntentState.COMPLETED,
        CHECKOUT_ID,
        "ORD-1001",
    )
    order = part_repos.orders.require(CHECKOUT_ID)[0]
    assert (order.status, order.simulated) == (CheckoutStatus.COMPLETED, None)


def test_a_created_checkout_keeps_counting_while_processing(
    part_gate: AuthorityGate, part_repos: Repositories, ledger: Ledger
) -> None:
    """The checkout is recorded against its intent and quote; the claim still counts."""
    intent_id = _claimed(part_gate, part_repos)
    created = part_gate.record_checkout_created(
        intent_id, _created(), simulated=True, replayed=False
    )
    assert (created.state, created.checkout_id) == (IntentState.EXECUTING, CHECKOUT_ID)
    assert part_gate.runway_usd("obj_1") == Decimal(358)
    event = ledger.events(types=[EventType.CHECKOUT_CREATED])[0]
    assert event.subject_id == CHECKOUT_ID
    assert event.refs == {"intent": intent_id, "quote": "lq_1"}
    assert event.payload == {
        "reap_quote_id": "5f0c2a4e-8d1b-4f6a-9c3e-2b7d1e0a9f41",
        "status": "PROCESSING",
        "amount": {"amount": "142", "currency": "USD"},
        "next_action": False,
        "simulated": True,
        "replayed": False,
        "idempotency_key": created.idempotency_key,
    }
    order = part_repos.orders.require(CHECKOUT_ID)[0]
    assert (order.intent_id, order.quote_id, order.status) == (
        intent_id,
        "lq_1",
        CheckoutStatus.PROCESSING,
    )
    assert order.quoted.amount == Decimal("142.00")
    assert order.simulated is True


def test_awaiting_approval_counts_against_the_runway(
    part_gate: AuthorityGate, part_repos: Repositories, ledger: Ledger
) -> None:
    """A checkout waiting on Reap's hosted page may yet charge, so it counts."""
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_checkout_created(
        intent_id, _created(CheckoutStatus.REQUIRES_ACTION), simulated=False
    )
    waiting = part_gate.record_awaiting_approval(
        intent_id,
        _checkout(
            CheckoutStatus.REQUIRES_ACTION, approval_url="https://pay.reap.global/approve/abc?t=1"
        ),
    )
    assert waiting.state == IntentState.AWAITING_APPROVAL
    assert waiting.committed_usd == Decimal("142.00")
    assert part_gate.runway_usd("obj_1") == Decimal(358)
    event = ledger.events(types=[EventType.CHECKOUT_AWAITING_APPROVAL])[0]
    assert (event.subject_id, event.refs) == (CHECKOUT_ID, {"intent": intent_id})
    assert event.payload == {
        "approval_host": "pay.reap.global",
        "expires_at": "2037-10-09T08:57:03Z",
    }
    order = part_repos.orders.require(CHECKOUT_ID)[0]
    assert (order.status, order.approval_host) == (
        CheckoutStatus.REQUIRES_ACTION,
        "pay.reap.global",
    )

    done = part_gate.record_completed(intent_id, _completed())
    assert done.state == IntentState.COMPLETED


def test_completion_replaces_the_quoted_amount_with_the_final_one(
    part_gate: AuthorityGate, part_repos: Repositories, ledger: Ledger
) -> None:
    """The order's finalAmount is what the runway counts, and a difference is recorded."""
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_checkout_created(intent_id, _created(), simulated=True)
    done = part_gate.record_completed(intent_id, _completed("139.50"))
    assert (done.state, done.order_id, done.final_amount_usd) == (
        IntentState.COMPLETED,
        "ORD-1001",
        Decimal("139.50"),
    )
    assert done.committed_usd == Decimal("139.50")
    assert part_gate.runway_usd("obj_1") == Decimal("360.50")

    completed = ledger.events(types=[EventType.CHECKOUT_COMPLETED])[0]
    assert (completed.subject_id, completed.refs) == (
        "ORD-1001",
        {"checkout": CHECKOUT_ID, "intent": intent_id},
    )
    assert completed.payload == {
        "order_id": "ORD-1001",
        "checkout_id": CHECKOUT_ID,
        "final_amount": {"amount": "139.50", "currency": "USD"},
    }
    mismatch = ledger.events(types=[EventType.ORDER_AMOUNT_MISMATCH])[0]
    assert (mismatch.subject_id, mismatch.refs) == (
        CHECKOUT_ID,
        {"intent": intent_id, "order": "ORD-1001"},
    )
    assert mismatch.payload == {
        "quoted": {"amount": "142.00", "currency": "USD"},
        "final": {"amount": "139.50", "currency": "USD"},
        "difference": "-2.50",
    }
    order = part_repos.orders.require(CHECKOUT_ID)[0]
    assert order.final_amount is not None
    assert (order.status, order.order_id, order.final_amount.amount) == (
        CheckoutStatus.COMPLETED,
        "ORD-1001",
        Decimal("139.50"),
    )


def test_a_final_amount_above_the_quote_is_recorded_as_charged(
    part_gate: AuthorityGate, part_repos: Repositories
) -> None:
    """Money already moved is counted as charged, never capped to the quote."""
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_checkout_created(intent_id, _created(), simulated=True)
    assert part_gate.record_completed(intent_id, _completed("145.00")).committed_usd == Decimal(145)
    assert part_gate.runway_usd("obj_1") == Decimal(355)


def test_a_completion_at_the_quoted_amount_records_no_mismatch(
    part_gate: AuthorityGate, part_repos: Repositories, ledger: Ledger
) -> None:
    """When Reap charges what it quoted, only the completion lands on the ledger."""
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_checkout_created(intent_id, _created(), simulated=True)
    part_gate.record_completed(intent_id, _completed("142"))
    assert ledger.events(types=[EventType.ORDER_AMOUNT_MISMATCH]) == []
    assert part_gate.runway_usd("obj_1") == Decimal(358)


def test_a_final_amount_in_another_currency_keeps_the_quoted_figure(
    part_gate: AuthorityGate, part_repos: Repositories, ledger: Ledger
) -> None:
    """A charge the budget cannot convert keeps counting at the quote and is flagged."""
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_checkout_created(intent_id, _created(), simulated=True)
    done = part_gate.record_completed(
        intent_id,
        _checkout(CheckoutStatus.COMPLETED, order_id="ORD-1", final="190", currency="SGD"),
    )
    assert (done.final_amount_usd, done.committed_usd) == (None, Decimal("142.00"))
    mismatch = ledger.events(types=[EventType.ORDER_AMOUNT_MISMATCH])[0]
    assert mismatch.payload["difference"] is None


@pytest.mark.parametrize(
    "case",
    [
        (CheckoutStatus.FAILED, IntentState.FAILED, EventType.CHECKOUT_FAILED),
        (CheckoutStatus.EXPIRED, IntentState.EXPIRED, EventType.CHECKOUT_EXPIRED),
    ],
)
def test_a_failed_or_expired_checkout_counts_nothing(
    part_gate: AuthorityGate,
    part_repos: Repositories,
    ledger: Ledger,
    case: tuple[CheckoutStatus, IntentState, EventType],
) -> None:
    """Both need a fresh quote and a fresh checkout; neither charged anything."""
    status, state, event = case
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_checkout_created(
        intent_id, _created(CheckoutStatus.REQUIRES_ACTION), simulated=False
    )
    part_gate.record_awaiting_approval(
        intent_id,
        _checkout(CheckoutStatus.REQUIRES_ACTION, approval_url="https://pay.reap.global/x"),
    )
    ended = part_gate.record_checkout_failed(intent_id, _checkout(status))
    assert (ended.state, ended.decline_code) == (state, status.value)
    assert part_gate.runway_usd("obj_1") == Decimal(500)
    recorded = ledger.events(types=[event])[0]
    assert (recorded.subject_id, recorded.refs, recorded.payload) == (
        CHECKOUT_ID,
        {"intent": intent_id},
        {"status": status.value},
    )
    assert part_repos.orders.require(CHECKOUT_ID)[0].status == status


def test_the_retry_limit_counts_failed_expired_and_declined_attempts_per_need(
    part_gate: AuthorityGate, part_repos: Repositories
) -> None:
    """Three attempts ended badly at one need; the fourth is refused, another need is not."""
    first = _claimed(part_gate, part_repos, make_landed_quote(quote_id="lq_a1", attempt=1))
    part_gate.record_failed(
        first, code="CHECKOUT_TEMPORARILY_UNAVAILABLE", error="503, Retry-After 2"
    )
    second = _claimed(part_gate, part_repos, make_landed_quote(quote_id="lq_a2", attempt=2))
    part_gate.record_checkout_created(second, _created(checkout_id="c-2"), simulated=True)
    part_gate.record_checkout_failed(second, _checkout(CheckoutStatus.EXPIRED, checkout_id="c-2"))
    third = _claimed(part_gate, part_repos, make_landed_quote(quote_id="lq_a3", attempt=3))
    part_gate.record_checkout_created(third, _created(checkout_id="c-3"), simulated=True)
    part_gate.record_checkout_failed(third, _checkout(CheckoutStatus.FAILED, checkout_id="c-3"))
    assert part_gate.runway_usd("obj_1") == Decimal(500)

    fourth = _propose_part(part_gate, _landed(part_repos, make_landed_quote(quote_id="lq_a4")))
    assert fourth.state == IntentState.REFUSED
    decision = part_repos.decisions.require(fourth.decision_id or "")[0]
    assert (decision.rule, decision.reason) == (
        "attempts.retry_limit",
        "3 of 3 attempts at need_1 have failed",
    )
    elsewhere = make_landed_quote(quote_id="lq_b1", need_id="need_2")
    assert _propose_part(part_gate, _landed(part_repos, elsewhere)).state == IntentState.ALLOWED


def test_a_completed_need_refuses_a_second_order(
    part_gate: AuthorityGate, part_repos: Repositories
) -> None:
    """Once the part is ordered, the same fault buys nothing more."""
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_checkout_created(intent_id, _created(), simulated=True)
    part_gate.record_completed(intent_id, _completed())
    again = _propose_part(part_gate, _landed(part_repos, make_landed_quote(quote_id="lq_2")))
    decision = part_repos.decisions.require(again.decision_id or "")[0]
    assert (again.state, decision.rule) == (IntentState.REFUSED, "need.not_already_ordered")
    assert decision.reason == f"need_1 already has {intent_id} completed (order ORD-1001)"


def test_the_apply_time_recheck_reads_the_quote_margin_and_the_enrolment(
    part_gate: AuthorityGate, part_repos: Repositories, clock: ManualClock
) -> None:
    """A quote about to lapse, or an enrolment revoked since, refuses at claim time."""
    near = _propose_part(part_gate, _landed(part_repos))
    clock.advance(seconds=105)
    late = part_gate.claim(near.id)
    assert late.decision is not None
    assert (late.claimed, late.decision.rule) == (False, "quote.not_expired")

    fresh = _propose_part(
        part_gate, _landed(part_repos, make_landed_quote(quote_id="lq_2", created_at=clock()))
    )
    objective, version = part_repos.objectives.require("obj_1")
    revoked = objective.model_copy(
        update={"enrolment": make_enrolment(status=EnrollmentStatus.REVOKED)}
    )
    part_repos.objectives.update(revoked, record_id="obj_1", expected_version=version)
    refused = part_gate.claim(fresh.id)
    assert refused.decision is not None
    assert (refused.claimed, refused.decision.rule) == (False, "enrolment.active")


def test_without_a_need_judge_every_catalogue_purchase_is_refused(
    part_repos: Repositories, ledger: Ledger, clock: ManualClock
) -> None:
    """The gate fails closed when nothing can say whether the item fills the need."""
    gate = AuthorityGate(part_repos, ledger, clock)
    gate.issue_grant(make_part_grant())
    intent = _propose_part(gate, _landed(part_repos))
    decision = part_repos.decisions.require(intent.decision_id or "")[0]
    assert (intent.state, decision.rule) == (IntentState.REFUSED, "item.matches_need")


def test_the_escalated_landed_price_waits_for_the_operator(
    part_repos: Repositories, ledger: Ledger, clock: ManualClock
) -> None:
    """Above the threshold the intent is held; approved, it claims like any other."""
    gate = AuthorityGate(part_repos, ledger, clock, need_judge=_matches)
    gate.issue_grant(make_part_grant(approval="120"))
    held = _propose_part(gate, _landed(part_repos))
    assert held.state == IntentState.ESCALATED
    gate.approve(held.id, "operator")
    assert gate.claim(held.id).claimed


def test_checkout_steps_follow_the_state_machine(
    part_gate: AuthorityGate, part_repos: Repositories
) -> None:
    """Each step refuses an intent in the wrong state, the wrong checkout or status."""
    unclaimed = _propose_part(part_gate, _landed(part_repos, make_landed_quote(quote_id="lq_0")))
    with pytest.raises(InvalidTransitionError):
        part_gate.record_checkout_created(unclaimed.id, _created(), simulated=True)

    intent_id = _claimed(part_gate, part_repos, make_landed_quote(need_id="need_9"))
    with pytest.raises(ValueError, match="REQUIRES_ACTION"):
        part_gate.record_awaiting_approval(intent_id, _checkout(CheckoutStatus.PROCESSING))
    with pytest.raises(ValueError, match="approval"):
        part_gate.record_awaiting_approval(intent_id, _checkout(CheckoutStatus.REQUIRES_ACTION))
    with pytest.raises(ValueError, match="COMPLETED"):
        part_gate.record_completed(intent_id, _checkout(CheckoutStatus.PROCESSING))
    with pytest.raises(ValueError, match="order id"):
        part_gate.record_completed(intent_id, _checkout(CheckoutStatus.COMPLETED, final="1"))
    with pytest.raises(ValueError, match="final amount"):
        part_gate.record_completed(intent_id, _checkout(CheckoutStatus.COMPLETED, order_id="O"))
    with pytest.raises(ValueError, match="FAILED or EXPIRED"):
        part_gate.record_checkout_failed(intent_id, _checkout(CheckoutStatus.COMPLETED))

    part_gate.record_checkout_created(intent_id, _created(), simulated=True)
    with pytest.raises(ValueError, match="not the intent's checkout"):
        part_gate.record_completed(intent_id, _completed().model_copy(update={"id": "other"}))
    with pytest.raises(InvalidTransitionError, match="has a checkout"):
        part_gate.record_failed(intent_id, code="X", error="a checkout exists")
    part_gate.record_completed(intent_id, _completed())
    with pytest.raises(InvalidTransitionError):
        part_gate.record_checkout_failed(intent_id, _checkout(CheckoutStatus.FAILED))
    with pytest.raises(InvalidTransitionError):
        part_gate.record_outcome_unknown(intent_id, error="late")


def test_a_lease_has_no_checkout(gate: AuthorityGate, repos: Repositories) -> None:
    """The card path's intents cannot be given an agentic checkout."""
    lease = _propose(gate, _quote(repos))
    gate.claim(lease.id)
    with pytest.raises(InvalidTransitionError, match="not a catalogue purchase"):
        gate.record_checkout_created(lease.id, _created(), simulated=True)
    with pytest.raises(InvalidTransitionError, match="not a catalogue purchase"):
        gate.record_completed(lease.id, _completed())


def test_a_replayed_create_is_recorded_against_the_same_checkout(
    part_gate: AuthorityGate, part_repos: Repositories, ledger: Ledger
) -> None:
    """A same-key replay that returns the checkout settles it; it never opens a second."""
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_checkout_created(intent_id, _created(), simulated=True)
    again = part_gate.record_checkout_created(intent_id, _created(), simulated=True, replayed=True)
    assert again.checkout_id == CHECKOUT_ID
    assert [e.payload["replayed"] for e in ledger.events(types=[EventType.CHECKOUT_CREATED])] == [
        None,
        True,
    ]
    with pytest.raises(ValueError, match="not the intent's checkout"):
        part_gate.record_checkout_created(
            intent_id, _created(checkout_id="another"), simulated=True
        )


def _runway_from_ledger(events: list[LedgerEvent], budget: Decimal) -> Decimal:
    """Recompute the runway from the ledger alone, as the dashboard's fold must.

    Args:
        events: The objective's events, oldest first.
        budget: The objective's budget.

    Returns:
        Budget less every intent's committed amount, by the one domain rule.
    """
    state: dict[str, IntentState] = {}
    amount: dict[str, Decimal] = {}
    final: dict[str, Decimal] = {}
    for event in events:
        intent = event.refs.get("intent", event.subject_id)
        match event.type:
            case EventType.PURCHASE_CLAIMED:
                state[intent] = IntentState.EXECUTING
                amount[intent] = Decimal(str(event.payload["amount_usd"]))
            case EventType.PURCHASE_OUTCOME_UNKNOWN:
                state[intent] = IntentState.OUTCOME_UNKNOWN
            case EventType.PURCHASE_FAILED:
                state[intent] = IntentState.FAILED
            case EventType.CHECKOUT_AWAITING_APPROVAL:
                state[intent] = IntentState.AWAITING_APPROVAL
            case EventType.CHECKOUT_COMPLETED:
                state[intent] = IntentState.COMPLETED
                charged = event.payload["final_amount"]
                assert isinstance(charged, dict)
                if charged["currency"] == "USD":
                    final[intent] = Decimal(str(charged["amount"]))
            case EventType.CHECKOUT_FAILED:
                state[intent] = IntentState.FAILED
            case EventType.CHECKOUT_EXPIRED:
                state[intent] = IntentState.EXPIRED
            case _:
                pass
    committed = sum(
        (committed_amount(s, amount[i], final=final.get(i)) for i, s in state.items()),
        Decimal(0),
    )
    return budget - committed


def test_the_runway_equals_the_figure_the_ledger_alone_gives(
    part_gate: AuthorityGate, part_repos: Repositories, ledger: Ledger
) -> None:
    """Every state's contribution is recoverable from the ledger, so the dashboard can match."""

    def need(n: int) -> LandedQuote:
        return make_landed_quote(quote_id=f"lq_{n}", need_id=f"need_{n}", final=f"{10 + n}.00")

    executing = _claimed(part_gate, part_repos, need(1))
    awaiting = _claimed(part_gate, part_repos, need(2))
    part_gate.record_checkout_created(
        awaiting, _created(CheckoutStatus.REQUIRES_ACTION, checkout_id="c2"), simulated=False
    )
    part_gate.record_awaiting_approval(
        awaiting,
        _checkout(CheckoutStatus.REQUIRES_ACTION, checkout_id="c2", approval_url="https://r/x"),
    )
    unknown = _claimed(part_gate, part_repos, need(3))
    part_gate.record_outcome_unknown(unknown, error="ReadTimeout")
    completed = _claimed(part_gate, part_repos, need(4))
    part_gate.record_checkout_created(completed, _created(checkout_id="c4"), simulated=True)
    part_gate.record_completed(
        completed,
        _checkout(CheckoutStatus.COMPLETED, checkout_id="c4", order_id="O4", final="13.25"),
    )
    failed = _claimed(part_gate, part_repos, need(5))
    part_gate.record_failed(failed, code="CHECKOUT_TEMPORARILY_UNAVAILABLE", error="503")
    expired = _claimed(part_gate, part_repos, need(6))
    part_gate.record_checkout_created(expired, _created(checkout_id="c6"), simulated=True)
    part_gate.record_checkout_failed(expired, _checkout(CheckoutStatus.EXPIRED, checkout_id="c6"))
    refused = _propose_part(part_gate, _landed(part_repos, need(7)), amount_usd=Decimal("1.00"))
    assert refused.state == IntentState.REFUSED
    assert executing

    expected = Decimal(500) - Decimal(11) - Decimal(12) - Decimal(13) - Decimal("13.25")
    assert part_gate.runway_usd("obj_1") == expected
    assert _runway_from_ledger(ledger.events(objective_id="obj_1"), Decimal(500)) == expected


def test_daily_spend_counts_a_completed_checkout_at_its_final_amount(
    part_gate: AuthorityGate, part_repos: Repositories, clock: ManualClock
) -> None:
    """The daily window uses the same committed figure as the runway."""
    intent_id = _claimed(part_gate, part_repos)
    part_gate.record_checkout_created(intent_id, _created(), simulated=True)
    part_gate.record_completed(intent_id, _completed("100.00"))
    assert part_gate.committed_on_day("obj_1", clock(), exclude=None) == Decimal(100)
    tomorrow = clock() + timedelta(days=1)
    assert part_gate.committed_on_day("obj_1", tomorrow, exclude=None) == Decimal(0)

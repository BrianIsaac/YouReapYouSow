"""The exact-action gate: hold, decide, claim, and record what happened.

It works in four moves:

* A proposed purchase is held as an intent and decided by pure rules into ``allow``,
  ``escalate`` (held for the operator) or ``refuse``, and the decision names the rule.
* Immediately before money moves, ``claim`` re-runs the same rules. Authority may have
  been revoked, its TTL may have lapsed, or another purchase may have used the runway;
  any of those refuses at apply time with its own decision record.
* The claim is the exactly-once fence: a compare-and-set from ``allowed`` to
  ``executing`` in the same transaction as the ledger event whose id seeds the
  idempotency key. A second caller loses and gets the recorded intent back.
* ``outcome_unknown`` is terminal for automation. It still counts against the budget
  and is resolved by reconciliation against Reap, never by an automatic retry; on the
  agentic path it also blocks its need (``attempts.retry_limit``).

On the agentic path a claimed intent's checkout is recorded step by step:
``record_checkout_created`` (still ``executing``), ``record_awaiting_approval``
(Reap's ``REQUIRES_ACTION``), then ``record_completed`` with the order id and the final
amount, or ``record_checkout_failed`` for ``FAILED`` and ``EXPIRED``. Each step reads
the wire model as Reap sent it, keeps the ``Order`` record current and appends its
``checkout.*`` event. Committed spend counts ``executing``, ``awaiting_approval`` and
``outcome_unknown`` at the quoted ``finalAmount`` and ``completed`` at the checkout's
``finalAmount``; failed, expired and refused intents count nothing. The card path's
``record_authorised``, ``record_declined``, ``record_settled`` and
``authorise_external`` stay, dormant.

Every state change and its ledger events commit in one transaction.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import JsonValue

from youreapyousow.authority.grants import AuthorityGrant, validate_grant
from youreapyousow.authority.idempotency import purchase_idempotency_key
from youreapyousow.authority.rules import GateContext, NeedJudge, evaluate
from youreapyousow.clock import Clock, utc_now
from youreapyousow.domain import (
    BUDGET_CURRENCY,
    COMMITTED_STATES,
    DecisionPhase,
    Disposition,
    IntentState,
    LandedQuote,
    Order,
    PolicyDecision,
    Price,
    PurchaseIntent,
    Quote,
    usd,
)
from youreapyousow.ids import new_id
from youreapyousow.ledger.events import EventType
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.merchants import merchant_for
from youreapyousow.reap.models import Checkout, CheckoutCreated, CheckoutStatus
from youreapyousow.repos import Repositories


class InvalidTransitionError(RuntimeError):
    """Raised when an intent is asked to move to a state its current state forbids."""


@dataclass(frozen=True)
class ClaimResult:
    """The outcome of trying to claim an intent for execution.

    Attributes:
        intent: The intent as it now stands.
        claimed: True only for the one caller that won the claim and may send money.
        decision: The apply-time decision, when the rules were re-run.
    """

    intent: PurchaseIntent
    claimed: bool
    decision: PolicyDecision | None


@dataclass(frozen=True)
class ExternalAuthorisation:
    """The gate's answer to Reap's real-time authorisation request.

    Attributes:
        approve: Whether to approve.
        reason: Why.
        intent_id: The claimed intent the request matched, if any.
    """

    approve: bool
    reason: str
    intent_id: str | None = None


_DISPOSITION_STATE = {
    Disposition.ALLOW: IntentState.ALLOWED,
    Disposition.ESCALATE: IntentState.ESCALATED,
    Disposition.REFUSE: IntentState.REFUSED,
}

_CHECKOUT_OPEN = frozenset(
    {IntentState.EXECUTING, IntentState.AWAITING_APPROVAL, IntentState.OUTCOME_UNKNOWN}
)
"""States from which a checkout may still be recorded as approved, completed or ended."""

_CHECKOUT_ENDED_STATE = {
    CheckoutStatus.FAILED: (IntentState.FAILED, EventType.CHECKOUT_FAILED),
    CheckoutStatus.EXPIRED: (IntentState.EXPIRED, EventType.CHECKOUT_EXPIRED),
}


def _price(price: Price) -> dict[str, JsonValue]:
    return {"amount": str(price.amount), "currency": price.currency}


def _decision_payload(decision: PolicyDecision) -> dict[str, JsonValue]:
    return {
        "phase": decision.phase.value,
        "disposition": decision.disposition.value,
        "rule": decision.rule,
        "reason": decision.reason,
        "runway_before_usd": str(decision.runway_before_usd),
        "runway_after_usd": str(decision.runway_after_usd),
        "checks": [
            {"rule": c.rule, "passed": c.passed, "detail": c.detail} for c in decision.checks
        ],
    }


class AuthorityGate:
    """Deterministic authority between the agent's intent and Reap's money."""

    def __init__(
        self,
        repos: Repositories,
        ledger: Ledger,
        clock: Clock = utc_now,
        *,
        need_judge: NeedJudge | None = None,
    ) -> None:
        """Wire the gate to its storage.

        Args:
            repos: Record repositories.
            ledger: The append-only ledger.
            clock: Time source.
            need_judge: Judges whether a landed quote fills its need (rule 10); without
                one every catalogue purchase is refused.
        """
        self._repos = repos
        self._ledger = ledger
        self._clock = clock
        self._need_judge = need_judge

    # Grants

    def issue_grant(self, grant: AuthorityGrant) -> AuthorityGrant:
        """Validate and record a grant of authority for an objective.

        Args:
            grant: The grant.

        Returns:
            The stored grant.
        """
        objective, _ = self._repos.objectives.require(grant.objective_id)
        validate_grant(grant, objective.budget_usd)
        with self._repos.db.transaction():
            self._repos.grants.insert(grant, record_id=grant.id, objective_id=grant.objective_id)
            self._ledger.append(
                EventType.GRANT_ISSUED,
                subject_id=grant.id,
                objective_id=grant.objective_id,
                payload=grant.model_dump(mode="json"),
            )
        return grant

    def revoke_grant(self, grant_id: str, reason: str) -> AuthorityGrant:
        """Revoke a grant; any purchase not yet claimed will be refused.

        Args:
            grant_id: The grant.
            reason: Why it was revoked.

        Returns:
            The revoked grant.
        """
        with self._repos.db.transaction():
            grant, version = self._repos.grants.require(grant_id)
            if grant.revoked_at is not None:
                return grant
            revoked = grant.model_copy(update={"revoked_at": self._clock()})
            self._repos.grants.update(revoked, record_id=grant_id, expected_version=version)
            self._ledger.append(
                EventType.GRANT_REVOKED,
                subject_id=grant_id,
                objective_id=grant.objective_id,
                payload={"reason": reason},
            )
        return revoked

    def current_grant(self, objective_id: str) -> AuthorityGrant | None:
        """Return the most recently issued grant for an objective.

        Args:
            objective_id: The objective.

        Returns:
            The latest grant, revoked or not, or None.
        """
        grants = self._repos.grants.list(objective_id=objective_id)
        return grants[-1] if grants else None

    # Budget

    def committed_usd(self, objective_id: str, *, exclude: str | None = None) -> Decimal:
        """Sum the spend committed against an objective.

        Args:
            objective_id: The objective.
            exclude: An intent to leave out, typically the one being decided.

        Returns:
            Committed spend in USD.
        """
        return sum(
            (
                i.committed_usd
                for i in self._repos.intents.list(objective_id=objective_id)
                if i.id != exclude
            ),
            Decimal(0),
        )

    def committed_on_day(self, objective_id: str, day: datetime, *, exclude: str | None) -> Decimal:
        """Sum the spend claimed on one UTC calendar day, as Reap's daily window counts it.

        Args:
            objective_id: The objective.
            day: Any time on the day in question.
            exclude: An intent to leave out.

        Returns:
            Committed spend claimed that day.
        """
        return sum(
            (
                i.committed_usd
                for i in self._repos.intents.list(objective_id=objective_id)
                if i.id != exclude
                and i.state in COMMITTED_STATES
                and i.claimed_at is not None
                and i.claimed_at.date() == day.date()
            ),
            Decimal(0),
        )

    def runway_usd(self, objective_id: str) -> Decimal:
        """Return the budget left for an objective; authoritative over Reap's counters.

        Args:
            objective_id: The objective.

        Returns:
            Budget minus committed spend.
        """
        objective, _ = self._repos.objectives.require(objective_id)
        return objective.budget_usd - self.committed_usd(objective_id)

    def _quote(self, intent: PurchaseIntent) -> Quote | LandedQuote | None:
        found = (
            self._repos.landed_quotes.get(intent.quote_id)
            if intent.is_agentic
            else self._repos.quotes.get(intent.quote_id)
        )
        return found[0] if found else None

    def _need_intents(self, intent: PurchaseIntent) -> tuple[PurchaseIntent, ...]:
        if intent.need_id is None:
            return ()
        return tuple(
            other
            for other in self._repos.intents.list(objective_id=intent.objective_id)
            if other.need_id == intent.need_id and other.id != intent.id
        )

    def _context(self, intent: PurchaseIntent) -> GateContext:
        now = self._clock()
        objective = self._repos.objectives.get(intent.objective_id)
        return GateContext(
            intent=intent,
            quote=self._quote(intent),
            objective=objective[0] if objective else None,
            grant=self.current_grant(intent.objective_id),
            committed_usd=self.committed_usd(intent.objective_id, exclude=intent.id),
            committed_today_usd=self.committed_on_day(intent.objective_id, now, exclude=intent.id),
            now=now,
            need_judge=self._need_judge,
            need_intents=self._need_intents(intent),
        )

    def _decide(self, intent: PurchaseIntent, phase: DecisionPhase) -> PolicyDecision:
        ctx = self._context(intent)
        decision = evaluate(ctx, phase=phase, decision_id=new_id("dec"))
        self._repos.decisions.insert(
            decision, record_id=decision.id, objective_id=decision.objective_id
        )
        refs = {"intent": intent.id, "quote": intent.quote_id, "decision": decision.id}
        if ctx.grant is not None:
            refs["grant"] = ctx.grant.id
        self._ledger.append(
            EventType.POLICY_DECIDED,
            subject_id=decision.id,
            objective_id=intent.objective_id,
            refs=refs,
            payload=_decision_payload(decision),
        )
        return decision

    # The gate

    def propose(
        self,
        *,
        objective_id: str,
        quote_id: str,
        provider: str,
        offer_id: str,
        amount_usd: Decimal,
        rationale: str,
        options_considered: list[str],
        quantity: int = 1,
        currency: str = BUDGET_CURRENCY,
        on_behalf_of: str | None = None,
    ) -> tuple[PurchaseIntent, PolicyDecision]:
        """Hold a proposed purchase and decide it.

        A quote id naming a landed catalogue quote makes the intent agentic: its need
        and attempt are copied from the quote, never taken from the caller.

        Args:
            objective_id: The objective it serves.
            quote_id: The quote it would buy: a lease or a landed catalogue quote.
            provider: Provider or merchant name, which must match the quote.
            offer_id: Offer or variant id, which must match the quote.
            amount_usd: Amount (a catalogue quote's ``finalAmount``), which must match.
            rationale: The agent's reason for choosing it.
            options_considered: Keys of every offer or variant the agent compared.
            quantity: How many, which must match the quote.
            currency: The amount's currency, which must match the quote.
            on_behalf_of: Who the purchase is for, named on the ledger; never decided on.

        Returns:
            The intent in its decided state, and the decision.
        """
        landed = self._repos.landed_quotes.get(quote_id)
        intent = PurchaseIntent(
            id=new_id("int"),
            objective_id=objective_id,
            quote_id=quote_id,
            provider=provider,
            offer_id=offer_id,
            amount_usd=amount_usd,
            rationale=rationale,
            options_considered=options_considered,
            created_at=self._clock(),
            quantity=quantity,
            currency=currency,
            need_id=landed[0].need_id if landed else None,
            attempt=landed[0].attempt if landed else None,
        )
        refs = {"quote": quote_id}
        payload: dict[str, JsonValue] = {
            "provider": provider,
            "offer_id": offer_id,
            "amount_usd": str(amount_usd),
            "rationale": rationale,
            "options_considered": list(options_considered),
        }
        if on_behalf_of is not None:
            payload["on_behalf_of"] = on_behalf_of
        if intent.need_id is not None:
            refs["need"] = intent.need_id
            payload |= {
                "need_id": intent.need_id,
                "attempt": intent.attempt,
                "quantity": quantity,
                "currency": currency,
            }
        with self._repos.db.transaction():
            self._repos.intents.insert(intent, record_id=intent.id, objective_id=objective_id)
            self._ledger.append(
                EventType.PURCHASE_PROPOSED,
                subject_id=intent.id,
                objective_id=objective_id,
                refs=refs,
                payload=payload,
            )
            decision = self._decide(intent, DecisionPhase.PROPOSAL)
            state = _DISPOSITION_STATE[decision.disposition]
            decided = intent.model_copy(update={"state": state, "decision_id": decision.id})
            self._repos.intents.update(decided, record_id=intent.id, expected_version=1)
        return decided, decision

    def approve(self, intent_id: str, approver: str) -> PurchaseIntent:
        """Record the operator's approval of an escalated intent.

        Approval lifts only the approval threshold; every other rule runs again at
        claim time.

        Args:
            intent_id: The escalated intent.
            approver: Who approved it.

        Returns:
            The intent, now allowed.
        """
        return self._operator_verdict(intent_id, approver, approved=True, reason="approved")

    def reject(self, intent_id: str, approver: str, reason: str) -> PurchaseIntent:
        """Record the operator's rejection of an escalated intent.

        Args:
            intent_id: The escalated intent.
            approver: Who rejected it.
            reason: Why.

        Returns:
            The intent, now refused.
        """
        return self._operator_verdict(intent_id, approver, approved=False, reason=reason)

    def _operator_verdict(
        self, intent_id: str, approver: str, *, approved: bool, reason: str
    ) -> PurchaseIntent:
        with self._repos.db.transaction():
            intent, version = self._repos.intents.require(intent_id)
            if intent.state != IntentState.ESCALATED:
                raise InvalidTransitionError(f"intent {intent_id} is {intent.state}, not escalated")
            updated = intent.model_copy(
                update={
                    "state": IntentState.ALLOWED if approved else IntentState.REFUSED,
                    "approved_by": approver if approved else None,
                }
            )
            self._repos.intents.update(updated, record_id=intent_id, expected_version=version)
            self._ledger.append(
                EventType.PURCHASE_APPROVED if approved else EventType.PURCHASE_REJECTED,
                subject_id=intent_id,
                objective_id=intent.objective_id,
                payload={"by": approver, "reason": reason},
            )
        return updated

    def claim(self, intent_id: str) -> ClaimResult:
        """Re-check an allowed intent and, if it still passes, claim it for execution.

        Args:
            intent_id: The intent.

        Returns:
            Whether this caller won the claim, the intent, and the apply-time decision.
        """
        with self._repos.db.transaction():
            intent, version = self._repos.intents.require(intent_id)
            if intent.state != IntentState.ALLOWED:
                return ClaimResult(intent=intent, claimed=False, decision=None)
            decision = self._decide(intent, DecisionPhase.APPLY)
            if decision.disposition != Disposition.ALLOW:
                held = intent.model_copy(
                    update={
                        "state": _DISPOSITION_STATE[decision.disposition],
                        "decision_id": decision.id,
                    }
                )
                self._repos.intents.update(held, record_id=intent_id, expected_version=version)
                return ClaimResult(intent=held, claimed=False, decision=decision)
            event = self._ledger.append(
                EventType.PURCHASE_CLAIMED,
                subject_id=intent_id,
                objective_id=intent.objective_id,
                refs={"decision": decision.id, "quote": intent.quote_id},
                payload={"amount_usd": str(intent.amount_usd)},
            )
            claimed = intent.model_copy(
                update={
                    "state": IntentState.EXECUTING,
                    "decision_id": decision.id,
                    "claim_event_id": event.event_id,
                    "idempotency_key": purchase_idempotency_key(intent_id, event.event_id),
                    "claimed_at": decision.decided_at,
                }
            )
            self._repos.intents.update(claimed, record_id=intent_id, expected_version=version)
        return ClaimResult(intent=claimed, claimed=True, decision=decision)

    # Outcomes

    def _transition(
        self,
        intent_id: str,
        *,
        allowed_from: frozenset[IntentState],
        event: EventType,
        subject_id: str | None = None,
        refs: dict[str, str] | None = None,
        payload: dict[str, JsonValue],
        check: Callable[[PurchaseIntent], None] | None = None,
        **changes: object,
    ) -> PurchaseIntent:
        with self._repos.db.transaction():
            intent, version = self._repos.intents.require(intent_id)
            if intent.state not in allowed_from:
                raise InvalidTransitionError(
                    f"intent {intent_id} is {intent.state}; cannot record {event.value}"
                )
            if check is not None:
                check(intent)
            updated = intent.model_copy(update=changes)
            self._repos.intents.update(updated, record_id=intent_id, expected_version=version)
            self._ledger.append(
                event,
                subject_id=subject_id or intent_id,
                objective_id=intent.objective_id,
                refs=refs,
                payload=payload,
            )
        return updated

    def record_authorised(self, intent_id: str, *, reap_transaction_id: str) -> PurchaseIntent:
        """Record that Reap authorised the purchase (card path, dormant).

        Args:
            intent_id: The executing intent.
            reap_transaction_id: Reap's transaction id, the provenance join key.

        Returns:
            The authorised intent.
        """
        return self._transition(
            intent_id,
            allowed_from=frozenset({IntentState.EXECUTING, IntentState.OUTCOME_UNKNOWN}),
            event=EventType.REAP_AUTHORISED,
            subject_id=reap_transaction_id,
            refs={"intent": intent_id},
            payload={},
            state=IntentState.AUTHORISED,
            reap_transaction_id=reap_transaction_id,
            executed_at=self._clock(),
        )

    def record_declined(
        self, intent_id: str, *, reap_transaction_id: str, code: str, policy_name: str | None
    ) -> PurchaseIntent:
        """Record that Reap declined the purchase; nothing is committed (card path, dormant).

        Args:
            intent_id: The executing intent.
            reap_transaction_id: Reap's transaction id.
            code: Reap's decline code.
            policy_name: The Reap policy that fired, when a policy did.

        Returns:
            The declined intent.
        """
        return self._transition(
            intent_id,
            allowed_from=frozenset({IntentState.EXECUTING, IntentState.OUTCOME_UNKNOWN}),
            event=EventType.REAP_DECLINED,
            subject_id=reap_transaction_id,
            refs={"intent": intent_id},
            payload={"code": code, "policy": policy_name},
            state=IntentState.DECLINED,
            reap_transaction_id=reap_transaction_id,
            decline_code=code,
            executed_at=self._clock(),
        )

    def record_outcome_unknown(self, intent_id: str, *, error: str) -> PurchaseIntent:
        """Record that the Reap call may or may not have landed.

        The intent keeps counting against the budget and is not retried automatically.

        Args:
            intent_id: The executing intent.
            error: What went wrong in transport.

        Returns:
            The intent, now outcome unknown.
        """
        return self._transition(
            intent_id,
            allowed_from=frozenset({IntentState.EXECUTING}),
            event=EventType.PURCHASE_OUTCOME_UNKNOWN,
            payload={"error": error},
            state=IntentState.OUTCOME_UNKNOWN,
        )

    def record_failed(self, intent_id: str, *, code: str, error: str) -> PurchaseIntent:
        """Record a purchase that certainly moved no money.

        Used when the request never reached Reap, or Reap rejected it outright
        (for example insufficient funds, or ``CHECKOUT_TEMPORARILY_UNAVAILABLE``) without
        creating a transaction or a checkout. Once a checkout exists, its own status
        decides through ``record_checkout_failed``.

        Args:
            intent_id: The executing intent.
            code: A short failure code, such as Reap's ``error.code``.
            error: What happened.

        Returns:
            The failed intent, which no longer counts against the budget.
        """

        def no_checkout(intent: PurchaseIntent) -> None:
            if intent.checkout_id is not None:
                raise InvalidTransitionError(
                    f"intent {intent_id} has a checkout {intent.checkout_id}; its status decides"
                )

        return self._transition(
            intent_id,
            allowed_from=frozenset({IntentState.EXECUTING}),
            event=EventType.PURCHASE_FAILED,
            payload={"code": code, "error": error},
            check=no_checkout,
            state=IntentState.FAILED,
            decline_code=code,
        )

    # Agentic checkout outcomes

    def _checkout_intent(
        self, intent_id: str, checkout_id: str, *, allowed_from: frozenset[IntentState]
    ) -> tuple[PurchaseIntent, int, tuple[Order, int] | None]:
        """Load an agentic intent for a checkout step, inside the caller's transaction.

        Args:
            intent_id: The intent.
            checkout_id: The checkout Reap returned.
            allowed_from: The states the step may start from.

        Returns:
            The intent, its version, and the checkout's order record if there is one.

        Raises:
            InvalidTransitionError: If the intent is not a catalogue purchase or its state
                forbids the step.
            ValueError: If the intent already belongs to a different checkout.
        """
        intent, version = self._repos.intents.require(intent_id)
        if not intent.is_agentic:
            raise InvalidTransitionError(f"intent {intent_id} is not a catalogue purchase")
        if intent.state not in allowed_from:
            raise InvalidTransitionError(
                f"intent {intent_id} is {intent.state}; cannot record checkout {checkout_id}"
            )
        if intent.checkout_id is not None and intent.checkout_id != checkout_id:
            raise ValueError(
                f"checkout {checkout_id} is not the intent's checkout {intent.checkout_id}"
            )
        return intent, version, self._repos.orders.get(checkout_id)

    def _save_order(
        self,
        intent: PurchaseIntent,
        checkout_id: str,
        existing: tuple[Order, int] | None,
        **changes: object,
    ) -> None:
        """Create or update the checkout's order record.

        Args:
            intent: The intent the checkout executes.
            checkout_id: Reap's checkout id, the record's id.
            existing: The current record and version, if any.
            **changes: Fields to set.
        """
        now = self._clock()
        if existing is None:
            order = Order.model_validate(
                {
                    "checkout_id": checkout_id,
                    "intent_id": intent.id,
                    "objective_id": intent.objective_id,
                    "quote_id": intent.quote_id,
                    "quoted": Price(amount=intent.amount_usd, currency=intent.currency),
                    "created_at": now,
                    "updated_at": now,
                }
                | changes
            )
            self._repos.orders.insert(
                order, record_id=checkout_id, objective_id=intent.objective_id
            )
            return
        current, version = existing
        updated = current.model_copy(update={**changes, "updated_at": now})
        self._repos.orders.update(updated, record_id=checkout_id, expected_version=version)

    def record_checkout_created(
        self,
        intent_id: str,
        created: CheckoutCreated,
        *,
        simulated: bool,
        replayed: bool | None = None,
        card_spend: bool = False,
    ) -> PurchaseIntent:
        """Record the checkout Reap opened for a claimed intent.

        The intent stays ``executing`` and keeps counting at the quoted amount; a
        same-key replay that returns the same checkout is recorded again, never as a
        second checkout. It also settles an ``outcome_unknown`` attempt whose replay
        returned the checkout.

        Args:
            intent_id: The executing intent.
            created: The ``POST /agentic/checkouts`` response.
            simulated: Whether ``X-Simulate-Checkout: COMPLETED`` was sent.
            replayed: Whether Reap marked the response ``Idempotent-Replayed``, if known.
            card_spend: Whether the backend pays it as a simulated spend on the
                participant's own card (Kwal); recorded only when True.

        Returns:
            The intent, now bound to the checkout.
        """
        with self._repos.db.transaction():
            intent, version, order = self._checkout_intent(
                intent_id,
                created.id,
                allowed_from=frozenset({IntentState.EXECUTING, IntentState.OUTCOME_UNKNOWN}),
            )
            updated = intent.model_copy(
                update={"state": IntentState.EXECUTING, "checkout_id": created.id}
            )
            self._repos.intents.update(updated, record_id=intent_id, expected_version=version)
            self._save_order(
                intent,
                created.id,
                order,
                status=created.status,
                amount=Price.from_reap(created.amount) if created.amount else None,
                simulated=simulated,
            )
            payload: dict[str, JsonValue] = {
                "reap_quote_id": created.quote_id,
                "status": created.status.value,
                "amount": _price(Price.from_reap(created.amount)) if created.amount else None,
                "next_action": created.next_action is not None,
                "simulated": simulated,
                "replayed": replayed,
                "idempotency_key": intent.idempotency_key,
            }
            if card_spend:
                payload["card_spend"] = True
            self._ledger.append(
                EventType.CHECKOUT_CREATED,
                subject_id=created.id,
                objective_id=intent.objective_id,
                refs={"intent": intent_id, "quote": intent.quote_id},
                payload=payload,
            )
        return updated

    def record_awaiting_approval(self, intent_id: str, checkout: Checkout) -> PurchaseIntent:
        """Record that the checkout waits on Reap's hosted approval page.

        The intent keeps counting against the budget: the charge may still run.

        Args:
            intent_id: The intent whose checkout was read.
            checkout: The ``GET /agentic/checkouts/{id}`` response, ``REQUIRES_ACTION``.

        Returns:
            The intent, now awaiting approval.

        Raises:
            ValueError: If the checkout is not ``REQUIRES_ACTION`` with an approval page.
        """
        if checkout.status != CheckoutStatus.REQUIRES_ACTION:
            raise ValueError(f"checkout {checkout.id} is {checkout.status}, not REQUIRES_ACTION")
        action = checkout.next_action
        if action is None:
            raise ValueError(f"checkout {checkout.id} names no approval page")
        host = urlsplit(action.url).hostname
        with self._repos.db.transaction():
            intent, version, order = self._checkout_intent(
                intent_id,
                checkout.id,
                allowed_from=frozenset({IntentState.EXECUTING, IntentState.OUTCOME_UNKNOWN}),
            )
            updated = intent.model_copy(
                update={"state": IntentState.AWAITING_APPROVAL, "checkout_id": checkout.id}
            )
            self._repos.intents.update(updated, record_id=intent_id, expected_version=version)
            self._save_order(
                intent,
                checkout.id,
                order,
                status=checkout.status,
                approval_host=host,
                approval_expires_at=action.expires_at,
            )
            self._ledger.append(
                EventType.CHECKOUT_AWAITING_APPROVAL,
                subject_id=checkout.id,
                objective_id=intent.objective_id,
                refs={"intent": intent_id},
                payload={"approval_host": host, "expires_at": action.expires_at},
            )
        return updated

    def record_completed(self, intent_id: str, checkout: Checkout) -> PurchaseIntent:
        """Record a completed checkout: the merchant's order and the amount charged.

        The checkout's ``finalAmount`` replaces the quoted amount in every budget figure,
        whether below or above it, since the money has moved; any difference is recorded
        as ``order.amount_mismatch``. A charge in a currency other than the intent's
        cannot be converted, so the quoted amount keeps counting and the mismatch says so.

        Args:
            intent_id: The intent whose checkout was read.
            checkout: The ``GET /agentic/checkouts/{id}`` response, ``COMPLETED``.

        Returns:
            The completed intent.

        Raises:
            ValueError: If the checkout is not ``COMPLETED`` with an order id and a final
                amount.
        """
        if checkout.status != CheckoutStatus.COMPLETED:
            raise ValueError(f"checkout {checkout.id} is {checkout.status}, not COMPLETED")
        if checkout.order_id is None:
            raise ValueError(f"completed checkout {checkout.id} carries no order id")
        if checkout.final_amount is None:
            raise ValueError(f"completed checkout {checkout.id} carries no final amount")
        order_id, final = checkout.order_id, Price.from_reap(checkout.final_amount)
        with self._repos.db.transaction():
            intent, version, order = self._checkout_intent(
                intent_id, checkout.id, allowed_from=_CHECKOUT_OPEN
            )
            quoted = Price(amount=intent.amount_usd, currency=intent.currency)
            same_currency = final.currency == quoted.currency
            updated = intent.model_copy(
                update={
                    "state": IntentState.COMPLETED,
                    "checkout_id": checkout.id,
                    "order_id": order_id,
                    "final_amount_usd": final.amount if same_currency else None,
                    "executed_at": self._clock(),
                }
            )
            self._repos.intents.update(updated, record_id=intent_id, expected_version=version)
            self._save_order(
                intent,
                checkout.id,
                order,
                status=checkout.status,
                order_id=order_id,
                final_amount=final,
            )
            self._ledger.append(
                EventType.CHECKOUT_COMPLETED,
                subject_id=order_id,
                objective_id=intent.objective_id,
                refs={"checkout": checkout.id, "intent": intent_id},
                payload={
                    "order_id": order_id,
                    "checkout_id": checkout.id,
                    "final_amount": _price(final),
                },
            )
            if not same_currency or final.amount != quoted.amount:
                self._ledger.append(
                    EventType.ORDER_AMOUNT_MISMATCH,
                    subject_id=checkout.id,
                    objective_id=intent.objective_id,
                    refs={"intent": intent_id, "order": order_id},
                    payload={
                        "quoted": _price(quoted),
                        "final": _price(final),
                        "difference": str(final.amount - quoted.amount) if same_currency else None,
                    },
                )
        return updated

    def record_checkout_failed(self, intent_id: str, checkout: Checkout) -> PurchaseIntent:
        """Record a checkout that ended ``FAILED`` or ``EXPIRED``; nothing was charged.

        Either needs a fresh quote and a fresh claim, and either uses up one of the
        need's attempts (``attempts.retry_limit``).

        Args:
            intent_id: The intent whose checkout was read.
            checkout: The ``GET /agentic/checkouts/{id}`` response.

        Returns:
            The failed or expired intent, which no longer counts against the budget.

        Raises:
            ValueError: If the checkout is neither ``FAILED`` nor ``EXPIRED``.
        """
        ended = _CHECKOUT_ENDED_STATE.get(checkout.status)
        if ended is None:
            raise ValueError(f"checkout {checkout.id} is {checkout.status}, not FAILED or EXPIRED")
        state, event = ended
        with self._repos.db.transaction():
            intent, version, order = self._checkout_intent(
                intent_id, checkout.id, allowed_from=_CHECKOUT_OPEN
            )
            updated = intent.model_copy(
                update={
                    "state": state,
                    "checkout_id": checkout.id,
                    "decline_code": checkout.status.value,
                    "executed_at": self._clock(),
                }
            )
            self._repos.intents.update(updated, record_id=intent_id, expected_version=version)
            self._save_order(intent, checkout.id, order, status=checkout.status)
            self._ledger.append(
                event,
                subject_id=checkout.id,
                objective_id=intent.objective_id,
                refs={"intent": intent_id},
                payload={"status": checkout.status.value},
            )
        return updated

    def record_settled(self, intent_id: str, *, amount_usd: Decimal) -> PurchaseIntent:
        """Record the cleared amount; runway is then measured on what cleared (card path).

        Args:
            intent_id: The authorised intent.
            amount_usd: Amount cleared, at most the authorised amount.

        Returns:
            The settled intent.

        Raises:
            ValueError: If the amount is negative or above the authorisation.
        """
        intent, _ = self._repos.intents.require(intent_id)
        amount = usd(amount_usd)
        if amount < 0 or amount > intent.amount_usd:
            raise ValueError(f"settled amount {amount} outside 0..{intent.amount_usd}")
        return self._transition(
            intent_id,
            allowed_from=frozenset({IntentState.AUTHORISED}),
            event=EventType.REAP_SETTLED,
            subject_id=intent.reap_transaction_id,
            refs={"intent": intent_id},
            payload={"authorised_usd": str(intent.amount_usd), "settled_usd": str(amount)},
            state=IntentState.SETTLED,
            settled_usd=amount,
        )

    def authorise_external(
        self, *, card_id: str, amount_usd: Decimal, merchant_name: str
    ) -> ExternalAuthorisation:
        """Answer Reap's real-time authorisation request for an objective's card (dormant).

        Approves only a charge that matches exactly a purchase this gate has already
        claimed: same card, same amount, same merchant. Anything else is declined.

        Args:
            card_id: The card being charged.
            amount_usd: The amount Reap is asking about.
            merchant_name: The merchant on the request.

        Returns:
            The decision and, when approved, the matching intent.
        """
        objectives = [
            o for o in self._repos.objectives.list() if o.reap and o.reap.card_id == card_id
        ]
        if not objectives:
            return ExternalAuthorisation(False, f"card {card_id} belongs to no objective")
        for intent in self._repos.intents.list(objective_id=objectives[0].id):
            if (
                intent.state == IntentState.EXECUTING
                and intent.amount_usd == amount_usd
                and merchant_for(intent.provider).name == merchant_name
            ):
                return ExternalAuthorisation(True, "matches a claimed purchase", intent.id)
        return ExternalAuthorisation(
            False, f"no claimed purchase of {amount_usd} at {merchant_name} on this card"
        )

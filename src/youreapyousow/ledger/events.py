"""Typed ledger events.

The agentic purchase runs objective, need, search, details, variant, quote, intent,
decision, claim, checkout, order and outcome, each event naming its subject and linking
its causes through ``refs`` so the provenance graph can walk it:

========================== ======================= =====================================
Event                      Subject                 Refs
========================== ======================= =====================================
reap.enrolled              enrolment id            objective
fault.detected             fault id                (the objective)
part.mapped                part key                fault
need.raised                need id                 the breach observation, or fault
catalogue.searched         Reap search id          need
catalogue.detailed         details id              search
catalogue.variant_resolved Reap variant id         product_details
quote.landed               landed quote id         variant, need
checkout.created           Reap checkout id        intent, quote
checkout.awaiting_approval Reap checkout id        intent
checkout.completed         merchant order id       checkout, intent
checkout.failed / expired  Reap checkout id        intent
order.amount_mismatch      Reap checkout id        intent, order
ticket.opened              ticket id               order, fault
========================== ======================= =====================================

The ``checkout.*`` and ``order.amount_mismatch`` events are appended by the authority
gate's record methods, in the same transaction as the intent's state change.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue

GENESIS_HASH = "0" * 64


class EventType(StrEnum):
    """Every step of the control loop that lands on the ledger."""

    OBJECTIVE_CREATED = "objective.created"
    OBJECTIVE_COMPLETED = "objective.completed"
    GRANT_ISSUED = "grant.issued"
    GRANT_REVOKED = "grant.revoked"
    REAP_CARD_ISSUED = "reap.card_issued"
    REAP_POLICY_ATTACHED = "reap.policy_attached"
    REAP_CARD_FROZEN = "reap.card_frozen"
    MARKET_DISCOVERED = "market.discovered"
    QUOTE_CREATED = "quote.created"
    PURCHASE_PROPOSED = "purchase.proposed"
    POLICY_DECIDED = "policy.decided"
    PURCHASE_APPROVED = "purchase.approved"
    PURCHASE_REJECTED = "purchase.rejected"
    PURCHASE_CLAIMED = "purchase.claimed"
    PURCHASE_OUTCOME_UNKNOWN = "purchase.outcome_unknown"
    PURCHASE_FAILED = "purchase.failed"
    REAP_AUTHORISED = "reap.authorised"
    REAP_DECLINED = "reap.declined"
    REAP_SETTLED = "reap.settled"
    REAP_WEBHOOK_RECEIVED = "reap.webhook_received"
    DEPLOYMENT_PROVISIONED = "deployment.provisioned"
    OBSERVATION_RECORDED = "observation.recorded"
    LIFECYCLE_DECIDED = "lifecycle.decided"
    DEPLOYMENT_RELEASED = "deployment.released"
    # The agentic purchase: enrolment, need, catalogue, quote, checkout.
    REAP_ENROLLED = "reap.enrolled"
    NEED_RAISED = "need.raised"
    CATALOGUE_SEARCHED = "catalogue.searched"
    CATALOGUE_DETAILED = "catalogue.detailed"
    CATALOGUE_VARIANT_RESOLVED = "catalogue.variant_resolved"
    QUOTE_LANDED = "quote.landed"
    CHECKOUT_CREATED = "checkout.created"
    CHECKOUT_AWAITING_APPROVAL = "checkout.awaiting_approval"
    CHECKOUT_COMPLETED = "checkout.completed"
    CHECKOUT_FAILED = "checkout.failed"
    CHECKOUT_EXPIRED = "checkout.expired"
    ORDER_AMOUNT_MISMATCH = "order.amount_mismatch"
    TICKET_OPENED = "ticket.opened"
    # Shape (b): the machine that buys its own fix.
    FAULT_DETECTED = "fault.detected"
    PART_MAPPED = "part.mapped"


class LedgerEvent(BaseModel):
    """One immutable entry on the ledger.

    Attributes:
        seq: Monotonic position on the ledger, assigned on append.
        event_id: Unique identifier of this event.
        type: What happened.
        at: When it happened, in UTC.
        objective_id: The objective this event serves, if any.
        subject_id: The record the event is about.
        refs: Typed links to other records, keyed by kind (``quote``, ``intent``,
            ``decision``, ``reap_transaction``, ``deployment``...), from which the
            provenance graph is drawn.
        payload: Event-specific detail.
        prev_hash: Hash of the previous event, chaining the ledger.
        hash: Hash of this event's content and ``prev_hash``.
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    event_id: str
    type: EventType
    at: datetime
    objective_id: str | None
    subject_id: str
    refs: dict[str, str]
    payload: dict[str, JsonValue]
    prev_hash: str
    hash: str

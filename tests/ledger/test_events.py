"""Tests for the ledger's event vocabulary."""

from youreapyousow.ledger.events import EventType

AGENTIC_EVENTS = {
    "REAP_ENROLLED": "reap.enrolled",
    "NEED_RAISED": "need.raised",
    "CATALOGUE_SEARCHED": "catalogue.searched",
    "CATALOGUE_DETAILED": "catalogue.detailed",
    "CATALOGUE_VARIANT_RESOLVED": "catalogue.variant_resolved",
    "QUOTE_LANDED": "quote.landed",
    "CHECKOUT_CREATED": "checkout.created",
    "CHECKOUT_AWAITING_APPROVAL": "checkout.awaiting_approval",
    "CHECKOUT_COMPLETED": "checkout.completed",
    "CHECKOUT_FAILED": "checkout.failed",
    "CHECKOUT_EXPIRED": "checkout.expired",
    "ORDER_AMOUNT_MISMATCH": "order.amount_mismatch",
    "TICKET_OPENED": "ticket.opened",
    "FAULT_DETECTED": "fault.detected",
    "PART_MAPPED": "part.mapped",
}


def test_the_agentic_purchase_events_carry_their_wire_names() -> None:
    """Every event of the agentic purchase exists under its exact wire name."""
    assert {name: EventType[name].value for name in AGENTIC_EVENTS} == AGENTIC_EVENTS


def test_event_names_are_unique() -> None:
    """No two event types share a wire name, so a stored event reads back unambiguously."""
    values = [e.value for e in EventType]
    assert len(values) == len(set(values))

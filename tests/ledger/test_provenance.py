"""Tests for the provenance graph drawn from ledger refs."""

from youreapyousow.clock import ManualClock
from youreapyousow.ledger.events import EventType
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.ledger.provenance import SUBJECT_KIND, build_graph


def _loop(ledger: Ledger, clock: ManualClock) -> None:
    obj = "obj_1"
    ledger.append(EventType.OBJECTIVE_CREATED, subject_id=obj, objective_id=obj)
    ledger.append(EventType.GRANT_ISSUED, subject_id="grt", objective_id=obj)
    ledger.append(
        EventType.MARKET_DISCOVERED,
        subject_id="dsc",
        objective_id=obj,
        payload={"options": [{"key": "vast:1"}, {"key": "runpod:a"}, "not-a-dict"]},
    )
    ledger.append(
        EventType.QUOTE_CREATED,
        subject_id="quo",
        objective_id=obj,
        refs={"offer": "vast:1", "discovery": "dsc"},
    )
    ledger.append(
        EventType.PURCHASE_PROPOSED, subject_id="int", objective_id=obj, refs={"quote": "quo"}
    )
    ledger.append(
        EventType.POLICY_DECIDED,
        subject_id="dec",
        objective_id=obj,
        refs={"intent": "int", "quote": "quo", "grant": "grt"},
    )
    clock.advance(seconds=3)
    ledger.append(
        EventType.REAP_AUTHORISED, subject_id="tx", objective_id=obj, refs={"intent": "int"}
    )
    ledger.append(
        EventType.DEPLOYMENT_PROVISIONED,
        subject_id="dep",
        objective_id=obj,
        refs={"intent": "int", "reap_transaction": "tx", "decision": "dec"},
    )
    ledger.append(
        EventType.OBSERVATION_RECORDED,
        subject_id="obs",
        objective_id=obj,
        refs={"deployment": "dep"},
    )
    ledger.append(EventType.DEPLOYMENT_RELEASED, subject_id="dep", objective_id=obj)


def test_graph_links_cause_to_effect(ledger: Ledger, clock: ManualClock) -> None:
    """Every ref becomes an edge from the referenced record to the event's subject."""
    _loop(ledger, clock)
    graph = build_graph(ledger.events(objective_id="obj_1"))
    kinds = {n.id: n.kind for n in graph.nodes.values()}
    assert kinds["vast:1"] == "offer"
    assert kinds["tx"] == "reap_transaction"
    assert kinds["dep"] == "deployment"
    relations = {(e.source, e.target) for e in graph.edges}
    assert ("dsc", "vast:1") in relations
    assert ("dsc", "runpod:a") in relations
    assert ("vast:1", "quo") in relations
    assert ("int", "tx") in relations
    assert ("dep", "obs") in relations
    assert ("obj_1", "grt") in relations
    deployment = graph.nodes["dep"]
    assert [kind for _, kind in deployment.events] == [
        "deployment.provisioned",
        "deployment.released",
    ]


def test_lineage_answers_why_we_are_paying(ledger: Ledger, clock: ManualClock) -> None:
    """Walking back from the deployment reaches every cause up to the objective."""
    _loop(ledger, clock)
    graph = build_graph(ledger.events(objective_id="obj_1"))
    lineage = [n.id for n in graph.lineage("dep")]
    assert set(lineage[:3]) == {"int", "tx", "dec"}
    assert {"quo", "vast:1", "dsc", "grt", "obj_1"} <= set(lineage)
    assert "runpod:a" not in lineage
    assert "obs" not in lineage


def test_graph_serialises_for_the_dashboard(ledger: Ledger, clock: ManualClock) -> None:
    """The JSON form lists nodes and edges."""
    _loop(ledger, clock)
    data = build_graph(ledger.events()).to_json()
    nodes, edges = data["nodes"], data["edges"]
    assert isinstance(nodes, list)
    assert isinstance(edges, list)
    assert len(edges) >= 10
    assert {"source": "int", "target": "tx", "relation": "reap.authorised"} in edges


def test_every_event_type_has_a_subject_kind() -> None:
    """A new event type cannot reach the ledger without a node kind for the graph."""
    assert set(SUBJECT_KIND) == set(EventType)


def _part_order(ledger: Ledger) -> None:
    obj = "obj_1"
    ledger.append(EventType.OBJECTIVE_CREATED, subject_id=obj, objective_id=obj)
    ledger.append(
        EventType.REAP_ENROLLED, subject_id="enr", objective_id=obj, refs={"objective": obj}
    )
    ledger.append(EventType.FAULT_DETECTED, subject_id="flt", objective_id=obj)
    ledger.append(
        EventType.PART_MAPPED, subject_id="nvme-1tb", objective_id=obj, refs={"fault": "flt"}
    )
    ledger.append(
        EventType.NEED_RAISED,
        subject_id="need",
        objective_id=obj,
        refs={"fault": "flt", "part": "nvme-1tb"},
    )
    ledger.append(
        EventType.CATALOGUE_SEARCHED, subject_id="srch", objective_id=obj, refs={"need": "need"}
    )
    ledger.append(
        EventType.CATALOGUE_DETAILED, subject_id="det", objective_id=obj, refs={"search": "srch"}
    )
    ledger.append(
        EventType.CATALOGUE_VARIANT_RESOLVED,
        subject_id="var",
        objective_id=obj,
        refs={"product_details": "det"},
    )
    ledger.append(
        EventType.QUOTE_LANDED,
        subject_id="lq",
        objective_id=obj,
        refs={"variant": "var", "need": "need"},
    )
    ledger.append(
        EventType.PURCHASE_PROPOSED, subject_id="int", objective_id=obj, refs={"quote": "lq"}
    )
    ledger.append(
        EventType.CHECKOUT_CREATED,
        subject_id="chk",
        objective_id=obj,
        refs={"intent": "int", "quote": "lq"},
    )
    ledger.append(
        EventType.CHECKOUT_AWAITING_APPROVAL,
        subject_id="chk",
        objective_id=obj,
        refs={"intent": "int"},
    )
    ledger.append(
        EventType.CHECKOUT_COMPLETED,
        subject_id="ord",
        objective_id=obj,
        refs={"checkout": "chk", "intent": "int"},
    )
    ledger.append(
        EventType.ORDER_AMOUNT_MISMATCH,
        subject_id="chk",
        objective_id=obj,
        refs={"intent": "int", "order": "ord"},
    )
    ledger.append(
        EventType.TICKET_OPENED,
        subject_id="tkt",
        objective_id=obj,
        refs={"order": "ord", "fault": "flt"},
    )


def test_a_ticket_traces_back_through_the_order_to_the_fault(ledger: Ledger) -> None:
    """Asking why a part was ordered walks order, checkout, quote, catalogue, need, fault."""
    _part_order(ledger)
    graph = build_graph(ledger.events(objective_id="obj_1"))
    kinds = {n.id: n.kind for n in graph.nodes.values()}
    assert kinds == {
        "obj_1": "objective",
        "enr": "enrolment",
        "flt": "fault",
        "nvme-1tb": "part",
        "need": "need",
        "srch": "search",
        "det": "product_details",
        "var": "variant",
        "lq": "quote",
        "int": "intent",
        "chk": "checkout",
        "ord": "order",
        "tkt": "ticket",
    }
    lineage = {n.id for n in graph.lineage("tkt")}
    assert lineage == {
        "ord",
        "chk",
        "int",
        "lq",
        "var",
        "det",
        "srch",
        "need",
        "nvme-1tb",
        "flt",
        "obj_1",
    }
    assert [kind for _, kind in graph.nodes["chk"].events] == [
        "checkout.created",
        "checkout.awaiting_approval",
        "order.amount_mismatch",
    ]

"""The economic decision graph, drawn from the ledger alone.

Each event names a subject record and links to the records it came from through typed
``refs``. Every subject becomes a node and every ref an edge from cause to effect, so
the graph is exactly what the ledger recorded, with nothing inferred. Asking "why are
we paying this provider?" is then a walk backwards from the deployment:
deployment, then Reap transaction, intent and decision, then quote, offer and
discovery, then objective. On the agentic path the same walk runs from a ticket or a
deployment through the order, checkout, intent, landed quote, variant, details, search
and need, back to the fault or breach that raised it.
"""

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import JsonValue

from youreapyousow.ledger.events import EventType, LedgerEvent

SUBJECT_KIND: dict[EventType, str] = {
    EventType.OBJECTIVE_CREATED: "objective",
    EventType.OBJECTIVE_COMPLETED: "objective",
    EventType.GRANT_ISSUED: "grant",
    EventType.GRANT_REVOKED: "grant",
    EventType.REAP_CARD_ISSUED: "card",
    EventType.REAP_POLICY_ATTACHED: "reap_policy",
    EventType.REAP_CARD_FROZEN: "card",
    EventType.MARKET_DISCOVERED: "discovery",
    EventType.QUOTE_CREATED: "quote",
    EventType.PURCHASE_PROPOSED: "intent",
    EventType.POLICY_DECIDED: "decision",
    EventType.PURCHASE_APPROVED: "intent",
    EventType.PURCHASE_REJECTED: "intent",
    EventType.PURCHASE_CLAIMED: "intent",
    EventType.PURCHASE_OUTCOME_UNKNOWN: "intent",
    EventType.PURCHASE_FAILED: "intent",
    EventType.REAP_AUTHORISED: "reap_transaction",
    EventType.REAP_DECLINED: "reap_transaction",
    EventType.REAP_SETTLED: "reap_transaction",
    EventType.REAP_WEBHOOK_RECEIVED: "webhook",
    EventType.DEPLOYMENT_PROVISIONED: "deployment",
    EventType.OBSERVATION_RECORDED: "observation",
    EventType.LIFECYCLE_DECIDED: "lifecycle_decision",
    EventType.DEPLOYMENT_RELEASED: "deployment",
    EventType.REAP_ENROLLED: "enrolment",
    EventType.NEED_RAISED: "need",
    EventType.CATALOGUE_SEARCHED: "search",
    EventType.CATALOGUE_DETAILED: "product_details",
    EventType.CATALOGUE_VARIANT_RESOLVED: "variant",
    EventType.QUOTE_LANDED: "quote",
    EventType.CHECKOUT_CREATED: "checkout",
    EventType.CHECKOUT_AWAITING_APPROVAL: "checkout",
    EventType.CHECKOUT_COMPLETED: "order",
    EventType.CHECKOUT_FAILED: "checkout",
    EventType.CHECKOUT_EXPIRED: "checkout",
    EventType.ORDER_AMOUNT_MISMATCH: "checkout",
    EventType.TICKET_OPENED: "ticket",
    EventType.FAULT_DETECTED: "fault",
    EventType.PART_MAPPED: "part",
}


@dataclass
class Node:
    """A record in the graph.

    Attributes:
        id: The record's identifier.
        kind: What sort of record it is.
        events: Ledger sequence numbers and types that touched it, in order.
        first_at: When it first appeared.
        detail: Payloads of the events about it, keyed by event type.
    """

    id: str
    kind: str
    events: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    first_at: datetime | None = None
    detail: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True)
class Edge:
    """A causal link from one record to another.

    Attributes:
        source: The cause.
        target: The effect.
        relation: The event type that recorded the link.
    """

    source: str
    target: str
    relation: str


@dataclass
class ProvenanceGraph:
    """Nodes and edges for one objective."""

    nodes: dict[str, Node] = field(default_factory=dict[str, Node])
    edges: list[Edge] = field(default_factory=list[Edge])

    def add_node(self, node_id: str, kind: str) -> Node:
        """Return the node for a record, creating it on first sight.

        Args:
            node_id: The record.
            kind: Its kind, used only when creating.

        Returns:
            The node.
        """
        node = self.nodes.get(node_id)
        if node is None:
            node = self.nodes[node_id] = Node(node_id, kind)
        return node

    def add_edge(self, source: str, target: str, relation: str) -> None:
        """Link a cause to an effect once; self-links are ignored.

        Args:
            source: The cause.
            target: The effect.
            relation: The event type recording the link.
        """
        edge = Edge(source, target, relation)
        if source != target and edge not in self.edges:
            self.edges.append(edge)

    def lineage(self, node_id: str) -> list[Node]:
        """Walk every cause of a record back to the objective.

        Args:
            node_id: The record to explain.

        Returns:
            The record's ancestors, nearest first, each once.
        """
        parents: dict[str, list[str]] = {}
        for edge in self.edges:
            parents.setdefault(edge.target, []).append(edge.source)
        seen: list[str] = []
        frontier = list(parents.get(node_id, []))
        while frontier:
            current = frontier.pop(0)
            if current in seen or current == node_id:
                continue
            seen.append(current)
            frontier.extend(parents.get(current, []))
        return [self.nodes[n] for n in seen]

    def to_json(self) -> dict[str, JsonValue]:
        """Serialise for the dashboard.

        Returns:
            ``{"nodes": [...], "edges": [...]}``.
        """
        return {
            "nodes": [
                {
                    "id": n.id,
                    "kind": n.kind,
                    "first_at": n.first_at.isoformat() if n.first_at else None,
                    "events": [[seq, kind] for seq, kind in n.events],
                    "detail": n.detail,
                }
                for n in self.nodes.values()
            ],
            "edges": [
                {"source": e.source, "target": e.target, "relation": e.relation} for e in self.edges
            ],
        }


def build_graph(events: list[LedgerEvent]) -> ProvenanceGraph:
    """Draw the graph from events in ledger order.

    Args:
        events: An objective's events, oldest first.

    Returns:
        The provenance graph.
    """
    graph = ProvenanceGraph()
    for event in events:
        subject = graph.add_node(event.subject_id, SUBJECT_KIND[event.type])
        subject.events.append((event.seq, event.type.value))
        subject.first_at = subject.first_at or event.at
        subject.detail[event.type.value] = event.payload
        for kind, ref_id in event.refs.items():
            graph.add_node(ref_id, kind)
            graph.add_edge(ref_id, event.subject_id, event.type.value)
        if event.type == EventType.MARKET_DISCOVERED:
            options = event.payload.get("options")
            for option in options if isinstance(options, list) else []:
                if isinstance(option, dict) and isinstance(key := option.get("key"), str):
                    offer = graph.add_node(key, "offer")
                    offer.detail["offer"] = option
                    graph.add_edge(event.subject_id, key, "market.considered")
        if not event.refs and event.objective_id and event.objective_id != event.subject_id:
            graph.add_node(event.objective_id, "objective")
            graph.add_edge(event.objective_id, event.subject_id, event.type.value)
    return graph

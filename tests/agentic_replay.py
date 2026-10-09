"""Both product shapes run on the agentic mock, for the dashboard and report tests.

The control plane is wired as ``tests/test_control.py`` wires it, and every Reap call
goes through the real client to the in-process agentic mock, so the ledger these stories
leave is the one the real loop writes. The steps before and after the purchase that the
agent loop owns (the readings, the fault and its part, the technician's ticket, the
capacity an order backs) are appended here as plain ledger events in the shapes
``agent/fulfil.py`` writes them.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic import SecretStr

from youreapyousow import purchase as scenario
from youreapyousow.authority.gate import AuthorityGate
from youreapyousow.authority.lifecycle import LifecycleRules
from youreapyousow.clock import ManualClock
from youreapyousow.control import (
    AgenticSettings,
    ControlPlane,
    Operator,
    PurchasePath,
    mock_card_entry,
    need_judge_for,
)
from youreapyousow.domain import (
    Comparator,
    Constraint,
    Disposition,
    IntentState,
    LandedQuote,
    ObjectiveKind,
    PurchaseIntent,
)
from youreapyousow.ids import new_id
from youreapyousow.ledger.events import EventType
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.market.provisioning import MockProvisioner
from youreapyousow.market.service import MarketMode, MarketService
from youreapyousow.procure.need import AttributeMatch, CatalogueSearch, Need, NeedSpec
from youreapyousow.procure.selection import best_quote
from youreapyousow.reap.client import MOCK_BASE_URL, ReapHttpClient
from youreapyousow.reap.mock.agentic import AgenticMockEngine, Catalogue, bundled_catalogue
from youreapyousow.reap.mock.engine import MockReapEngine
from youreapyousow.reap.mock.server import create_mock_app
from youreapyousow.repos import Repositories
from youreapyousow.store import Database

PURCHASE_DIR = scenario.PURCHASE_CONFIG_DIR
PART = scenario.load_purchase(PURCHASE_DIR / "purchase-part.yaml")
COMPUTE = scenario.load_purchase(PURCHASE_DIR / "purchase-compute.yaml")
FAN = PART.need_spec().model_copy(
    update={
        "search": CatalogueSearch(query="120mm case fan"),
        "match": AttributeMatch(attributes={"Size": ("120 mm",)}),
    }
)
P95 = Constraint(
    metric="p95_latency_ms", comparator=Comparator.LT, threshold=Decimal(500), unit="ms"
)
RATIONALE = "the cheapest landed price that fills the need"
HOSTILE = "<script>alert(1)</script>"


@dataclass
class AgenticStack:
    """The control plane on the agentic mock, and the pieces a test pokes at.

    Attributes:
        cp: The control plane.
        engine: The agentic mock engine, for its test hooks.
        reap: The real client, pointed at the in-process mock.
        clock: The manual clock everything shares.
    """

    cp: ControlPlane
    engine: AgenticMockEngine
    reap: ReapHttpClient
    clock: ManualClock

    async def aclose(self) -> None:
        """Release the client's connections."""
        await self.reap.aclose()

    def tick(self, seconds: float) -> None:
        """Move the shared clock on, as time passes between steps of a run.

        Args:
            seconds: How far.
        """
        self.clock.advance(seconds=seconds)

    async def buy(self, need: Need, merchants: tuple[str, ...]) -> PurchaseIntent:
        """Quote a need, propose the best landed quote exactly as quoted, and execute it.

        Args:
            need: The need.
            merchants: The grant's merchant scope.

        Returns:
            The intent after the gate and, when allowed, after the checkout.
        """
        quotes = await self.cp.gather_quotes(need.id)
        chosen = best_quote(quotes, need=need, merchants=merchants)
        assert chosen is not None, f"no eligible quote for {need.what}"
        self.tick(2)
        intent, decision = self.propose(chosen, [q.variant.key for q in quotes])
        if decision != Disposition.ALLOW:
            return intent
        self.tick(1)
        return await self.cp.execute_purchase(intent.id)

    def propose(
        self, quote: LandedQuote, considered: list[str]
    ) -> tuple[PurchaseIntent, Disposition]:
        """Propose a landed quote exactly as quoted.

        Args:
            quote: The landed quote.
            considered: The variant keys compared.

        Returns:
            The intent and the gate's disposition.
        """
        intent, decision = self.cp.propose_purchase(
            quote_id=quote.id,
            provider=quote.merchant,
            offer_id=quote.variant.id,
            amount_usd=quote.final_amount.amount,
            rationale=RATIONALE,
            options_considered=considered,
            quantity=quote.quantity,
            currency=quote.final_amount.currency,
        )
        return intent, decision.disposition


def agentic_stack(
    clock: ManualClock,
    *,
    db: Database | None = None,
    catalogue: Catalogue | None = None,
    settings: AgenticSettings | None = None,
) -> AgenticStack:
    """Build the control plane on the agentic path against the agentic mock.

    Args:
        clock: The manual clock; polls and back-offs advance it instead of sleeping.
        db: The database, in memory when None.
        catalogue: The mock's catalogue, all three bundled ones when None.
        settings: How the agentic path checks out, the part file's when None.

    Returns:
        The stack.
    """
    database = db or Database()
    repos, ledger = Repositories(database), Ledger(database, clock)
    engine = AgenticMockEngine(catalogue or bundled_catalogue(), clock=clock)
    app = create_mock_app(MockReapEngine(clock=clock), clock, agentic=engine)

    async def sleep(seconds: float) -> None:
        clock.advance(seconds=seconds)

    reap = ReapHttpClient(
        base_url=MOCK_BASE_URL,
        api_key=SecretStr("mock-key"),
        backend="mock",
        transport=httpx.ASGITransport(app=app),
        sleep=sleep,
        monotonic=lambda: clock().timestamp(),
    )
    cp = ControlPlane(
        repos=repos,
        ledger=ledger,
        gate=AuthorityGate(repos, ledger, clock, need_judge=need_judge_for(repos)),
        reap=reap,
        market=MarketService(httpx.AsyncClient(), mode=MarketMode.MOCK, clock=clock),
        provisioner=MockProvisioner(),
        operator=Operator(email="op@example.com", phone="+6500000000"),
        clock=clock,
        purchase_path=PurchasePath.AGENTIC,
        settings=settings or PART.settings,
        card_entry=mock_card_entry(engine),
        sleep=sleep,
    )
    return AgenticStack(cp, engine, reap, clock)


@dataclass(frozen=True)
class PartStory:
    """What shape (b)'s story left on the ledger.

    Attributes:
        objective_id: The objective.
        fault_id: The drive's fault.
        need_id: The drive need.
        intent_id: The drive purchase, completed.
        checkout_id: Its Reap checkout.
        order_id: The merchant's order id.
        ticket_id: The technician's ticket.
        fan_need_id: The fan need.
        fan_intent_id: The fan purchase, escalated, approved and awaiting Reap's page.
        fan_checkout_id: Its Reap checkout.
    """

    objective_id: str
    fault_id: str
    need_id: str
    intent_id: str
    checkout_id: str
    order_id: str
    ticket_id: str
    fan_need_id: str
    fan_intent_id: str
    fan_checkout_id: str


@dataclass(frozen=True)
class Sensor:
    """One of the storage node's sensors, as ``configs/machine.yaml`` describes it.

    Attributes:
        component: What it watches.
        metric: The metric it reports.
        unit: The metric's unit.
        healthy: A healthy reading.
        failed: A failed reading.
        constraint: The objective's constraint on it.
        code: The fault code the bill of materials maps.
        part: The part the code maps to.
        query: The part's catalogue search.
    """

    component: str
    metric: str
    unit: str
    healthy: int
    failed: int
    constraint: Constraint
    code: str
    part: str
    query: str


DRIVE = Sensor(
    "drive",
    "reallocated_sectors",
    "sectors",
    0,
    64,
    Constraint(
        metric="reallocated_sectors",
        comparator=Comparator.LT,
        threshold=Decimal(10),
        unit="sectors",
    ),
    "drive_failing",
    "1 TB NVMe M.2 2280 SSD",
    "1TB NVMe M.2 2280 SSD",
)
FAN_SENSOR = Sensor(
    "fan",
    "fan_rpm",
    "rpm",
    1800,
    0,
    Constraint(metric="fan_rpm", comparator=Comparator.GT, threshold=Decimal(600), unit="rpm"),
    "fan_stopped",
    "120 mm case fan",
    "120mm case fan",
)


def _fault(stack: AgenticStack, objective_id: str, sensor: Sensor) -> str:
    """Record a failed reading, the fault and the part it maps to, as the machine does.

    The shapes follow ``agent/fulfil.py``.

    Args:
        stack: The stack.
        objective_id: The objective.
        sensor: The sensor that failed.

    Returns:
        The fault id.
    """
    cp = stack.cp
    reading = cp.record_observation(
        objective_id, metric=sensor.metric, value=Decimal(sensor.failed), unit=sensor.unit
    )
    fault_id = new_id("flt")
    bound = sensor.constraint
    cp.ledger.append(
        EventType.FAULT_DETECTED,
        subject_id=fault_id,
        objective_id=objective_id,
        refs={"objective": objective_id, "observation": reading.id},
        payload={
            "code": sensor.code,
            "component": sensor.component,
            "metric": sensor.metric,
            "value": str(reading.value),
            "unit": sensor.unit,
            "constraint": f"{bound.comparator.value} {bound.threshold}",
            "source": reading.source,
        },
    )
    cp.ledger.append(
        EventType.PART_MAPPED,
        subject_id=new_id("prt"),
        objective_id=objective_id,
        refs={"fault": fault_id},
        payload={"fault": sensor.code, "part": sensor.part, "query": sensor.query, "accept": {}},
    )
    return fault_id


def _ticket(
    stack: AgenticStack, done: PurchaseIntent, fault_id: str, sensor: Sensor, since: datetime
) -> str:
    """Open the technician's ticket for a completed part order, as the follow-through does.

    Args:
        stack: The stack.
        done: The completed intent.
        fault_id: The fault it fixes.
        sensor: The failed sensor.
        since: When the fault was detected.

    Returns:
        The ticket id.
    """
    seconds = int((stack.clock() - since).total_seconds())
    ticket_id = new_id("tkt")
    stack.cp.ledger.append(
        EventType.TICKET_OPENED,
        subject_id=ticket_id,
        objective_id=done.objective_id,
        refs={
            "fault": fault_id,
            "intent": done.id,
            "checkout": done.checkout_id or "",
            "order": done.order_id or "",
        },
        payload={
            "fault": sensor.code,
            "component": sensor.component,
            "part": sensor.part,
            "merchant": done.provider,
            "variant": done.offer_id,
            "order_id": done.order_id,
            "final_amount_usd": str(done.final_amount_usd),
            "seconds_from_fault": seconds,
            "text": f"Replace the {sensor.component}: {sensor.part} ordered {seconds} s after"
            f" the fault, order {done.order_id} from {done.provider}",
        },
    )
    return ticket_id


async def part_objective(stack: AgenticStack, *, statement: str | None = None) -> str:
    """Create an objective under the part file's grant, enrolled on the mock.

    Args:
        stack: The stack.
        statement: The objective in words.

    Returns:
        The objective id.
    """
    objective = await stack.cp.create_objective(
        kind=ObjectiveKind.SERVICE_FLEET,
        statement=statement or "Keep storage node sn-01 in service",
        constraints=[DRIVE.constraint, FAN_SENSOR.constraint],
        budget_usd=PART.grant.budget_usd,
        grant=PART.terms,
        lifecycle=LifecycleRules(),
    )
    for sensor in (DRIVE, FAN_SENSOR):
        stack.cp.record_observation(
            objective.id, metric=sensor.metric, value=Decimal(sensor.healthy), unit=sensor.unit
        )
    return objective.id


PSU = PART.need_spec().model_copy(
    update={
        "search": CatalogueSearch(query="650W power supply"),
        "match": AttributeMatch(attributes={"Wattage": ("650 W",)}),
    }
)


async def replay_part(stack: AgenticStack, *, statement: str | None = None) -> PartStory:
    """Run shape (b): a failing drive bought and ticketed, then a premium fan escalated.

    Args:
        stack: The stack.
        statement: The objective in words.

    Returns:
        The ids the story produced.
    """
    cp = stack.cp
    objective_id = await part_objective(stack, statement=statement)
    stack.tick(20)
    fault_at = stack.clock()
    fault_id = _fault(stack, objective_id, DRIVE)
    need = cp.raise_need(
        objective_id,
        PART.need_spec(),
        reason="drive failing: reallocated sectors 64, healthy below 10",
        refs={"fault": fault_id},
    )
    stack.tick(3)
    done = await stack.buy(need, PART.merchants)
    assert done.state == IntentState.COMPLETED, done.state
    assert done.order_id is not None
    assert done.checkout_id is not None
    ticket_id = _ticket(stack, done, fault_id, DRIVE, fault_at)
    stack.tick(30)
    fan_fault = _fault(stack, objective_id, FAN_SENSOR)
    fan_need = cp.raise_need(
        objective_id, FAN, reason="fan stopped: 0 rpm, healthy above 600", refs={"fault": fan_fault}
    )
    escalated = await stack.buy(fan_need, PART.merchants)
    assert escalated.state == IntentState.ESCALATED, escalated.state
    stack.tick(5)
    cp.gate.approve(escalated.id, "operator")
    waiting = await cp.execute_purchase(escalated.id)
    assert waiting.state == IntentState.AWAITING_APPROVAL, waiting.state
    assert waiting.checkout_id is not None
    return PartStory(
        objective_id=objective_id,
        fault_id=fault_id,
        need_id=need.id,
        intent_id=done.id,
        checkout_id=done.checkout_id,
        order_id=done.order_id,
        ticket_id=ticket_id,
        fan_need_id=fan_need.id,
        fan_intent_id=waiting.id,
        fan_checkout_id=waiting.checkout_id,
    )


@dataclass(frozen=True)
class ComputeStory:
    """What shape (a)'s story left on the ledger.

    Attributes:
        objective_id: The objective.
        breach_id: The observation that broke the target.
        need_id: The credits need.
        intent_id: The purchase, completed.
        checkout_id: Its Reap checkout.
        order_id: The merchant's order id.
        deployment_id: The capacity the order backs.
    """

    objective_id: str
    breach_id: str
    need_id: str
    intent_id: str
    checkout_id: str
    order_id: str
    deployment_id: str


async def replay_compute(stack: AgenticStack) -> ComputeStory:
    """Run shape (a): a latency breach, credits bought to cover the lease, capacity added.

    Args:
        stack: The stack, built with the compute file's settings.

    Returns:
        The ids the story produced.
    """
    cp = stack.cp
    objective = await cp.create_objective(
        kind=ObjectiveKind.SERVICE_FLEET,
        statement="Keep p95 under 500 ms",
        constraints=[P95],
        budget_usd=COMPUTE.grant.budget_usd,
        grant=COMPUTE.terms,
        lifecycle=LifecycleRules(),
    )
    objective_id = objective.id
    baseline = (("requests_per_min", 14, "req/min"), ("p95_latency_ms", 281, "ms"))
    for metric, value, unit in baseline:
        cp.record_observation(objective_id, metric=metric, value=Decimal(value), unit=unit)
    stack.tick(20)
    cp.record_observation(
        objective_id, metric="requests_per_min", value=Decimal(80), unit="req/min"
    )
    stack.tick(3)
    breach = cp.record_observation(
        objective_id, metric="p95_latency_ms", value=Decimal(1360), unit="ms"
    )
    need = cp.raise_need(
        objective_id,
        COMPUTE.need_spec(),
        reason="p95 1,360 ms breaks p95 < 500 ms",
        estimate_usd=Decimal(40),
        refs={"observation": breach.id},
    )
    stack.tick(3)
    done = await stack.buy(need, COMPUTE.merchants)
    assert done.state == IntentState.COMPLETED, done.state
    assert done.order_id is not None
    assert done.checkout_id is not None
    deployment_id = new_id("dep")
    cp.ledger.append(
        EventType.DEPLOYMENT_PROVISIONED,
        subject_id=deployment_id,
        objective_id=objective_id,
        refs={
            "intent": done.id,
            "checkout": done.checkout_id,
            "need": need.id,
            "order": done.order_id,
        },
        payload={
            "provider": "vast",
            "offer": "vast:1",
            "mode": "mock",
            "simulation_notice": "Mock provisioning: no capacity was started anywhere.",
            "usd_per_hour": "0.071",
            "order_id": done.order_id,
            "merchant": done.provider,
            "credit_usd": str(done.final_amount_usd),
        },
    )
    stack.tick(16)
    cp.record_observation(
        objective_id,
        metric="p95_latency_ms",
        value=Decimal(265),
        unit="ms",
        deployment_id=deployment_id,
    )
    return ComputeStory(
        objective_id=objective_id,
        breach_id=breach.id,
        need_id=need.id,
        intent_id=done.id,
        checkout_id=done.checkout_id,
        order_id=done.order_id,
        deployment_id=deployment_id,
    )


def hostile_catalogue(directory: Path) -> Path:
    """Write a catalogue whose merchant name and product title carry a script tag.

    Args:
        directory: Where to write it.

    Returns:
        The catalogue file.
    """
    path = directory / "hostile.yaml"
    path.write_text(
        f"""
name: hostile
description: A merchant and a product whose names are markup.
currency: USD
default_country: SG
countries:
  SG: {{tax_rate: "0.09", tax_included: false, calling_code: "65"}}
merchants:
  - name: "Evil {HOSTILE} Parts"
    domain: evil.example
    ships_to: [SG]
    shipping_options:
      - {{id: standard, name: "Standard {HOSTILE}", price: "5", selected: true}}
    products:
      - id: prd-evil-drive
        name: "NVMe drive {HOSTILE}"
        keywords: [nvme]
        variants:
          - id: var-evil-1tb
            name: "1 TB {HOSTILE}"
            options: {{Capacity: "1 TB"}}
            price: "60"
""",
        encoding="utf-8",
    )
    return path


HOSTILE_NEED = NeedSpec.model_validate(
    PART.need_spec().model_dump()
    | {
        "search": CatalogueSearch(query="nvme").model_dump(),
        "match": AttributeMatch(attributes={"Capacity": ("1 TB",)}).model_dump(),
    }
)
HOSTILE_MERCHANT = f"Evil {HOSTILE} Parts"


async def replay_hostile(stack: AgenticStack) -> str:
    """Buy from the hostile catalogue, so markup lands in every event that names it.

    Args:
        stack: The stack, built over ``hostile_catalogue``.

    Returns:
        The objective id.
    """
    objective = await stack.cp.create_objective(
        kind=ObjectiveKind.SERVICE_FLEET,
        statement=f"Keep {HOSTILE} in service",
        constraints=[],
        budget_usd=PART.grant.budget_usd,
        grant=replace(PART.terms, allowed_merchants=(HOSTILE_MERCHANT,)),
        lifecycle=LifecycleRules(),
    )
    objective_id = objective.id
    need = stack.cp.raise_need(objective_id, HOSTILE_NEED, reason=f"fault {HOSTILE}")
    done = await stack.buy(need, (HOSTILE_MERCHANT,))
    assert done.state == IntentState.COMPLETED, done.state
    return objective_id

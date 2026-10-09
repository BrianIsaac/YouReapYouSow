"""End-to-end tests of the tool contracts: the service-fleet loop on the mock stack."""

import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from youreapyousow import purchase as scenario
from youreapyousow.authority.gate import AuthorityGate
from youreapyousow.authority.lifecycle import LifecycleAction, LifecycleRules
from youreapyousow.clock import ManualClock
from youreapyousow.control import (
    AgenticSettings,
    CompletedPurchase,
    ControlPlane,
    ControlPlaneError,
    GrantTerms,
    Operator,
    PurchasePath,
    mock_card_entry,
    need_judge_for,
    purchase_path_from_env,
)
from youreapyousow.domain import (
    Comparator,
    Constraint,
    DeploymentStatus,
    Disposition,
    IntentState,
    LandedQuote,
    ObjectiveKind,
    ObjectiveStatus,
    Offer,
    PurchaseIntent,
    ResourceSpec,
    Source,
)
from youreapyousow.ledger.events import EventType, LedgerEvent
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.market.provisioning import MockProvisioner
from youreapyousow.market.service import MarketMode, MarketService
from youreapyousow.procure.need import (
    AttributeMatch,
    CatalogueSearch,
    Need,
    NeedSpec,
    SearchPreference,
    Shape,
    ValueMatch,
    ValueOption,
)
from youreapyousow.procure.selection import best_quote
from youreapyousow.reap.client import (
    MOCK_BASE_URL,
    ReapError,
    ReapHttpClient,
    ReapMock,
    ReapTransportError,
)
from youreapyousow.reap.mock.agentic import (
    TEST_OTP,
    AgenticMockConfig,
    AgenticMockEngine,
    Operation,
    SimulatedCreate,
    bundled_catalogue,
)
from youreapyousow.reap.mock.engine import MockReapEngine
from youreapyousow.reap.mock.server import create_mock_app
from youreapyousow.reap.models import (
    SIMULATE_CHECKOUT_HEADER,
    AgenticErrorCode,
    CardTransaction,
    CheckoutStatus,
    DeclinedCardTransaction,
    EnrollmentStatus,
    ScopeType,
    SimulateAuthorizationRequest,
    SimulatedMerchant,
)
from youreapyousow.repos import Repositories
from youreapyousow.store import Database

pytestmark = pytest.mark.anyio

SPEC = ResourceSpec(min_vram_gb=24, max_price_usd_per_hour=Decimal("1.00"))
P95 = Constraint(metric="p95_latency_ms", comparator=Comparator.LT, threshold=Decimal(500))
TERMS = GrantTerms(
    allowed_providers=("vast", "runpod", "shadeform"),
    per_transaction_cap_usd=Decimal(5),
    daily_cap_usd=Decimal(20),
    ttl=timedelta(hours=4),
    max_price_usd_per_hour=Decimal("1.00"),
)


@dataclass
class Stack:
    """The control plane and the pieces tests poke at.

    Attributes:
        cp: The control plane.
        reap: The mock Reap backend.
        clock: The manual clock.
    """

    cp: ControlPlane
    reap: "FlakyReap"
    clock: ManualClock

    def card_id(self, objective_id: str) -> str:
        """Return an objective's Reap card.

        Returns:
            The card id.
        """
        binding = self.cp.objective(objective_id).reap
        assert binding is not None
        return binding.card_id


class FlakyReap(ReapMock):
    """The mock backend, able to fail authorisations in transport on demand."""

    fail_with_maybe_sent: bool | None = None

    async def simulate_authorization(
        self, req: SimulateAuthorizationRequest, *, idempotency_key: str | None
    ) -> CardTransaction:
        """Authorise, or fail in transport when told to.

        Returns:
            The transaction.

        Raises:
            ReapTransportError: When ``fail_with_maybe_sent`` is set.
        """
        if self.fail_with_maybe_sent is not None:
            raise ReapTransportError("read timeout", maybe_sent=self.fail_with_maybe_sent)
        return await super().simulate_authorization(req, idempotency_key=idempotency_key)


@pytest.fixture
async def stack(clock: ManualClock) -> AsyncIterator[Stack]:
    """A control plane on the mock Reap and the fixture market.

    Yields:
        The stack.
    """
    db = Database()
    repos, ledger = Repositories(db), Ledger(db, clock)
    reap = FlakyReap(clock=clock)
    http = httpx.AsyncClient()
    cp = ControlPlane(
        repos=repos,
        ledger=ledger,
        gate=AuthorityGate(repos, ledger, clock),
        reap=reap,
        market=MarketService(http, mode=MarketMode.MOCK, clock=clock),
        provisioner=MockProvisioner(),
        operator=Operator(email="op@example.com", phone="+6500000000"),
        clock=clock,
    )
    yield Stack(cp, reap, clock)
    await reap.aclose()
    await http.aclose()


async def _objective(stack: Stack, terms: GrantTerms = TERMS) -> str:
    objective = await stack.cp.create_objective(
        kind=ObjectiveKind.SERVICE_FLEET,
        statement="Maintain p95 below 500 ms",
        constraints=[P95],
        budget_usd=Decimal(25),
        grant=terms,
        lifecycle=LifecycleRules(),
    )
    return objective.id


async def _buy(stack: Stack, objective_id: str, hours: str = "2") -> tuple[str, Offer]:
    offers = await stack.cp.discover_resources(objective_id, SPEC)
    chosen = offers[0]
    quote = stack.cp.request_quote(objective_id, chosen, Decimal(hours))
    intent, decision = stack.cp.propose_purchase(
        quote_id=quote.id,
        provider=chosen.provider,
        offer_id=chosen.offer_id,
        amount_usd=quote.amount_usd,
        rationale="cheapest offer meeting the spec",
        options_considered=[o.key for o in offers[:3]],
    )
    assert decision.disposition == Disposition.ALLOW, decision.reason
    return intent.id, chosen


async def test_demo_loop_from_spike_to_release_and_provenance(stack: Stack) -> None:
    """Spike, discover, authorise, provision, recover, normalise, release, report."""
    cp, clock = stack.cp, stack.clock
    objective_id = await _objective(stack)
    objective = cp.objective(objective_id)
    assert objective.reap is not None
    assert len(objective.reap.policy_ids) == 4

    cp.record_observation(objective_id, metric="requests_per_min", value=Decimal(80))
    cp.record_observation(objective_id, metric="p95_latency_ms", value=Decimal(1360))

    intent_id, offer = await _buy(stack, objective_id)
    assert offer.source == Source.MOCK
    intent = await cp.execute_purchase(intent_id)
    assert intent.state == IntentState.AUTHORISED
    assert intent.reap_transaction_id is not None
    runway_after_auth = cp.gate.runway_usd(objective_id)
    assert runway_after_auth == Decimal(25) - intent.amount_usd

    effective = await stack.reap.effective_policies(ScopeType.CARD, objective.reap.card_id)
    lifetime = next(e for e in effective.items if e.policy.name.endswith("lifetime budget"))
    assert lifetime.usage is not None
    assert lifetime.usage.remaining == runway_after_auth

    deployment = await cp.provision(intent_id)
    assert deployment.status == DeploymentStatus.RUNNING
    assert "Mock provisioning" in deployment.simulation_notice
    assert await cp.provision(intent_id) == deployment

    clock.advance(seconds=30)
    cp.record_observation(
        objective_id, metric="p95_latency_ms", value=Decimal(418), deployment_id=deployment.id
    )
    clock.advance(seconds=100)
    assert cp.evaluate_deployment(deployment.id).action == LifecycleAction.KEEP

    cp.record_observation(objective_id, metric="requests_per_min", value=Decimal(14))
    clock.advance(minutes=6)
    decision = cp.evaluate_deployment(deployment.id)
    assert (decision.action, decision.rule) == (LifecycleAction.RELEASE, "demand.normalised")

    released = await cp.release(deployment.id, decision.rule)
    assert released.status == DeploymentStatus.RELEASED
    settled = cp.repos.intents.require(intent_id)[0]
    assert settled.state == IntentState.SETTLED
    assert settled.settled_usd is not None
    assert Decimal(0) < settled.settled_usd < intent.amount_usd
    assert cp.gate.runway_usd(objective_id) == Decimal(25) - settled.settled_usd

    completed = await cp.complete_objective(objective_id)
    assert completed.status == ObjectiveStatus.COMPLETED
    card = await stack.reap.get_card(objective.reap.card_id)
    assert card.frozen

    graph = cp.provenance(objective_id)
    release_causes = [
        graph.nodes[e.source].kind
        for e in graph.edges
        if e.target == deployment.id and e.relation == "deployment.released"
    ]
    assert release_causes == ["lifecycle_decision"]
    assert not any(e.source == objective_id and e.target == deployment.id for e in graph.edges)
    why = {n.kind for n in graph.lineage(deployment.id)}
    assert {
        "objective",
        "discovery",
        "offer",
        "quote",
        "intent",
        "decision",
        "reap_transaction",
    } <= why
    assert cp.ledger.verify_chain().ok


async def test_agent_cannot_change_the_price_it_was_quoted(stack: Stack) -> None:
    """A proposal that restates a different amount is refused and nothing is charged."""
    objective_id = await _objective(stack)
    offers = await stack.cp.discover_resources(objective_id, SPEC)
    quote = stack.cp.request_quote(objective_id, offers[0], Decimal(2))
    intent, decision = stack.cp.propose_purchase(
        quote_id=quote.id,
        provider=offers[0].provider,
        offer_id=offers[0].offer_id,
        amount_usd=quote.amount_usd - Decimal("0.01"),
        rationale="injected",
        options_considered=[],
    )
    assert (decision.disposition, decision.rule) == (Disposition.REFUSE, "quote.exact_match")
    assert (await stack.cp.execute_purchase(intent.id)).state == IntentState.REFUSED
    assert not stack.cp.ledger.events(types=[EventType.REAP_AUTHORISED])


async def test_execute_twice_charges_once(stack: Stack) -> None:
    """A replayed execute returns the recorded outcome without a second charge."""
    objective_id = await _objective(stack)
    intent_id, _ = await _buy(stack, objective_id)
    first = await stack.cp.execute_purchase(intent_id)
    second = await stack.cp.execute_purchase(intent_id)
    assert first.reap_transaction_id == second.reap_transaction_id
    assert len(stack.cp.ledger.events(types=[EventType.REAP_AUTHORISED])) == 1


async def test_revocation_after_proposal_stops_the_charge(stack: Stack) -> None:
    """Authority revoked between proposal and execution refuses before Reap is called."""
    objective_id = await _objective(stack)
    intent_id, _ = await _buy(stack, objective_id)
    grant = stack.cp.gate.current_grant(objective_id)
    assert grant is not None
    stack.cp.gate.revoke_grant(grant.id, "operator pulled the plug")
    assert (await stack.cp.execute_purchase(intent_id)).state == IntentState.REFUSED
    assert not stack.cp.ledger.events(types=[EventType.PURCHASE_CLAIMED])


async def test_reap_floor_declines_what_the_gate_would_never_send(stack: Stack) -> None:
    """Even bypassing the gate, Reap's mirrored policies refuse and name the rule."""
    objective_id = await _objective(stack)
    card_id = stack.card_id(objective_id)
    over_cap: CardTransaction = await stack.reap.simulate_authorization(
        SimulateAuthorizationRequest(
            card_id=card_id, amount=Decimal("5.01"), merchant=SimulatedMerchant(mcc_code="7372")
        ),
        idempotency_key=None,
    )
    assert isinstance(over_cap, DeclinedCardTransaction)
    assert over_cap.decline_reason.code == "TRANSACTION_AMOUNT_LIMIT_EXCEEDED"
    assert over_cap.policy is not None
    assert over_cap.policy.name.endswith("per-transaction cap")
    casino = await stack.reap.simulate_authorization(
        SimulateAuthorizationRequest(
            card_id=card_id, amount=Decimal(1), merchant=SimulatedMerchant(mcc_code="7995")
        ),
        idempotency_key=None,
    )
    assert isinstance(casino, DeclinedCardTransaction)
    assert casino.decline_reason.code == "MERCHANT_NOT_ALLOWED"


async def test_reap_decline_is_recorded_and_costs_nothing(stack: Stack) -> None:
    """If Reap declines (here a frozen card), the intent is declined and runway intact."""
    objective_id = await _objective(stack)
    intent_id, _ = await _buy(stack, objective_id)
    await stack.reap.freeze_card(stack.card_id(objective_id))
    intent = await stack.cp.execute_purchase(intent_id)
    assert (intent.state, intent.decline_code) == (IntentState.DECLINED, "CARD_FROZEN")
    assert stack.cp.gate.runway_usd(objective_id) == Decimal(25)


@pytest.mark.parametrize(
    ("maybe_sent", "state"),
    [(True, IntentState.OUTCOME_UNKNOWN), (False, IntentState.FAILED)],
)
async def test_transport_failures(stack: Stack, maybe_sent: bool, state: IntentState) -> None:
    """An ambiguous failure keeps counting; a certain non-send releases the budget."""
    objective_id = await _objective(stack)
    intent_id, _ = await _buy(stack, objective_id)
    stack.reap.fail_with_maybe_sent = maybe_sent
    intent = await stack.cp.execute_purchase(intent_id)
    assert intent.state == state
    expected = Decimal(25) - (intent.amount_usd if maybe_sent else 0)
    assert stack.cp.gate.runway_usd(objective_id) == expected
    assert not stack.cp.ledger.events(types=[EventType.REAP_AUTHORISED])


async def test_provision_requires_authorisation_and_completion_requires_release(
    stack: Stack,
) -> None:
    """Tools called out of order refuse rather than guess."""
    objective_id = await _objective(stack)
    intent_id, _ = await _buy(stack, objective_id)
    with pytest.raises(ControlPlaneError, match="not authorised"):
        await stack.cp.provision(intent_id)
    await stack.cp.execute_purchase(intent_id)
    await stack.cp.provision(intent_id)
    with pytest.raises(ControlPlaneError, match="still running"):
        await stack.cp.complete_objective(objective_id)


async def test_second_objective_reuses_the_operator(stack: Stack) -> None:
    """The KYC'd cardholder is created once; each objective gets its own card."""
    first = stack.cp.objective(await _objective(stack))
    second = stack.cp.objective(await _objective(stack))
    assert first.reap is not None
    assert second.reap is not None
    assert first.reap.user_id == second.reap.user_id
    assert first.reap.card_id != second.reap.card_id


async def test_quote_expiry_forces_a_requote(stack: Stack) -> None:
    """A quote older than its TTL cannot be executed."""
    objective_id = await _objective(stack)
    intent_id, _ = await _buy(stack, objective_id)
    stack.clock.advance(seconds=61)
    assert (await stack.cp.execute_purchase(intent_id)).state == IntentState.REFUSED


# The agentic path: need, catalogue, landed quote, gate,
# checkout and order, on the agentic mock behind the real HTTP client.

PURCHASE_FILES = {
    "compute": scenario.PURCHASE_CONFIG_DIR / "purchase-compute.yaml",
    "part": scenario.PURCHASE_CONFIG_DIR / "purchase-part.yaml",
    "checkout_url": scenario.PURCHASE_CONFIG_DIR / "purchase-checkout-url.yaml",
}
PART = scenario.load_purchase(PURCHASE_FILES["part"])
COMPUTE = scenario.load_purchase(PURCHASE_FILES["compute"])
FAN = PART.need_spec().model_copy(
    update={
        "search": CatalogueSearch(query="120mm case fan"),
        "match": AttributeMatch(attributes={"Size": ("120 mm",)}),
    }
)
CHECKOUTS = "/agentic/checkouts"


class RecordingTransport(httpx.AsyncBaseTransport):
    """Hands every request to the mock and keeps it, so a test sees what Reap saw."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        """Wrap a transport.

        Args:
            inner: The transport to the mock app.
        """
        self.inner = inner
        self.seen: list[httpx.Request] = []
        self._lost: dict[tuple[str, str], int] = {}
        self._cut: tuple[str, str] | None = None
        self.ignored: set[str] = set()

    def lose_answers(self, method: str, path: str, *, times: int) -> None:
        """Lose the next answers to one operation after the mock has handled the request.

        Unlike the engine's own drop, this also loses the answers to same-key replays.

        Args:
            method: The HTTP method.
            path: The exact path.
            times: How many answers to lose.
        """
        self._lost[method, path] = times

    def cut(self, method: str, prefix: str | None) -> None:
        """Refuse every connection for one method under a path prefix, or restore them.

        Args:
            method: The HTTP method.
            prefix: The path prefix to cut off; None restores the connection.
        """
        self._cut = (method, prefix) if prefix is not None else None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record the request and pass it on, losing the answer when told to.

        Args:
            request: The request.

        Returns:
            The mock's response.

        Raises:
            httpx.ConnectError: When the path is cut off.
            httpx.ReadError: When the answer is to be lost.
        """
        cut = self._cut
        if cut is not None and request.method == cut[0] and request.url.path.startswith(cut[1]):
            raise httpx.ConnectError("connection refused", request=request)
        for header in self.ignored:
            request.headers.pop(header, None)
        self.seen.append(request)
        response = await self.inner.handle_async_request(request)
        key = (request.method, request.url.path)
        if self._lost.get(key, 0) > 0:
            self._lost[key] -= 1
            raise httpx.ReadError("answer lost after the request was sent", request=request)
        return response

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        """Return the requests sent to one operation.

        Args:
            method: The HTTP method.
            path: The exact path.

        Returns:
            Those requests, in order.
        """
        return [r for r in self.seen if r.method == method and r.url.path == path]


@dataclass
class Agentic:
    """The control plane on the agentic mock, and the pieces tests poke at.

    Attributes:
        cp: The control plane.
        engine: The agentic mock engine, for its test hooks.
        wire: Every request the mock saw.
        clock: The manual clock the mock, the client and the control plane share.
        completed: Every purchase the follow-through hook was handed.
    """

    cp: ControlPlane
    engine: AgenticMockEngine
    wire: RecordingTransport
    clock: ManualClock
    completed: list[CompletedPurchase]

    async def objective(self, purchase: scenario.PurchaseConfig = PART) -> str:
        """Create an objective under a purchase block's grant, enrolled on the mock.

        Args:
            purchase: The purchase block.

        Returns:
            The objective id.
        """
        objective = await self.cp.create_objective(
            kind=ObjectiveKind.SERVICE_FLEET,
            statement="Keep the storage node in service",
            constraints=[],
            budget_usd=purchase.grant.budget_usd,
            grant=purchase.terms,
            lifecycle=LifecycleRules(),
        )
        return objective.id

    def need(
        self,
        objective_id: str,
        spec: NeedSpec | None = None,
        *,
        estimate: str | None = None,
    ) -> Need:
        """Raise a need.

        Args:
            objective_id: The objective.
            spec: What to buy; the part file's when None.
            estimate: The estimated lease, for a need judged by value.

        Returns:
            The need.
        """
        return self.cp.raise_need(
            objective_id,
            spec or PART.need_spec(),
            reason="reallocated sectors past the threshold",
            estimate_usd=Decimal(estimate) if estimate is not None else None,
        )

    def propose(self, quote: LandedQuote) -> tuple[PurchaseIntent, Disposition]:
        """Propose a landed quote exactly as quoted.

        Args:
            quote: The landed quote.

        Returns:
            The intent and the gate's disposition.
        """
        intent, decision = self.cp.propose_purchase(
            quote_id=quote.id,
            provider=quote.merchant,
            offer_id=quote.variant.id,
            amount_usd=quote.final_amount.amount,
            rationale="the cheapest landed price that fills the need",
            options_considered=[quote.variant.key],
            quantity=quote.quantity,
            currency=quote.final_amount.currency,
        )
        return intent, decision.disposition

    async def cheapest(self, need: Need, purchase: scenario.PurchaseConfig = PART) -> LandedQuote:
        """Gather the need's landed quotes and take the best.

        Args:
            need: The need.
            purchase: The purchase block whose merchants scope the choice.

        Returns:
            The cheapest eligible landed quote.
        """
        quotes = await self.cp.gather_quotes(need.id)
        chosen = best_quote(quotes, need=need, merchants=purchase.merchants)
        assert chosen is not None
        return chosen

    def runway(self, objective_id: str) -> Decimal:
        """Return the objective's runway.

        Returns:
            Budget minus committed spend.
        """
        return self.cp.gate.runway_usd(objective_id)

    def events(self, *types: EventType) -> list[LedgerEvent]:
        """Return the ledger's events of the given types.

        Returns:
            The events, in order.
        """
        return self.cp.ledger.events(types=list(types))


def _agentic_plane(
    clock: ManualClock,
    *,
    purchase_path: PurchasePath | None = PurchasePath.AGENTIC,
    settings: AgenticSettings | None = None,
    card_entry: bool = True,
    config: AgenticMockConfig | None = None,
) -> tuple[Agentic, ReapHttpClient]:
    db = Database()
    repos, ledger = Repositories(db), Ledger(db, clock)
    engine = AgenticMockEngine(bundled_catalogue(), clock=clock, config=config)
    app = create_mock_app(MockReapEngine(clock=clock), clock, agentic=engine)
    wire = RecordingTransport(httpx.ASGITransport(app=app))

    async def sleep(seconds: float) -> None:
        clock.advance(seconds=seconds)

    reap = ReapHttpClient(
        base_url=MOCK_BASE_URL,
        api_key=SecretStr("mock-key"),
        backend="mock",
        transport=wire,
        sleep=sleep,
        monotonic=lambda: clock().timestamp(),
    )
    completed: list[CompletedPurchase] = []

    async def follow_through(purchase: CompletedPurchase) -> None:
        completed.append(purchase)

    cp = ControlPlane(
        repos=repos,
        ledger=ledger,
        gate=AuthorityGate(repos, ledger, clock, need_judge=need_judge_for(repos)),
        reap=reap,
        market=MarketService(httpx.AsyncClient(), mode=MarketMode.MOCK, clock=clock),
        provisioner=MockProvisioner(),
        operator=Operator(email="op@example.com", phone="+6500000000"),
        clock=clock,
        purchase_path=purchase_path,
        settings=settings or PART.settings,
        card_entry=mock_card_entry(engine) if card_entry else None,
        follow_through={Shape.COMPUTE: follow_through, Shape.PART: follow_through},
        sleep=sleep,
    )
    return Agentic(cp, engine, wire, clock, completed), reap


@pytest.fixture
async def agentic(clock: ManualClock) -> AsyncIterator[Agentic]:
    """A control plane on the agentic path, against the agentic mock.

    Yields:
        The stack.
    """
    stack, reap = _agentic_plane(clock)
    yield stack
    await reap.aclose()


async def test_a_part_from_need_to_order(agentic: Agentic) -> None:
    """Shape (b): the drive need is searched, quoted, decided, checked out and ordered."""
    objective_id = await agentic.objective()
    enrolment = agentic.cp.objective(objective_id).enrolment
    assert enrolment is not None
    assert enrolment.is_active
    assert agentic.cp.objective(objective_id).reap is None

    need = agentic.need(objective_id)
    quotes = await agentic.cp.gather_quotes(need.id)
    landed = {q.variant.id: q.final_amount.amount for q in quotes}
    assert landed == {
        "var-northwind-nvme-1tb": Decimal(77),
        "var-kestrel-nvme-1tb": Decimal(79),
        "var-northwind-nvme-2tb": Decimal(107),
        "var-kestrel-pro-nvme-1tb": Decimal(194),
    }
    chosen = best_quote(quotes, need=need, merchants=PART.merchants)
    assert chosen is not None
    assert chosen.variant.id == "var-northwind-nvme-1tb"

    intent, disposition = agentic.propose(chosen)
    assert disposition == Disposition.ALLOW
    done = await agentic.cp.execute_purchase(intent.id)
    assert done.state == IntentState.COMPLETED
    assert done.order_id is not None
    assert done.order_id.startswith("MOCK-ORDER-")
    assert done.final_amount_usd == Decimal(77)
    assert agentic.runway(objective_id) == PART.grant.budget_usd - Decimal(77)

    order = agentic.cp.repos.orders.require(done.checkout_id or "")[0]
    assert (order.status, order.simulated, order.order_id) == (
        CheckoutStatus.COMPLETED,
        True,
        done.order_id,
    )
    [purchase] = agentic.completed
    assert (purchase.intent.id, purchase.need.id, purchase.order.order_id) == (
        intent.id,
        need.id,
        done.order_id,
    )
    assert purchase.need.spec.shape == Shape.PART

    types = [e.type for e in agentic.cp.ledger.events(objective_id=objective_id)]
    for earlier, later in itertools.pairwise(
        [
            EventType.REAP_ENROLLED,
            EventType.NEED_RAISED,
            EventType.CATALOGUE_SEARCHED,
            EventType.CATALOGUE_DETAILED,
            EventType.CATALOGUE_VARIANT_RESOLVED,
            EventType.QUOTE_LANDED,
            EventType.PURCHASE_PROPOSED,
            EventType.PURCHASE_CLAIMED,
            EventType.CHECKOUT_CREATED,
            EventType.CHECKOUT_COMPLETED,
        ]
    ):
        assert types.index(earlier) < types.index(later), (earlier, later)
    why = {n.kind for n in agentic.cp.provenance(objective_id).lineage(done.order_id)}
    assert {
        "need",
        "search",
        "product_details",
        "variant",
        "quote",
        "intent",
        "decision",
        "checkout",
    } <= why
    assert agentic.cp.ledger.verify_chain().ok


async def test_compute_credits_from_need_to_order(agentic: Agentic) -> None:
    """Shape (a): credits that cover a 40 USD estimate within four times it, cheapest landed."""
    objective_id = await agentic.objective(COMPUTE)
    within_four = COMPUTE.need_spec().model_copy(
        update={
            "match": ValueMatch(value_from=ValueOption(option="Amount"), max_multiple=Decimal(4))
        }
    )
    need = agentic.need(objective_id, within_four, estimate="40")
    quotes = await agentic.cp.gather_quotes(need.id)
    assert {q.variant.id for q in quotes} == {
        "var-northwind-gpu-credits-50",
        "var-northwind-gpu-credits-100",
        "var-kestrel-gpu-credits-50",
        "var-kestrel-gpu-credits-100",
    }
    chosen = best_quote(quotes, need=need, merchants=COMPUTE.merchants)
    assert chosen is not None
    assert (chosen.variant.id, chosen.final_amount.amount) == (
        "var-northwind-gpu-credits-50",
        Decimal("52.50"),
    )
    intent, disposition = agentic.propose(chosen)
    assert disposition == Disposition.ALLOW
    done = await agentic.cp.execute_purchase(intent.id)
    assert done.state == IntentState.COMPLETED
    assert agentic.completed[0].need.spec.shape == Shape.COMPUTE
    assert agentic.runway(objective_id) == COMPUTE.grant.budget_usd - Decimal("52.50")


async def test_a_refused_landed_price_moves_no_money(agentic: Agentic) -> None:
    """The premium drive lands above the 150 cap: refused, never claimed, never checked out."""
    objective_id = await agentic.objective()
    need = agentic.need(objective_id)
    quotes = await agentic.cp.gather_quotes(need.id)
    premium = next(q for q in quotes if q.variant.id == "var-kestrel-pro-nvme-1tb")
    intent, disposition = agentic.propose(premium)
    assert disposition == Disposition.REFUSE
    decision = agentic.cp.repos.decisions.require(intent.decision_id or "")[0]
    assert decision.rule == "amount.per_transaction_cap"
    assert (await agentic.cp.execute_purchase(intent.id)).state == IntentState.REFUSED
    assert agentic.wire.sent("POST", CHECKOUTS) == []
    assert not agentic.events(EventType.PURCHASE_CLAIMED, EventType.CHECKOUT_CREATED)
    assert agentic.runway(objective_id) == PART.grant.budget_usd


async def test_an_escalated_purchase_sends_no_header_and_waits_for_approval(
    agentic: Agentic,
) -> None:
    """The only fan is premium (137, above the 120 threshold): approved, then Reap's page."""
    objective_id = await agentic.objective()
    need = agentic.need(objective_id, FAN)
    chosen = await agentic.cheapest(need)
    assert (chosen.variant.id, chosen.final_amount.amount) == (
        "var-northwind-fan-120-premium",
        Decimal(137),
    )
    intent, disposition = agentic.propose(chosen)
    assert disposition == Disposition.ESCALATE
    assert (await agentic.cp.execute_purchase(intent.id)).state == IntentState.ESCALATED
    assert agentic.wire.sent("POST", CHECKOUTS) == []

    agentic.cp.gate.approve(intent.id, "operator")
    waiting = await agentic.cp.execute_purchase(intent.id)
    assert waiting.state == IntentState.AWAITING_APPROVAL
    [create] = agentic.wire.sent("POST", CHECKOUTS)
    assert SIMULATE_CHECKOUT_HEADER not in create.headers
    order = agentic.cp.repos.orders.require(waiting.checkout_id or "")[0]
    assert (order.simulated, order.approval_host) == (False, "reap-mock.local")
    assert agentic.runway(objective_id) == PART.grant.budget_usd - Decimal(137)
    assert agentic.completed == []
    assert (await agentic.cp.purchase_status(intent.id)).state == IntentState.AWAITING_APPROVAL

    agentic.engine.approve_checkout(waiting.checkout_id or "")
    assert (await agentic.cp.purchase_status(intent.id)).state == IntentState.AWAITING_APPROVAL
    agentic.clock.advance(seconds=1)
    done = await agentic.cp.purchase_status(intent.id)
    assert done.state == IntentState.COMPLETED
    assert done.order_id is not None
    assert len(agentic.completed) == 1
    assert len(agentic.wire.sent("POST", CHECKOUTS)) == 1


async def test_a_dropped_response_after_create_becomes_outcome_unknown(agentic: Agentic) -> None:
    """The checkout opened but every answer was lost: counted, the need blocked, never resent."""
    objective_id = await agentic.objective()
    need = agentic.need(objective_id)
    chosen = await agentic.cheapest(need)
    intent, _ = agentic.propose(chosen)
    agentic.wire.lose_answers("POST", CHECKOUTS, times=3)
    unknown = await agentic.cp.execute_purchase(intent.id)
    assert unknown.state == IntentState.OUTCOME_UNKNOWN
    assert unknown.checkout_id is None
    keys = {r.headers["Idempotency-Key"] for r in agentic.wire.sent("POST", CHECKOUTS)}
    assert keys == {unknown.idempotency_key}
    assert len(agentic.wire.sent("POST", CHECKOUTS)) == 3
    assert agentic.runway(objective_id) == PART.grant.budget_usd - chosen.final_amount.amount
    [event] = agentic.events(EventType.PURCHASE_OUTCOME_UNKNOWN)
    assert "2 same-key replays" in str(event.payload["error"])

    assert (await agentic.cp.purchase_status(intent.id)).state == IntentState.OUTCOME_UNKNOWN
    assert (await agentic.cp.execute_purchase(intent.id)).state == IntentState.OUTCOME_UNKNOWN
    assert len(agentic.wire.sent("POST", CHECKOUTS)) == 3
    retry = await agentic.cheapest(need)
    _, disposition = agentic.propose(retry)
    assert disposition == Disposition.REFUSE


async def test_one_dropped_answer_is_settled_by_a_same_key_replay(agentic: Agentic) -> None:
    """A replay under the claim's key returns the checkout the lost answer opened."""
    objective_id = await agentic.objective()
    chosen = await agentic.cheapest(agentic.need(objective_id))
    intent, _ = agentic.propose(chosen)
    agentic.engine.drop_next_response(Operation.CREATE_CHECKOUT)
    done = await agentic.cp.execute_purchase(intent.id)
    assert done.state == IntentState.COMPLETED
    creates = agentic.wire.sent("POST", CHECKOUTS)
    assert len(creates) == 2
    assert len({r.headers["Idempotency-Key"] for r in creates}) == 1
    assert len(agentic.events(EventType.CHECKOUT_CREATED)) == 1


async def test_a_second_execute_returns_the_recorded_outcome(agentic: Agentic) -> None:
    """Executing again neither claims nor checks out again."""
    objective_id = await agentic.objective()
    intent, _ = agentic.propose(await agentic.cheapest(agentic.need(objective_id)))
    first = await agentic.cp.execute_purchase(intent.id)
    second = await agentic.cp.execute_purchase(intent.id)
    assert first == second
    assert len(agentic.wire.sent("POST", CHECKOUTS)) == 1
    assert len(agentic.events(EventType.PURCHASE_CLAIMED)) == 1
    assert len(agentic.completed) == 1


async def test_a_temporarily_unavailable_checkout_fails_and_uses_an_attempt(
    agentic: Agentic,
) -> None:
    """``CHECKOUT_TEMPORARILY_UNAVAILABLE`` opened nothing: failed, counts nothing."""
    objective_id = await agentic.objective()
    intent, _ = agentic.propose(await agentic.cheapest(agentic.need(objective_id)))
    agentic.engine.inject_error(
        Operation.CREATE_CHECKOUT, AgenticErrorCode.CHECKOUT_TEMPORARILY_UNAVAILABLE
    )
    failed = await agentic.cp.execute_purchase(intent.id)
    assert (failed.state, failed.decline_code) == (
        IntentState.FAILED,
        AgenticErrorCode.CHECKOUT_TEMPORARILY_UNAVAILABLE,
    )
    assert agentic.runway(objective_id) == PART.grant.budget_usd


async def test_an_unreadable_checkout_is_outcome_unknown_until_read(agentic: Agentic) -> None:
    """Reads fail until the deadline: outcome unknown; a later read reconciles the order."""
    objective_id = await agentic.objective()
    intent, _ = agentic.propose(await agentic.cheapest(agentic.need(objective_id)))
    agentic.wire.cut("GET", f"{CHECKOUTS}/")
    started = agentic.clock()
    unknown = await agentic.cp.execute_purchase(intent.id)
    assert unknown.state == IntentState.OUTCOME_UNKNOWN
    assert unknown.checkout_id is not None
    assert agentic.clock() - started >= timedelta(seconds=PART.settings.poll_deadline_s)
    [event] = agentic.events(EventType.PURCHASE_OUTCOME_UNKNOWN)
    assert "still unread" in str(event.payload["error"])
    assert (await agentic.cp.purchase_status(intent.id)).state == IntentState.OUTCOME_UNKNOWN
    agentic.wire.cut("GET", None)
    done = await agentic.cp.purchase_status(intent.id)
    assert done.state == IntentState.COMPLETED
    assert len(agentic.completed) == 1


async def test_a_revoked_enrolment_stops_purchasing(agentic: Agentic) -> None:
    """``ENROLLMENT_NOT_ACTIVE`` fails the attempt and re-reads the enrolment, so rule 6 bites."""
    objective_id = await agentic.objective()
    need = agentic.need(objective_id)
    intent, _ = agentic.propose(await agentic.cheapest(need))
    enrolment = agentic.cp.objective(objective_id).enrolment
    assert enrolment is not None
    await agentic.cp.reap.revoke_enrollment(enrolment.id)
    failed = await agentic.cp.execute_purchase(intent.id)
    assert (failed.state, failed.decline_code) == (
        IntentState.FAILED,
        AgenticErrorCode.ENROLLMENT_NOT_ACTIVE,
    )
    reread = agentic.cp.objective(objective_id).enrolment
    assert reread is not None
    assert reread.status == EnrollmentStatus.REVOKED
    _, disposition = agentic.propose(await agentic.cheapest(need))
    assert disposition == Disposition.REFUSE


async def test_the_operators_enrolment_is_bound_and_recorded(clock: ManualClock) -> None:
    """Without a card step the enrolment waits on its hosted page; the operator completes it."""
    stack, reap = _agentic_plane(clock, card_entry=False)
    try:
        objective_id = await stack.objective()
        pending = stack.cp.objective(objective_id).enrolment
        assert pending is not None
        assert pending.status == EnrollmentStatus.REQUIRES_ACTION
        need = stack.need(objective_id)
        _, disposition = stack.propose(await stack.cheapest(need))
        assert disposition == Disposition.REFUSE

        stack.engine.submit_card(pending.id, **dict(zip(CARD_FIELDS, TEST_CARD, strict=True)))
        bound = await stack.cp.bind_enrolment(objective_id, enrollment_id=pending.id)
        assert bound.enrolment is not None
        assert (bound.enrolment.status, bound.enrolment.last4) == (EnrollmentStatus.ACTIVE, "7797")
        enrolled = stack.events(EventType.REAP_ENROLLED)
        assert [e.payload["status"] for e in enrolled] == ["REQUIRES_ACTION", "ACTIVE"]
        assert enrolled[-1].payload["completed_by"] == "operator"
        assert enrolled[-1].refs == {"objective": objective_id}
        _, disposition = stack.propose(await stack.cheapest(need))
        assert disposition == Disposition.ALLOW
    finally:
        await reap.aclose()


async def test_a_quote_unavailable_retries_under_a_new_key(agentic: Agentic) -> None:
    """``QUOTE_TEMPORARILY_UNAVAILABLE`` is cached under its key; the retry mints the next."""
    objective_id = await agentic.objective()
    need = agentic.need(objective_id)
    items = await agentic.cp.search_products(need.id)
    northwind = next(i for i in items if i.product_id == "prd-northwind-nvme-1tb")
    details = await agentic.cp.product_details(need.id, [northwind])
    product = details.products[0]
    variant = await agentic.cp.select_variant(
        details, product.id, [o.values[0].option_id for o in product.options]
    )
    agentic.engine.inject_error(
        Operation.CREATE_QUOTE, AgenticErrorCode.QUOTE_TEMPORARILY_UNAVAILABLE
    )
    quote = await agentic.cp.request_catalogue_quote(need.id, variant)
    assert (quote.attempt, quote.idempotency_key) == (
        2,
        f"quo:{need.id}:var-northwind-nvme-1tb:2",
    )
    keys = [r.headers["Idempotency-Key"] for r in agentic.wire.sent("POST", "/agentic/quotes")]
    assert keys == [f"quo:{need.id}:var-northwind-nvme-1tb:1", quote.idempotency_key]
    assert agentic.cp.repos.needs.require(need.id)[0].quote_attempts == 2
    again = await agentic.cp.request_catalogue_quote(need.id, variant)
    assert again.attempt == 3


async def test_an_unresolved_merchant_preference_is_dropped_once(agentic: Agentic) -> None:
    """``MERCHANT_NOT_RESOLVED`` searches again without the preference, and says so."""
    objective_id = await agentic.objective()
    spec = PART.need_spec()
    assert spec.search is not None
    search = spec.search.model_copy(
        update={"merchant_preference": SearchPreference(mode="ONLY", merchant_name="Nobody")}
    )
    need = agentic.need(objective_id, spec.model_copy(update={"search": search}))
    items = await agentic.cp.search_products(need.id)
    assert {i.merchant for i in items} == {"Northwind Components", "Kestrel Parts"}
    assert len(agentic.wire.sent("POST", "/agentic/products/search")) == 2
    [searched] = agentic.events(EventType.CATALOGUE_SEARCHED)
    assert searched.payload["merchant_preference_dropped"] is True
    assert searched.refs == {"need": need.id}


async def test_a_lease_cannot_be_bought_on_the_agentic_path(agentic: Agentic) -> None:
    """The card path is dormant: an objective has no card and a lease is not executed."""
    objective_id = await agentic.objective()
    assert agentic.cp.objective(objective_id).reap is None
    offers = await agentic.cp.discover_resources(objective_id, SPEC)
    quote = agentic.cp.request_quote(objective_id, offers[0], Decimal(1))
    intent, _ = agentic.cp.propose_purchase(
        quote_id=quote.id,
        provider=offers[0].provider,
        offer_id=offers[0].offer_id,
        amount_usd=quote.amount_usd,
        rationale="a lease",
        options_considered=[],
    )
    with pytest.raises(ControlPlaneError, match="card path is dormant"):
        await agentic.cp.execute_purchase(intent.id)
    assert not agentic.events(EventType.PURCHASE_CLAIMED)


async def test_the_default_path_calls_no_card_or_simulation_endpoint(
    clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``REAP_PURCHASE_PATH`` unset the purchase is agentic end to end, and only that."""
    monkeypatch.delenv("REAP_PURCHASE_PATH")
    stack, reap = _agentic_plane(clock, purchase_path=None)
    try:
        assert stack.cp.purchase_path == PurchasePath.AGENTIC
        objective_id = await stack.objective()
        intent, _ = stack.propose(await stack.cheapest(stack.need(objective_id)))
        assert (await stack.cp.execute_purchase(intent.id)).state == IntentState.COMPLETED
        paths = {r.url.path for r in stack.wire.seen}
        assert paths
        assert all(p.startswith("/agentic/") for p in paths), paths
        assert not stack.events(EventType.REAP_CARD_ISSUED, EventType.REAP_POLICY_ATTACHED)
    finally:
        await reap.aclose()


def test_the_purchase_path_reads_reap_purchase_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``card`` keeps the dormant card path; anything else unknown is refused."""
    assert purchase_path_from_env() == PurchasePath.CARD
    monkeypatch.setenv("REAP_PURCHASE_PATH", "agentic")
    assert purchase_path_from_env() == PurchasePath.AGENTIC
    monkeypatch.setenv("REAP_PURCHASE_PATH", "cash")
    with pytest.raises(ValidationError):
        purchase_path_from_env()


@pytest.mark.parametrize("name", sorted(PURCHASE_FILES))
async def test_swapping_the_purchase_file_changes_the_product_not_the_code(
    clock: ManualClock, name: str
) -> None:
    """One driver, three files: each reaches a completed order for its own product."""
    purchase = scenario.load_purchase(PURCHASE_FILES[name])
    stack, reap = _agentic_plane(clock, settings=purchase.settings)
    try:
        objective_id = await stack.objective(purchase)
        need = stack.need(objective_id, purchase.need_spec(), estimate="40")
        quotes = await stack.cp.gather_quotes(need.id)
        chosen = best_quote(quotes, need=need, merchants=purchase.merchants)
        assert chosen is not None
        intent, disposition = stack.propose(chosen)
        assert disposition == Disposition.ALLOW
        done = await stack.cp.execute_purchase(intent.id)
        assert done.state == IntentState.COMPLETED
        assert done.order_id is not None
        assert (done.offer_id, done.provider) == SEAM_PRODUCTS[name]
        assert [p.need.spec.shape for p in stack.completed] == [purchase.shape]
    finally:
        await reap.aclose()


async def test_a_checkout_never_sent_fails_after_same_key_retries(agentic: Agentic) -> None:
    """Connection refused four times, backing off 1, 2 and 4 s: failed, nothing moved."""
    objective_id = await agentic.objective()
    intent, _ = agentic.propose(await agentic.cheapest(agentic.need(objective_id)))
    agentic.wire.cut("POST", CHECKOUTS)
    started = agentic.clock()
    failed = await agentic.cp.execute_purchase(intent.id)
    assert (failed.state, failed.decline_code) == (IntentState.FAILED, "NOT_SENT")
    assert agentic.clock() - started == timedelta(seconds=7)
    assert agentic.wire.sent("POST", CHECKOUTS) == []
    assert agentic.runway(objective_id) == PART.grant.budget_usd


async def test_an_approval_left_unanswered_expires_and_costs_nothing(agentic: Agentic) -> None:
    """Reap's page lapses: the checkout is ``EXPIRED``, the intent too, and the money freed."""
    objective_id = await agentic.objective()
    intent, _ = agentic.propose(await agentic.cheapest(agentic.need(objective_id, FAN)))
    agentic.cp.gate.approve(intent.id, "operator")
    assert (await agentic.cp.execute_purchase(intent.id)).state == IntentState.AWAITING_APPROVAL
    agentic.clock.advance(seconds=901)
    expired = await agentic.cp.purchase_status(intent.id)
    assert (expired.state, expired.decline_code) == (IntentState.EXPIRED, "EXPIRED")
    assert agentic.runway(objective_id) == PART.grant.budget_usd
    assert agentic.completed == []


async def test_the_guides_reading_of_the_sandbox_header_also_completes(
    clock: ManualClock,
) -> None:
    """Created ``REQUIRES_ACTION`` under the header, read ``COMPLETED``: an order, no wait."""
    config = AgenticMockConfig(simulated_create=SimulatedCreate.REQUIRES_ACTION)
    stack, reap = _agentic_plane(clock, config=config)
    try:
        objective_id = await stack.objective()
        intent, _ = stack.propose(await stack.cheapest(stack.need(objective_id)))
        done = await stack.cp.execute_purchase(intent.id)
        assert done.state == IntentState.COMPLETED
        [created] = stack.events(EventType.CHECKOUT_CREATED)
        assert (created.payload["status"], created.payload["simulated"]) == (
            "REQUIRES_ACTION",
            True,
        )
        assert not stack.events(EventType.CHECKOUT_AWAITING_APPROVAL)
    finally:
        await reap.aclose()


async def test_a_header_the_sandbox_ignores_is_waited_out_then_awaits_approval(
    agentic: Agentic,
) -> None:
    """Sent with the header, still ``REQUIRES_ACTION`` after 10 s: handled as awaiting approval."""
    agentic.wire.ignored.add(SIMULATE_CHECKOUT_HEADER)
    objective_id = await agentic.objective()
    intent, _ = agentic.propose(await agentic.cheapest(agentic.need(objective_id)))
    started = agentic.clock()
    waiting = await agentic.cp.execute_purchase(intent.id)
    assert waiting.state == IntentState.AWAITING_APPROVAL
    assert agentic.clock() - started >= timedelta(seconds=PART.settings.simulated_grace_s)
    order = agentic.cp.repos.orders.require(waiting.checkout_id or "")[0]
    assert order.simulated is True
    assert len(agentic.wire.sent("GET", f"{CHECKOUTS}/{waiting.checkout_id}")) > 1


async def test_the_runway_counts_what_the_checkout_charged(agentic: Agentic) -> None:
    """A final amount above the quote is what counts, and the difference is recorded."""
    objective_id = await agentic.objective()
    chosen = await agentic.cheapest(agentic.need(objective_id))
    intent, _ = agentic.propose(chosen)
    agentic.engine.override_final_amount(chosen.reap_quote_id, Decimal(80))
    done = await agentic.cp.execute_purchase(intent.id)
    assert done.final_amount_usd == Decimal(80)
    assert agentic.runway(objective_id) == PART.grant.budget_usd - Decimal(80)
    [mismatch] = agentic.events(EventType.ORDER_AMOUNT_MISMATCH)
    assert Decimal(str(mismatch.payload["difference"])) == Decimal(3)


async def test_a_refused_candidate_is_skipped_and_a_wrong_address_surfaces(
    agentic: Agentic,
) -> None:
    """``VARIANT_UNAVAILABLE`` drops one candidate; ``INVALID_PHONE`` is the file's to fix."""
    objective_id = await agentic.objective()
    need = agentic.need(objective_id)
    agentic.engine.inject_error(Operation.CREATE_QUOTE, AgenticErrorCode.VARIANT_UNAVAILABLE)
    assert len(await agentic.cp.gather_quotes(need.id)) == 3
    agentic.engine.inject_error(
        Operation.CREATE_QUOTE,
        AgenticErrorCode.QUOTE_UNFULFILLABLE,
        detail={"reason": AgenticErrorCode.INVALID_PHONE},
    )
    with pytest.raises(ReapError, match="QUOTE_UNFULFILLABLE"):
        await agentic.cp.gather_quotes(need.id)


SEAM_PRODUCTS = {
    "compute": ("var-northwind-gpu-credits-50", "Northwind Cloud"),
    "part": ("var-northwind-nvme-1tb", "Northwind Components"),
    "checkout_url": ("https://merchant.example/cart/var_123:1", "Example Merchant"),
}
CARD_FIELDS = ("number", "cvc", "expiry", "otp")
TEST_CARD = ("4622 9431 2313 7797", "640", "12/27", TEST_OTP)

"""The control plane's tool contracts: what the agent and the MCP server call.

Every method either reads state or moves the loop one step:

1. The objective and its authority are set up, and bound to the operator's agentic
   enrolment (or, on the dormant card path, to a card with floor policies).
2. A need is raised and searched for in Reap's catalogue: search, details, variant, and
   a quote whose landed ``finalAmount`` the gate decides on. The dormant card path
   discovers and quotes leases instead.
3. A purchase is proposed, decided and executed: the gate's claim, then
   ``POST /agentic/checkouts`` under the claim's idempotency key, then reads of the
   checkout until it completes, fails, expires or waits on Reap's approval page.
4. A completed order hands over to the shape's follow-through (capacity for compute, a
   technician's ticket for a part); on the card path capacity is provisioned, kept or
   released by the lifecycle rules, and release clears the metered amount.

Each step lands on the ledger with refs that the provenance graph is drawn from. No
decision about money is made here; the gate makes it.

``REAP_PURCHASE_PATH`` chooses the path: ``agentic`` (the default) or ``card``, which
keeps the dormant card path. On the agentic path no card or simulation endpoint is
called.

Errors on a checkout create branch on ``error.code``: a request never sent is retried
under the same key, then failed; an answer lost after sending is replayed under the same
key, then ``outcome_unknown``; a definite refusal fails the attempt. Choices made where
Reap's docs are silent:

* ``AGENTIC_SERVICE_UNAVAILABLE``, and any other 5xx code except
  ``CHECKOUT_TEMPORARILY_UNAVAILABLE``, on a checkout create is ``outcome_unknown``; any
  other 4xx code (a 429 the client gave up on included, being uncached and unprocessed)
  is a failed attempt.
* A checkout that cannot be read until the poll's deadline is ``outcome_unknown`` with
  its id kept, and ``purchase_status`` reconciles it by reading it again.
* The enrolment is owned by the client reference ``operator`` (the operator of record)
  and keyed ``enr:<objective id>``; ``reap.enrolled`` is appended when the enrolment is
  first bound and whenever a read finds its status changed.
* Quote attempts are counted on the need and persisted before each request, so a quote
  key (``quo:<need>:<variant>:<attempt>``) is never reused after a cached ``503``; a
  checkout-URL quote's key names ``url`` in place of a variant.
* A checkout-URL quote is recorded against a variant whose id is the cart URL, from the
  configured merchant, priced at the items subtotal, quantity one: Reap's quote does
  not list the cart's items.
* ``gather_quotes`` resolves at most ``max_variants_per_product`` option combinations
  per product, each value available and, for a need judged by attributes, accepted; it
  skips a candidate answered ``VARIANT_UNAVAILABLE``, ``CARD_PAYMENT_UNAVAILABLE``,
  ``QUOTE_TEMPORARILY_UNAVAILABLE`` or ``QUOTE_UNFULFILLABLE`` with reason
  ``ITEMS_UNSHIPPABLE``, and lets any other refusal surface.
* A raw response's hash is SHA-256 over its canonical wire JSON, since the client
  returns parsed models rather than bytes.
"""

import asyncio
import contextlib
import hashlib
import itertools
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import JsonValue, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from youreapyousow.authority.floor import policies_for
from youreapyousow.authority.gate import AuthorityGate, ExternalAuthorisation
from youreapyousow.authority.grants import AuthorityGrant, validate_grant
from youreapyousow.authority.lifecycle import LifecycleDecision, LifecycleRules, evaluate_deployment
from youreapyousow.authority.rules import NeedJudge
from youreapyousow.clock import Clock, utc_now
from youreapyousow.domain import (
    BUDGET_CURRENCY,
    CatalogueItem,
    CatalogueVariant,
    Constraint,
    Deployment,
    DeploymentStatus,
    Enrolment,
    IntentState,
    LandedQuote,
    Objective,
    ObjectiveKind,
    ObjectiveStatus,
    Observation,
    Offer,
    Order,
    PolicyDecision,
    Price,
    PurchaseIntent,
    Quote,
    ReapBinding,
    ResourceKind,
    ResourceSpec,
    usd,
)
from youreapyousow.ids import new_id
from youreapyousow.ledger.events import EventType
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.ledger.provenance import ProvenanceGraph, build_graph
from youreapyousow.market.provisioning import Provisioner, metered_usd
from youreapyousow.market.service import MarketService
from youreapyousow.merchants import merchant_for
from youreapyousow.procure.need import (
    AttributeMatch,
    Need,
    NeedSpec,
    Route,
    Shape,
    judge_variant,
    need_judge,
)
from youreapyousow.procure.selection import in_scope
from youreapyousow.reap.client import (
    CheckoutPollTimeoutError,
    ReapClient,
    ReapError,
    ReapTransportError,
)
from youreapyousow.reap.mock.agentic import TEST_CARDS, TEST_OTP, AgenticMockEngine
from youreapyousow.reap.models import (
    AgenticErrorCode,
    CardAuthorizationRequest,
    Checkout,
    CheckoutCreated,
    CheckoutStatus,
    ClientReferenceOwner,
    CreateAccountRequest,
    CreateCardRequest,
    CreateCheckoutRequest,
    CreateExternalCheckoutQuoteRequest,
    CreateExternalEnrollmentRequest,
    CreateItemsQuoteRequest,
    CreateQuoteRequest,
    CreateUserRequest,
    DeclinedCardTransaction,
    Enrollment,
    EnrollmentStatus,
    ExternalAuthDecision,
    ExternalCheckout,
    Presentation,
    ProductDetail,
    ProductDetailsRequest,
    ProductError,
    QuoteItem,
    ResolveVariantRequest,
    SimulateAuthorizationRequest,
    SimulateCheckout,
    SimulateClearingRequest,
    SimulatedMerchant,
    SimulateFiatDepositRequest,
    WebhookEvent,
    Wire,
)
from youreapyousow.repos import ObjectiveLifecycle, ReapOperator, Repositories

OPERATOR_KEY = "operator"


class ControlPlaneError(RuntimeError):
    """Raised when a tool is called out of order or on a missing record."""


@dataclass(frozen=True)
class GrantTerms:
    """The operator's authority for a new objective, before ids and times are assigned.

    Attributes:
        allowed_providers: Providers the agent may buy from.
        per_transaction_cap_usd: Largest single purchase.
        daily_cap_usd: Largest total per UTC day.
        ttl: How long the authority lasts.
        max_price_usd_per_hour: Highest acceptable hourly rate, if capped.
        approval_threshold_usd: Above this, purchases wait for the operator.
        allowed_kinds: Resource kinds the agent may buy.
        allowed_merchants: The merchant scope on the agentic path.
        attempts_per_need: Attempts at one need that may end failed, expired or declined.
        quote_margin_s: How long before Reap's ``expiresAt`` a quote stops being bought.
    """

    allowed_providers: tuple[str, ...]
    per_transaction_cap_usd: Decimal
    daily_cap_usd: Decimal
    ttl: timedelta
    max_price_usd_per_hour: Decimal | None = None
    approval_threshold_usd: Decimal | None = None
    allowed_kinds: tuple[ResourceKind, ...] = (ResourceKind.GPU_COMPUTE,)
    allowed_merchants: tuple[str, ...] = ()
    attempts_per_need: int = 3
    quote_margin_s: int = 15


@dataclass(frozen=True)
class AgenticSettings:
    """How the agentic path enrols, checks out and waits.

    Attributes:
        return_url: The https URI Reap sends the browser to after a hosted step.
        simulate_completed_when_allowed: Send the sandbox's ``X-Simulate-Checkout:
            COMPLETED`` on checkouts the gate allowed without the operator; never on an
            approved escalation.
        poll_every_s: The first wait between checkout reads.
        poll_deadline_s: How long a checkout may stay ``PROCESSING`` before its outcome is
            unknown.
        poll_max_every_s: The longest wait between checkout reads.
        create_replays: Same-key replays of a checkout create whose answer was lost.
        connect_retries: Same-key retries of a checkout create that was never sent.
        backoff_s: The wait before the first replay or retry, doubled for each one after.
        simulated_grace_s: How long a checkout sent with the sandbox header may still read
            ``REQUIRES_ACTION`` before it is handled as awaiting approval.
        max_variants_per_product: The most option combinations resolved per product.
    """

    return_url: str = "https://example.invalid/checkout/return"
    simulate_completed_when_allowed: bool = True
    poll_every_s: float = 1.0
    poll_deadline_s: float = 120.0
    poll_max_every_s: float = 5.0
    create_replays: int = 2
    connect_retries: int = 3
    backoff_s: float = 1.0
    simulated_grace_s: float = 10.0
    max_variants_per_product: int = 8


@dataclass(frozen=True)
class Operator:
    """The human cardholder of record behind every objective's card.

    Attributes:
        email: Email for the Reap user.
        phone: Phone for the Reap user.
        first_name: Given name.
        last_name: Family name.
    """

    email: str
    phone: str
    first_name: str = "Operator"
    last_name: str = "Of Record"


class PurchasePath(StrEnum):
    """Which purchase path is live (``REAP_PURCHASE_PATH``)."""

    AGENTIC = "agentic"
    """Reap's Agentic module: catalogue, landed quote, checkout. The default."""
    CARD = "card"
    """The dormant card path: a card per objective, floor policies, the simulator."""


class _PurchasePathSetting(BaseSettings):
    """``REAP_PURCHASE_PATH``, from the environment or ``.env``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    reap_purchase_path: PurchasePath = PurchasePath.AGENTIC


def purchase_path_from_env() -> PurchasePath:
    """Read ``REAP_PURCHASE_PATH``.

    Returns:
        The path; agentic when unset.

    Raises:
        ValidationError: If it names neither ``agentic`` nor ``card``.
    """
    return _PurchasePathSetting().reap_purchase_path


@dataclass(frozen=True)
class CompletedPurchase:
    """A completed order, as handed to the shape's follow-through.

    Attributes:
        intent: The completed intent.
        order: The checkout's order record: order id and final amount.
        need: The need it filled.
    """

    intent: PurchaseIntent
    order: Order
    need: Need


type FollowThrough = Callable[[CompletedPurchase], Awaitable[None]]
"""What follows a completed order for one shape: capacity, or a technician's ticket.

It is called once when the control plane records the completion, and again by
``follow_through``; it must be idempotent per intent. An exception it raises reaches the
caller after the completion is recorded, so the order is never lost to a failed hook.
"""

type CardEntry = Callable[[str], Awaitable[None]]
"""Completes an enrolment's hosted card step by its id. Only the mock has one: on the
sandbox a person enters the card."""


@dataclass(frozen=True)
class CatalogueDetails:
    """Products expanded by ``POST /agentic/products/details``.

    Attributes:
        id: Our identifier for the reading, the ``catalogue.detailed`` subject.
        need_id: The need they were read for.
        search_id: The search the products came from.
        products: The products with their options and default variants.
        errors: The ids that did not resolve.
        merchants: Each product's merchant, by product id.
    """

    id: str
    need_id: str
    search_id: str
    products: tuple[ProductDetail, ...]
    errors: tuple[ProductError, ...]
    merchants: Mapping[str, str]


def mock_card_entry(engine: AgenticMockEngine) -> CardEntry:
    """Complete enrolments on the mock with the first published test card.

    Args:
        engine: The agentic mock engine.

    Returns:
        The card step, entering the card and the one-time password on the mock's page.
    """
    number, (cvc, expiry) = next(iter(TEST_CARDS.items()))

    async def enter(enrollment_id: str) -> None:
        engine.submit_card(enrollment_id, number=number, cvc=cvc, expiry=expiry, otp=TEST_OTP)

    return enter


def need_judge_for(repos: Repositories) -> NeedJudge:
    """Build the gate's rule 10 over the needs the control plane raised.

    Args:
        repos: The repositories holding the needs.

    Returns:
        The judge to give ``AuthorityGate``.
    """

    def lookup(need_id: str) -> Need | None:
        found = repos.needs.get(need_id)
        return found[0] if found else None

    return need_judge(lookup)


def _raw_hash(response: Wire) -> str:
    canonical = json.dumps(response.to_wire(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _price(price: Price) -> dict[str, JsonValue]:
    return {"amount": str(price.amount), "currency": price.currency}


_SKIPPED_QUOTE_CODES = frozenset(
    {
        AgenticErrorCode.VARIANT_UNAVAILABLE,
        AgenticErrorCode.CARD_PAYMENT_UNAVAILABLE,
        AgenticErrorCode.QUOTE_TEMPORARILY_UNAVAILABLE,
    }
)
"""Quote refusals that rule out one candidate and leave the others."""

_ENROLMENT_CODES = frozenset(
    {AgenticErrorCode.ENROLLMENT_NOT_ACTIVE, AgenticErrorCode.ENROLLMENT_NOT_FOUND}
)


def _skips_candidate(error: ReapError) -> bool:
    if error.code in _SKIPPED_QUOTE_CODES:
        return True
    reason = (error.detail or {}).get("reason")
    return (
        error.code == AgenticErrorCode.QUOTE_UNFULFILLABLE
        and reason == AgenticErrorCode.ITEMS_UNSHIPPABLE
    )


def _maybe_charged(error: ReapError) -> bool:
    """Whether a refused checkout create could still have opened a payment.

    Args:
        error: Reap's answer.

    Returns:
        True for ``AGENTIC_SERVICE_UNAVAILABLE`` and any 5xx but the documented
        ``CHECKOUT_TEMPORARILY_UNAVAILABLE``.
    """
    if error.code == AgenticErrorCode.AGENTIC_SERVICE_UNAVAILABLE:
        return True
    return error.status >= 500 and error.code != AgenticErrorCode.CHECKOUT_TEMPORARILY_UNAVAILABLE


class ControlPlane:
    """The tool surface over the gate, Reap, the market, provisioning and the ledger."""

    def __init__(
        self,
        *,
        repos: Repositories,
        ledger: Ledger,
        gate: AuthorityGate,
        reap: ReapClient,
        market: MarketService,
        provisioner: Provisioner,
        operator: Operator,
        clock: Clock = utc_now,
        quote_ttl: timedelta = timedelta(seconds=60),
        purchase_path: PurchasePath | None = None,
        settings: AgenticSettings | None = None,
        card_entry: CardEntry | None = None,
        follow_through: Mapping[Shape, FollowThrough] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wire the control plane.

        Args:
            repos: Record repositories.
            ledger: The ledger.
            gate: The authority gate; on the agentic path built with
                ``need_judge_for(repos)``, or every catalogue purchase fails rule 10.
            reap: The Reap backend (mock or sandbox).
            market: Price discovery.
            provisioner: Capacity provisioning.
            operator: The operator of record: the cardholder on the card path, the
                enrolment's owner on the agentic path.
            clock: Time source.
            quote_ttl: How long a lease quote may be purchased (card path).
            purchase_path: The live path; ``REAP_PURCHASE_PATH`` when None.
            settings: How the agentic path enrols, checks out and waits.
            card_entry: Completes an enrolment's hosted card step; the mock's only.
            follow_through: What follows a completed order, per shape.
            sleep: How back-off waits; replaced in tests.
        """
        self.repos = repos
        self.ledger = ledger
        self.gate = gate
        self.reap = reap
        self.market = market
        self.provisioner = provisioner
        self.purchase_path = purchase_path or purchase_path_from_env()
        self.settings = settings or AgenticSettings()
        self._operator = operator
        self._clock = clock
        self._quote_ttl = quote_ttl
        self._card_entry = card_entry
        self._follow_through: Mapping[Shape, FollowThrough] = follow_through or {}
        self._sleep = sleep

    # Setup

    async def _reap_operator(self) -> ReapOperator:
        existing = self.repos.reap_operator.get(OPERATOR_KEY)
        if existing is not None:
            return existing[0]
        user = await self.reap.create_user(
            CreateUserRequest(
                email=self._operator.email,
                phone_number=self._operator.phone,
                first_name=self._operator.first_name,
                last_name=self._operator.last_name,
                external_id=OPERATOR_KEY,
            )
        )
        await self.reap.simulate_user_application(user.id, "APPROVED")
        account = await self.reap.create_account(
            CreateAccountRequest(owner_id=user.id, owner_type="USER"),
            idempotency_key=f"account:{user.id}",
        )
        operator = ReapOperator(user_id=user.id, account_id=account.id)
        self.repos.reap_operator.insert(operator, record_id=OPERATOR_KEY, objective_id=None)
        return operator

    async def create_objective(
        self,
        *,
        kind: ObjectiveKind,
        statement: str,
        constraints: list[Constraint],
        budget_usd: Decimal,
        grant: GrantTerms,
        lifecycle: LifecycleRules,
        enrollment_id: str | None = None,
    ) -> Objective:
        """Create an objective and its authority, bound to how it pays.

        On the agentic path the objective is bound to an agentic enrolment
        (``bind_enrolment``); on the card path it gets its own Reap card with the floor
        policies.

        Args:
            kind: Which scenario.
            statement: The objective in words.
            constraints: What success means.
            budget_usd: Total money the objective may commit.
            grant: The operator's authority terms.
            lifecycle: Rollback and termination settings.
            enrollment_id: The operator's completed enrolment, on the agentic path; a
                new one is created when None.

        Returns:
            The objective, bound to its enrolment or its Reap card.
        """
        now = self._clock()
        objective = Objective(
            id=new_id("obj"),
            kind=kind,
            statement=statement,
            constraints=constraints,
            budget_usd=budget_usd,
            created_at=now,
        )
        authority = AuthorityGrant(
            id=new_id("grt"),
            objective_id=objective.id,
            allowed_providers=list(grant.allowed_providers),
            allowed_kinds=list(grant.allowed_kinds),
            per_transaction_cap_usd=grant.per_transaction_cap_usd,
            daily_cap_usd=grant.daily_cap_usd,
            max_price_usd_per_hour=grant.max_price_usd_per_hour,
            approval_threshold_usd=grant.approval_threshold_usd,
            issued_at=now,
            expires_at=now + grant.ttl,
            allowed_merchants=list(grant.allowed_merchants),
            attempts_per_need=grant.attempts_per_need,
            quote_margin_s=grant.quote_margin_s,
        )
        validate_grant(authority, budget_usd)
        with self.repos.db.transaction():
            self.repos.objectives.insert(
                objective, record_id=objective.id, objective_id=objective.id
            )
            self.repos.lifecycles.insert(
                ObjectiveLifecycle(objective_id=objective.id, rules=lifecycle),
                record_id=objective.id,
                objective_id=objective.id,
            )
            self.ledger.append(
                EventType.OBJECTIVE_CREATED,
                subject_id=objective.id,
                objective_id=objective.id,
                payload=objective.model_dump(mode="json")
                | {"lifecycle": lifecycle.model_dump(mode="json")},
            )
        self.gate.issue_grant(authority)
        if self.purchase_path == PurchasePath.CARD:
            return await self._bind_card(objective, authority)
        return await self.bind_enrolment(objective.id, enrollment_id=enrollment_id)

    async def bind_enrolment(
        self,
        objective_id: str,
        *,
        enrollment_id: str | None = None,
        completed_by: str = "operator",
    ) -> Objective:
        """Bind the objective to the agentic enrolment its purchases are charged to.

        With an id, the operator's completed enrolment is read and recorded (the
        operator enters the test card on Reap's hosted page by hand). Without one an
        ``EXTERNAL`` enrolment is created; the mock's card step completes it, and on the
        sandbox it waits on its hosted page until a person does and it is read again.

        Args:
            objective_id: The objective.
            enrollment_id: An existing enrolment, or None to create one.
            completed_by: Who completed the hosted step of an existing enrolment.

        Returns:
            The objective, with its enrolment as just read.
        """
        if enrollment_id is None:
            created = await self.reap.create_enrollment(
                CreateExternalEnrollmentRequest(
                    owner=ClientReferenceOwner(id=OPERATOR_KEY, email=self._operator.email),
                    presentation=Presentation(return_url=self.settings.return_url),
                ),
                idempotency_key=f"enr:{objective_id}",
            )
            enrollment_id = created.id
            if created.status == EnrollmentStatus.REQUIRES_ACTION and self._card_entry is not None:
                await self._card_entry(created.id)
                completed_by = f"the {self.reap.backend} card step with a published test card"
        enrollment = await self.reap.get_enrollment(enrollment_id)
        return self._record_enrolment(objective_id, enrollment, completed_by=completed_by)

    async def refresh_enrolment(self, objective_id: str) -> Objective:
        """Read the objective's enrolment again, so rule 6 sees its current status.

        Args:
            objective_id: The objective.

        Returns:
            The objective, with its enrolment as just read.

        Raises:
            ControlPlaneError: If the objective has no enrolment.
        """
        current = self.objective(objective_id).enrolment
        if current is None:
            raise ControlPlaneError(f"objective {objective_id} has no enrolment")
        enrollment = await self.reap.get_enrollment(current.id)
        return self._record_enrolment(objective_id, enrollment, completed_by="operator")

    def _record_enrolment(
        self, objective_id: str, enrollment: Enrollment, *, completed_by: str
    ) -> Objective:
        enrolment = Enrolment.from_reap(enrollment, read_at=self._clock())
        with self.repos.db.transaction():
            objective, version = self.repos.objectives.require(objective_id)
            previous = objective.enrolment
            bound = objective.model_copy(update={"enrolment": enrolment})
            self.repos.objectives.update(bound, record_id=objective_id, expected_version=version)
            if previous is None or (previous.id, previous.status) != (
                enrolment.id,
                enrolment.status,
            ):
                self.ledger.append(
                    EventType.REAP_ENROLLED,
                    subject_id=enrolment.id,
                    objective_id=objective_id,
                    refs={"objective": objective_id},
                    payload={
                        "status": enrolment.status.value,
                        "network": enrolment.network,
                        "last4": enrolment.last4,
                        "completed_by": completed_by
                        if enrolment.status == EnrollmentStatus.ACTIVE
                        else None,
                        "backend": self.reap.backend,
                    },
                )
        return bound

    async def _bind_card(self, objective: Objective, grant: AuthorityGrant) -> Objective:
        operator = await self._reap_operator()
        await self.reap.simulate_fiat_deposit(
            SimulateFiatDepositRequest(
                amount=objective.budget_usd, currency="USD", reference=objective.id
            )
        )
        card = await self.reap.create_card(
            CreateCardRequest(
                user_id=operator.user_id, account_id=operator.account_id, type="VIRTUAL"
            ),
            idempotency_key=f"card:{objective.id}",
        )
        self.ledger.append(
            EventType.REAP_CARD_ISSUED,
            subject_id=card.id,
            objective_id=objective.id,
            payload={"last4": card.last4, "backend": self.reap.backend},
        )
        policy_ids: list[str] = []
        for index, spec in enumerate(
            policies_for(grant, budget_usd=objective.budget_usd, card_id=card.id)
        ):
            policy = await self.reap.create_policy(
                spec, idempotency_key=f"policy:{objective.id}:{index}"
            )
            policy_ids.append(policy.id)
            self.ledger.append(
                EventType.REAP_POLICY_ATTACHED,
                subject_id=policy.id,
                objective_id=objective.id,
                refs={"card": card.id},
                payload={"name": policy.name, "type": policy.type},
            )
        bound = objective.model_copy(
            update={
                "reap": ReapBinding(
                    user_id=operator.user_id,
                    account_id=operator.account_id,
                    card_id=card.id,
                    policy_ids=policy_ids,
                )
            }
        )
        _, version = self.repos.objectives.require(objective.id)
        self.repos.objectives.update(bound, record_id=objective.id, expected_version=version)
        return bound

    def objective(self, objective_id: str) -> Objective:
        """Read an objective.

        Args:
            objective_id: The objective.

        Returns:
            The objective.
        """
        return self.repos.objectives.require(objective_id)[0]

    # Observe

    def record_observation(
        self,
        objective_id: str,
        *,
        metric: str,
        value: Decimal,
        unit: str = "",
        deployment_id: str | None = None,
        source: str = "simulated",
        at: datetime | None = None,
    ) -> Observation:
        """Record one measured value.

        Args:
            objective_id: The objective it informs.
            metric: Metric name.
            value: The measurement.
            unit: Unit for display.
            deployment_id: The deployment it concerns, if any.
            source: ``simulated`` or ``live``.
            at: When it was measured; now when not given. Every metric of one reading
                must share its instant: readers group a reading's metrics by it.

        Returns:
            The stored observation.
        """
        observation = Observation(
            id=new_id("obs"),
            objective_id=objective_id,
            deployment_id=deployment_id,
            metric=metric,
            value=value,
            unit=unit,
            at=at or self._clock(),
            source=source,
        )
        with self.repos.db.transaction():
            self.repos.observations.insert(
                observation, record_id=observation.id, objective_id=objective_id
            )
            self.ledger.append(
                EventType.OBSERVATION_RECORDED,
                subject_id=observation.id,
                objective_id=objective_id,
                refs={"deployment": deployment_id} if deployment_id else {},
                payload={"metric": metric, "value": str(value), "unit": unit, "source": source},
            )
        return observation

    def observations(self, objective_id: str) -> list[Observation]:
        """List an objective's observations, oldest first.

        Args:
            objective_id: The objective.

        Returns:
            The observations.
        """
        return self.repos.observations.list(objective_id=objective_id)

    # Discover and quote

    async def discover_resources(self, objective_id: str, spec: ResourceSpec) -> list[Offer]:
        """Query the market and record every option considered.

        Args:
            objective_id: The objective the search serves.
            spec: What is needed.

        Returns:
            Matching offers, cheapest first, each with its source badge.
        """
        offers = await self.market.discover_resources(spec)
        options: list[JsonValue] = [
            {
                "key": o.key,
                "provider": o.provider,
                "gpu": o.gpu_name,
                "vram_gb": o.vram_gb,
                "usd_per_hour": str(o.price_usd_per_hour),
                "source": o.source.value,
                "fetched_at": o.fetched_at.isoformat(),
                "latency_ms": o.latency_ms,
                "raw_ref": o.raw_ref,
            }
            for o in offers
        ]
        self.ledger.append(
            EventType.MARKET_DISCOVERED,
            subject_id=new_id("dsc"),
            objective_id=objective_id,
            payload={"spec": spec.model_dump(mode="json"), "options": options},
        )
        return offers

    def request_quote(self, objective_id: str, offer: Offer, hours: Decimal) -> Quote:
        """Commit a price for leasing an offer, valid for the quote TTL.

        Args:
            objective_id: The objective.
            offer: The offer, as discovered.
            hours: Lease length.

        Returns:
            The quote.
        """
        now = self._clock()
        quote = Quote(
            id=new_id("quo"),
            objective_id=objective_id,
            offer=offer,
            hours=hours,
            amount_usd=usd(offer.price_usd_per_hour * hours),
            created_at=now,
            expires_at=now + self._quote_ttl,
        )
        discovery = self.ledger.events(
            objective_id=objective_id, types=[EventType.MARKET_DISCOVERED]
        )
        refs = {"offer": offer.key}
        if discovery:
            refs["discovery"] = discovery[-1].subject_id
        with self.repos.db.transaction():
            self.repos.quotes.insert(quote, record_id=quote.id, objective_id=objective_id)
            self.ledger.append(
                EventType.QUOTE_CREATED,
                subject_id=quote.id,
                objective_id=objective_id,
                refs=refs,
                payload={
                    "hours": str(hours),
                    "amount_usd": str(quote.amount_usd),
                    "expires_at": quote.expires_at.isoformat(),
                },
            )
        return quote

    # Need, catalogue and landed quote (the agentic path)

    def raise_need(
        self,
        objective_id: str,
        spec: NeedSpec,
        *,
        reason: str,
        estimate_usd: Decimal | None = None,
        refs: Mapping[str, str] | None = None,
    ) -> Need:
        """Raise a need: one thing the objective must buy, and why.

        Args:
            objective_id: The objective.
            spec: What to buy and how, from the ``purchase:`` block.
            reason: Why, in words: the breach or the fault.
            estimate_usd: Shape (a): the estimated lease the item must cover.
            refs: What raised it, such as ``{"observation": id}`` or ``{"fault": id}``.

        Returns:
            The need.
        """
        self.objective(objective_id)
        need = Need(
            id=new_id("need"),
            objective_id=objective_id,
            spec=spec,
            reason=reason,
            estimate_usd=estimate_usd,
            raised_at=self._clock(),
        )
        with self.repos.db.transaction():
            self.repos.needs.insert(need, record_id=need.id, objective_id=objective_id)
            self.ledger.append(
                EventType.NEED_RAISED,
                subject_id=need.id,
                objective_id=objective_id,
                refs=dict(refs or {}),
                payload={
                    "shape": spec.shape.value,
                    "route": spec.route.value,
                    "what": need.what,
                    "reason": reason,
                    "estimate_usd": str(estimate_usd) if estimate_usd is not None else None,
                    "quantity": spec.quantity,
                    "match": spec.match.model_dump(mode="json") if spec.match else None,
                },
            )
        return need

    def need(self, need_id: str) -> Need:
        """Read a need.

        Args:
            need_id: The need.

        Returns:
            The need, with its quote attempts so far.
        """
        return self.repos.needs.require(need_id)[0]

    async def search_products(self, need_id: str) -> list[CatalogueItem]:
        """Search Reap's catalogue for a need, and record every result.

        ``MERCHANT_NOT_RESOLVED`` searches once more without the merchant preference.

        Args:
            need_id: A need on the catalogue route.

        Returns:
            The products found, each with its merchant and price range.

        Raises:
            ControlPlaneError: If the need has no catalogue search.
        """
        need = self.need(need_id)
        if need.spec.search is None:
            raise ControlPlaneError(f"need {need_id} is not searched for in the catalogue")
        request = need.spec.search.request()
        dropped = False
        try:
            response = await self.reap.search_products(request)
        except ReapError as error:
            if (
                error.code != AgenticErrorCode.MERCHANT_NOT_RESOLVED
                or request.merchant_preference is None
            ):
                raise
            request = request.model_copy(update={"merchant_preference": None})
            dropped = True
            response = await self.reap.search_products(request)
        items = [CatalogueItem.from_reap(p, search_id=response.id) for p in response.products]
        self.ledger.append(
            EventType.CATALOGUE_SEARCHED,
            subject_id=response.id,
            objective_id=need.objective_id,
            refs={"need": need_id},
            payload={
                "request": request.to_wire(),
                "merchant_preference_dropped": dropped,
                "products": [
                    {
                        "id": i.product_id,
                        "merchant": i.merchant,
                        "name": i.name,
                        "price_min": _price(i.price_min),
                        "price_max": _price(i.price_max),
                        "available": i.available,
                        "preview_variant": i.preview_variant_id,
                    }
                    for i in items
                ],
                "warnings": list(response.warnings),
                "raw_sha256": _raw_hash(response),
            },
        )
        return items

    async def product_details(
        self, need_id: str, items: Sequence[CatalogueItem]
    ) -> CatalogueDetails:
        """Expand search results into their options and default variants.

        Args:
            need_id: The need they were found for.
            items: One to ten results of the same search.

        Returns:
            The details, with each product's merchant.

        Raises:
            ControlPlaneError: If the items are none, more than ten, or from two searches.
        """
        need = self.need(need_id)
        ids = list(dict.fromkeys(i.product_id for i in items))
        searches = {i.search_id for i in items}
        if not ids or len(ids) > 10 or len(searches) != 1:
            raise ControlPlaneError("details need 1 to 10 products from one search")
        response = await self.reap.product_details(ProductDetailsRequest(product_ids=ids))
        found = {i.product_id: i.merchant for i in items}
        merchants = {
            p.id: p.merchant.name if p.merchant else found[p.id] for p in response.products
        }
        details = CatalogueDetails(
            id=new_id("dtl"),
            need_id=need_id,
            search_id=searches.pop(),
            products=tuple(response.products),
            errors=tuple(response.errors),
            merchants=merchants,
        )
        self.ledger.append(
            EventType.CATALOGUE_DETAILED,
            subject_id=details.id,
            objective_id=need.objective_id,
            refs={"search": details.search_id, "need": need_id},
            payload={
                "products": [
                    {
                        "id": p.id,
                        "merchant": merchants[p.id],
                        "name": p.name,
                        "options": {o.name: [v.label for v in o.values] for o in p.options},
                        "default_variant": p.default_variant.id,
                    }
                    for p in response.products
                ],
                "errors": [e.to_wire() for e in response.errors],
                "raw_sha256": _raw_hash(response),
            },
        )
        return details

    async def select_variant(
        self, details: CatalogueDetails, product_id: str, option_ids: Sequence[str]
    ) -> CatalogueVariant:
        """Resolve chosen options to the variant a quote accepts.

        Args:
            details: The details the product came from.
            product_id: The product.
            option_ids: One option id per option group.

        Returns:
            The variant, with its options by name and its unit price.

        Raises:
            ControlPlaneError: If the product is not in the details.
        """
        if product_id not in details.merchants:
            raise ControlPlaneError(f"product {product_id} is not in details {details.id}")
        need = self.need(details.need_id)
        response = await self.reap.resolve_variant(
            ResolveVariantRequest(product_id=product_id, option_ids=list(option_ids))
        )
        variant = CatalogueVariant.from_reap(
            response, product_id=product_id, merchant=details.merchants[product_id]
        )
        self.ledger.append(
            EventType.CATALOGUE_VARIANT_RESOLVED,
            subject_id=variant.id,
            objective_id=need.objective_id,
            refs={"product_details": details.id, "need": need.id},
            payload={
                "product_id": product_id,
                "option_ids": list(option_ids),
                "merchant": variant.merchant,
                "options": dict(variant.options),
                "price": _price(variant.price),
                "available": variant.available,
                "requires_shipping": variant.requires_shipping,
            },
        )
        return variant

    def _next_quote_attempt(self, need_id: str) -> int:
        with self.repos.db.transaction():
            need, version = self.repos.needs.require(need_id)
            attempt = need.quote_attempts + 1
            self.repos.needs.update(
                need.model_copy(update={"quote_attempts": attempt}),
                record_id=need_id,
                expected_version=version,
            )
        return attempt

    def _quote_request(self, need: Need, variant: CatalogueVariant | None) -> CreateQuoteRequest:
        spec = need.spec
        if spec.route == Route.CHECKOUT_URL:
            cart, address = spec.external_checkout, spec.shipping_address
            if variant is not None or cart is None or address is None:
                raise ControlPlaneError(f"need {need.id} quotes its configured cart, no variant")
            return CreateExternalCheckoutQuoteRequest(
                email=spec.email,
                external_checkout=ExternalCheckout(
                    merchant_domain=cart.merchant_domain, checkout_url=cart.checkout_url
                ),
                shipping_address=address,
            )
        if variant is None:
            raise ControlPlaneError(f"need {need.id} quotes a catalogue variant")
        ships = variant.requires_shipping is not False
        if variant.requires_shipping and spec.shipping_address is None:
            raise ControlPlaneError(
                f"{variant.key} ships, and the purchase block names no shipping_address"
            )
        return CreateItemsQuoteRequest(
            email=spec.email,
            items=[QuoteItem(variant_id=variant.id, quantity=spec.quantity)],
            shipping_address=spec.shipping_address if ships else None,
        )

    async def request_catalogue_quote(
        self, need_id: str, variant: CatalogueVariant | None = None
    ) -> LandedQuote:
        """Ask Reap for a quote and record its landed price, the figure the gate decides on.

        Each request is a new attempt at the need, counted before it is sent, under the
        key ``quo:<need>:<variant>:<attempt>``; ``QUOTE_TEMPORARILY_UNAVAILABLE`` is
        retried under the next attempt's key, as the client's retry policy allows.

        Args:
            need_id: The need.
            variant: The resolved variant; None on the checkout-URL route, which quotes
                the configured cart.

        Returns:
            The landed quote, stored and ready to propose.

        Raises:
            ControlPlaneError: If the variant does not fit the need's route, or a variant
                that ships has no address to ship to.
        """
        need = self.need(need_id)
        request = self._quote_request(need, variant)
        item = variant.id if variant is not None else "url"
        attempt = self._next_quote_attempt(need_id)
        keys = {attempt: f"quo:{need_id}:{item}:{attempt}"}

        def retry_key() -> str:
            nonlocal attempt
            attempt = self._next_quote_attempt(need_id)
            keys[attempt] = f"quo:{need_id}:{item}:{attempt}"
            return keys[attempt]

        quote = await self.reap.create_quote(
            request, idempotency_key=keys[attempt], retry_key=retry_key
        )
        if variant is None:
            cart = need.spec.external_checkout
            assert cart is not None
            variant = CatalogueVariant(
                id=cart.checkout_url,
                product_id=cart.merchant_domain,
                merchant=cart.merchant,
                price=Price.from_reap(quote.amount_breakdown.items_subtotal),
                requires_shipping=True,
            )
        landed = LandedQuote.from_reap(
            quote,
            quote_id=new_id("lqt"),
            objective_id=need.objective_id,
            need_id=need_id,
            attempt=attempt,
            variant=variant,
            quantity=1 if need.spec.route == Route.CHECKOUT_URL else need.spec.quantity,
            idempotency_key=keys[attempt],
            created_at=self._clock(),
        )
        with self.repos.db.transaction():
            self.repos.landed_quotes.insert(
                landed, record_id=landed.id, objective_id=need.objective_id
            )
            self.ledger.append(
                EventType.QUOTE_LANDED,
                subject_id=landed.id,
                objective_id=need.objective_id,
                refs={"variant": variant.id, "need": need_id},
                payload={
                    "reap_quote_id": landed.reap_quote_id,
                    "route": need.spec.route.value,
                    "merchant": landed.merchant,
                    "variant": variant.id,
                    "quantity": landed.quantity,
                    "breakdown": landed.breakdown.model_dump(mode="json"),
                    "shipping": landed.shipping.model_dump(mode="json")
                    if landed.shipping
                    else None,
                    "expires_at": quote.expires_at,
                    "idempotency_key": landed.idempotency_key,
                    "attempt": attempt,
                    "raw_sha256": _raw_hash(quote),
                },
            )
        return landed

    def _option_sets(self, product: ProductDetail, need: Need) -> list[list[str]]:
        match = need.spec.match
        groups: list[list[str]] = []
        for option in product.options:
            values = [
                v.option_id
                for v in option.values
                if v.available is not False
                and (not isinstance(match, AttributeMatch) or match.accepts(option.name, v.label))
            ]
            if not values:
                return []
            groups.append(values)
        combinations = itertools.product(*groups)
        return [
            list(c) for c in itertools.islice(combinations, self.settings.max_variants_per_product)
        ]

    async def _candidates(self, need: Need) -> list[CatalogueVariant]:
        grant = self.gate.current_grant(need.objective_id)
        scope = grant.allowed_merchants if grant else []
        items = [i for i in await self.search_products(need.id) if in_scope(i.merchant, scope)]
        if not items:
            return []
        details = await self.product_details(need.id, items[:10])
        found: list[CatalogueVariant] = []
        for product in details.products:
            for option_ids in self._option_sets(product, need):
                if option_ids:
                    try:
                        variant = await self.select_variant(details, product.id, option_ids)
                    except ReapError as error:
                        if error.code != AgenticErrorCode.VARIANT_RESOLUTION_FAILED:
                            raise
                        continue
                else:
                    variant = CatalogueVariant.from_reap(
                        product.default_variant,
                        product_id=product.id,
                        merchant=details.merchants[product.id],
                    )
                fills, _ = judge_variant(need, variant, quantity=need.spec.quantity)
                if variant.available is not False and fills:
                    found.append(variant)
        return found

    async def gather_quotes(self, need_id: str) -> list[LandedQuote]:
        """Run the need's route to landed quotes: every in-scope candidate that fills it.

        On the catalogue route: search, details of the in-scope results, each available
        option combination resolved to a variant, the variants that fill the need quoted.
        On the checkout-URL route: the configured cart quoted. The same calls run for
        either shape, so the ``purchase:`` block alone decides what is bought.

        Args:
            need_id: The need.

        Returns:
            The landed quotes, in the order quoted; ranking is ``procure.selection``'s.
        """
        need = self.need(need_id)
        if need.spec.route == Route.CHECKOUT_URL:
            return [await self.request_catalogue_quote(need_id)]
        quotes: list[LandedQuote] = []
        for variant in await self._candidates(need):
            try:
                quotes.append(await self.request_catalogue_quote(need_id, variant))
            except ReapError as error:
                if not _skips_candidate(error):
                    raise
        return quotes

    # Purchase

    def propose_purchase(
        self,
        *,
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
        """Propose buying a quote, restating the exact action; the gate decides.

        Args:
            quote_id: A landed catalogue quote, or a lease quote on the card path.
            provider: The merchant (or provider), as the agent understands it.
            offer_id: The variant (or offer), as the agent understands it.
            amount_usd: The landed ``finalAmount`` (or lease amount), as the agent
                understands it.
            rationale: Why this option.
            options_considered: Keys of the variants or offers compared.
            quantity: How many, as the agent understands it.
            currency: The amount's currency, as the agent understands it.
            on_behalf_of: Who the purchase is for, named on the ledger; never decided on.

        Returns:
            The intent and the gate's decision.
        """
        landed = self.repos.landed_quotes.get(quote_id)
        lease = None if landed else self.repos.quotes.get(quote_id)
        quote = landed or lease
        objective_id = quote[0].objective_id if quote else ""
        return self.gate.propose(
            objective_id=objective_id,
            quote_id=quote_id,
            provider=provider,
            offer_id=offer_id,
            amount_usd=amount_usd,
            rationale=rationale,
            options_considered=options_considered,
            quantity=quantity,
            currency=currency,
            on_behalf_of=on_behalf_of,
        )

    async def execute_purchase(self, intent_id: str) -> PurchaseIntent:
        """Re-check, claim once, and pay: a Reap checkout, or a card authorisation.

        A catalogue purchase is claimed, checked out under the claim's idempotency key
        and read until it completes, fails, expires, waits on Reap's approval page or
        its outcome is unknown. Calling it again for the same intent returns the
        recorded outcome and never pays twice.

        Args:
            intent_id: An allowed intent.

        Returns:
            The intent after the attempt: completed, awaiting approval, failed, expired,
            outcome unknown or refused at apply time (on the card path: authorised,
            declined, failed, outcome unknown or refused).

        Raises:
            ControlPlaneError: If a lease is executed while the card path is dormant.
        """
        intent, _ = self.repos.intents.require(intent_id)
        if intent.is_agentic:
            return await self._execute_checkout(intent_id)
        if self.purchase_path != PurchasePath.CARD:
            raise ControlPlaneError(
                f"intent {intent_id} leases on a card; the card path is dormant "
                "(REAP_PURCHASE_PATH=agentic)"
            )
        return await self._execute_card(intent_id)

    async def _execute_checkout(self, intent_id: str) -> PurchaseIntent:
        claim = self.gate.claim(intent_id)
        if not claim.claimed:
            return claim.intent
        intent = claim.intent
        quote, _ = self.repos.landed_quotes.require(intent.quote_id)
        enrolment = self.objective(intent.objective_id).enrolment
        if enrolment is None:
            return self.gate.record_failed(
                intent_id, code="NO_ENROLMENT", error="the objective has no enrolment"
            )
        simulate: SimulateCheckout | None = (
            "COMPLETED"
            if self.settings.simulate_completed_when_allowed
            and intent.approved_by is None
            and not self.reap.card_spend
            else None
        )
        try:
            request = CreateCheckoutRequest(
                quote_id=quote.reap_quote_id,
                enrollment_id=enrolment.id,
                presentation=Presentation(return_url=self.settings.return_url),
            )
        except ValidationError as error:
            return self.gate.record_failed(
                intent_id, code=AgenticErrorCode.VALIDATION_FAILED, error=str(error)
            )
        created = await self._create_checkout(intent, request, simulate)
        if isinstance(created, PurchaseIntent):
            return created
        self.gate.record_checkout_created(
            intent_id, created, simulated=simulate is not None, card_spend=self.reap.card_spend
        )
        return await self._follow_checkout(intent_id, created.id, simulated=simulate is not None)

    async def _create_checkout(
        self,
        intent: PurchaseIntent,
        request: CreateCheckoutRequest,
        simulate: SimulateCheckout | None,
    ) -> CheckoutCreated | PurchaseIntent:
        """Send the checkout under the claim's key, retrying and replaying as the module says.

        Args:
            intent: The claimed intent.
            request: The checkout request.
            simulate: The sandbox header's value, if sent.

        Returns:
            The created checkout, or the intent as recorded when none could be.
        """
        key = intent.idempotency_key
        assert key is not None
        retries = replays = 0
        ambiguous = False
        while True:
            try:
                return await self.reap.create_checkout(
                    request, idempotency_key=key, simulate=simulate
                )
            except ReapTransportError as error:
                ambiguous = ambiguous or error.maybe_sent
                failure = str(error)
            except ReapError as error:
                if error.code != AgenticErrorCode.IDEMPOTENCY_REQUEST_IN_PROGRESS:
                    return await self._checkout_refused(intent, error)
                ambiguous = True
                failure = str(error)
            if ambiguous:
                if replays >= self.settings.create_replays:
                    return self.gate.record_outcome_unknown(
                        intent.id, error=f"{failure}; {replays} same-key replays"
                    )
                replays += 1
            else:
                if retries >= self.settings.connect_retries:
                    return self.gate.record_failed(intent.id, code="NOT_SENT", error=failure)
                retries += 1
            await self._sleep(self.settings.backoff_s * 2 ** (retries + replays - 1))

    async def _checkout_refused(self, intent: PurchaseIntent, error: ReapError) -> PurchaseIntent:
        if _maybe_charged(error):
            return self.gate.record_outcome_unknown(intent.id, error=str(error))
        failed = self.gate.record_failed(intent.id, code=error.code, error=error.message)
        if error.code in _ENROLMENT_CODES:
            with contextlib.suppress(ReapError, ReapTransportError):
                await self.refresh_enrolment(intent.objective_id)
        return failed

    async def _follow_checkout(
        self, intent_id: str, checkout_id: str, *, simulated: bool
    ) -> PurchaseIntent:
        settings = self.settings
        try:
            checkout = await self.reap.poll_checkout(
                checkout_id,
                every_s=settings.poll_every_s,
                deadline_s=settings.poll_deadline_s,
                max_every_s=settings.poll_max_every_s,
            )
            waited = 0.0
            while (
                simulated
                and checkout.status == CheckoutStatus.REQUIRES_ACTION
                and waited < settings.simulated_grace_s
            ):
                await self._sleep(settings.poll_every_s)
                waited += settings.poll_every_s
                checkout = await self.reap.get_checkout(checkout_id)
        except CheckoutPollTimeoutError as error:
            status = error.last.status.value if error.last else "unread"
            return self.gate.record_outcome_unknown(
                intent_id,
                error=f"checkout {checkout_id} still {status} after {error.waited_s:.0f} s",
            )
        except (ReapError, ReapTransportError) as error:
            return self.gate.record_outcome_unknown(
                intent_id, error=f"checkout {checkout_id} unreadable: {error}"
            )
        return await self._record_checkout(intent_id, checkout)

    async def _record_checkout(self, intent_id: str, checkout: Checkout) -> PurchaseIntent:
        intent, _ = self.repos.intents.require(intent_id)
        match checkout.status:
            case CheckoutStatus.COMPLETED:
                completed = self.gate.record_completed(intent_id, checkout)
                await self.follow_through(intent_id)
                return completed
            case CheckoutStatus.FAILED | CheckoutStatus.EXPIRED:
                return self.gate.record_checkout_failed(intent_id, checkout)
            case CheckoutStatus.REQUIRES_ACTION:
                if intent.state == IntentState.AWAITING_APPROVAL or checkout.next_action is None:
                    return intent
                return self.gate.record_awaiting_approval(intent_id, checkout)
            case CheckoutStatus.PROCESSING:
                return intent

    async def purchase_status(self, intent_id: str) -> PurchaseIntent:
        """Read a purchase's checkout again and record what changed.

        Used while a checkout awaits approval, to reconcile an unknown outcome whose
        checkout id was seen, and on every tick until the purchase ends. An unknown
        outcome with no checkout id is the operator's to reconcile.

        Args:
            intent_id: The intent.

        Returns:
            The intent as now recorded.
        """
        intent, _ = self.repos.intents.require(intent_id)
        open_states = {
            IntentState.EXECUTING,
            IntentState.AWAITING_APPROVAL,
            IntentState.OUTCOME_UNKNOWN,
        }
        if not intent.is_agentic or intent.checkout_id is None or intent.state not in open_states:
            return intent
        try:
            checkout = await self.reap.get_checkout(intent.checkout_id)
        except (ReapError, ReapTransportError):
            return intent
        return await self._record_checkout(intent_id, checkout)

    async def follow_through(self, intent_id: str) -> None:
        """Hand a completed order to its shape's follow-through, if one is wired.

        Args:
            intent_id: A completed catalogue purchase.

        Raises:
            ControlPlaneError: If the purchase has not completed.
        """
        intent, _ = self.repos.intents.require(intent_id)
        if intent.state != IntentState.COMPLETED or intent.need_id is None:
            raise ControlPlaneError(f"intent {intent_id} is {intent.state}, not completed")
        need = self.need(intent.need_id)
        hook = self._follow_through.get(need.spec.shape)
        if hook is None:
            return
        order, _ = self.repos.orders.require(intent.checkout_id or "")
        await hook(CompletedPurchase(intent=intent, order=order, need=need))

    async def _execute_card(self, intent_id: str) -> PurchaseIntent:
        """Claim once and ask Reap to authorise the charge on the objective's card.

        Args:
            intent_id: An allowed lease intent.

        Returns:
            The intent after the attempt: authorised, declined, failed, outcome
            unknown, or refused at apply time.
        """
        claim = self.gate.claim(intent_id)
        if not claim.claimed:
            return claim.intent
        intent = claim.intent
        card_id = self._card_id(intent.objective_id)
        merchant = merchant_for(intent.provider)
        request = SimulateAuthorizationRequest(
            card_id=card_id,
            amount=intent.amount_usd,
            channel="ECOMMERCE",
            merchant=SimulatedMerchant(
                name=merchant.name,
                mcc_code=merchant.mcc_code,
                mcc_category=merchant.mcc_category,
                country=merchant.country,
            ),
        )
        try:
            txn = await self.reap.simulate_authorization(
                request, idempotency_key=intent.idempotency_key
            )
        except ReapTransportError as error:
            if error.maybe_sent:
                return self.gate.record_outcome_unknown(intent_id, error=str(error))
            return self.gate.record_failed(intent_id, code="NOT_SENT", error=str(error))
        except ReapError as error:
            return self.gate.record_failed(intent_id, code=error.code, error=error.message)
        if isinstance(txn, DeclinedCardTransaction):
            return self.gate.record_declined(
                intent_id,
                reap_transaction_id=txn.id,
                code=txn.decline_reason.code,
                policy_name=txn.policy.name if txn.policy else None,
            )
        return self.gate.record_authorised(intent_id, reap_transaction_id=txn.id)

    def _card_id(self, objective_id: str) -> str:
        objective = self.objective(objective_id)
        if objective.reap is None:
            raise ControlPlaneError(f"objective {objective_id} has no Reap card")
        return objective.reap.card_id

    # Provision, evaluate, release

    async def provision(self, intent_id: str) -> Deployment:
        """Start the capacity an authorised purchase paid for; idempotent per intent.

        Args:
            intent_id: An authorised intent.

        Returns:
            The deployment.

        Raises:
            ControlPlaneError: If the purchase was not authorised.
        """
        intent, _ = self.repos.intents.require(intent_id)
        existing = [
            d
            for d in self.repos.deployments.list(objective_id=intent.objective_id)
            if d.intent_id == intent_id
        ]
        if existing:
            return existing[0]
        if intent.state != IntentState.AUTHORISED:
            raise ControlPlaneError(f"intent {intent_id} is {intent.state}, not authorised")
        quote, _ = self.repos.quotes.require(intent.quote_id)
        deployment = await self.provisioner.provision(
            quote.offer, objective_id=intent.objective_id, intent_id=intent_id, now=self._clock()
        )
        refs = {"intent": intent_id, "reap_transaction": intent.reap_transaction_id or ""}
        if intent.decision_id:
            refs["decision"] = intent.decision_id
        with self.repos.db.transaction():
            self.repos.deployments.insert(
                deployment, record_id=deployment.id, objective_id=intent.objective_id
            )
            self.ledger.append(
                EventType.DEPLOYMENT_PROVISIONED,
                subject_id=deployment.id,
                objective_id=intent.objective_id,
                refs=refs,
                payload={
                    "provider": deployment.provider,
                    "offer": quote.offer.key,
                    "mode": deployment.mode,
                    "simulation_notice": deployment.simulation_notice,
                },
            )
        return deployment

    def evaluate_deployment(self, deployment_id: str) -> LifecycleDecision:
        """Apply the rollback and termination rules to a deployment.

        Args:
            deployment_id: The deployment.

        Returns:
            Keep, release or roll back, naming the rule.
        """
        deployment, _ = self.repos.deployments.require(deployment_id)
        objective = self.objective(deployment.objective_id)
        rules, _ = self.repos.lifecycles.require(deployment.objective_id)
        decision = evaluate_deployment(
            deployment,
            objective=objective,
            grant=self.gate.current_grant(deployment.objective_id),
            observations=self.observations(deployment.objective_id),
            rules=rules.rules,
            now=self._clock(),
        )
        self.ledger.append(
            EventType.LIFECYCLE_DECIDED,
            subject_id=new_id("lcd"),
            objective_id=deployment.objective_id,
            refs={"deployment": deployment_id},
            payload={
                "action": decision.action.value,
                "rule": decision.rule,
                "reason": decision.reason,
            },
        )
        return decision

    async def release(self, deployment_id: str, reason: str) -> Deployment:
        """Stop a deployment and settle what it actually cost.

        The metered amount (price times time used, at the provider's granularity,
        capped at the authorisation) is cleared through Reap and becomes the spend the
        runway counts.

        Args:
            deployment_id: A running, non-protected deployment.
            reason: Why it is released; usually the lifecycle rule.

        Returns:
            The released deployment.
        """
        deployment, version = self.repos.deployments.require(deployment_id)
        now = self._clock()
        released = await self.provisioner.release(deployment, reason=reason, now=now)
        verdicts = [
            e
            for e in self.ledger.events(
                objective_id=deployment.objective_id, types=[EventType.LIFECYCLE_DECIDED]
            )
            if e.refs.get("deployment") == deployment_id and e.payload.get("action") != "keep"
        ]
        refs = {"lifecycle_decision": verdicts[-1].subject_id} if verdicts else {}
        with self.repos.db.transaction():
            self.repos.deployments.update(
                released, record_id=deployment_id, expected_version=version
            )
            self.ledger.append(
                EventType.DEPLOYMENT_RELEASED,
                subject_id=deployment_id,
                objective_id=deployment.objective_id,
                refs=refs,
                payload={"reason": reason},
            )
        if deployment.intent_id is not None and deployment.offer is not None:
            await self._settle(
                deployment.intent_id, metered_usd(deployment.offer, deployment.started_at, now)
            )
        return released

    async def _settle(self, intent_id: str, metered: Decimal) -> None:
        intent, _ = self.repos.intents.require(intent_id)
        if intent.state != IntentState.AUTHORISED or intent.reap_transaction_id is None:
            return
        amount = min(metered, intent.amount_usd)
        await self.reap.simulate_clearing(
            SimulateClearingRequest(
                card_id=self._card_id(intent.objective_id),
                transaction_id=intent.reap_transaction_id,
                amount=amount,
            )
        )
        self.gate.record_settled(intent_id, amount_usd=amount)

    async def complete_objective(self, objective_id: str) -> Objective:
        """Close an objective and freeze its card, so no further charge can land.

        Args:
            objective_id: The objective.

        Returns:
            The completed objective.

        Raises:
            ControlPlaneError: If any of its deployments is still running.
        """
        running = [
            d
            for d in self.repos.deployments.list(objective_id=objective_id)
            if d.status == DeploymentStatus.RUNNING and not d.protected
        ]
        if running:
            raise ControlPlaneError(f"{len(running)} deployment(s) still running")
        objective, version = self.repos.objectives.require(objective_id)
        if objective.reap is not None:
            await self.reap.freeze_card(objective.reap.card_id)
            self.ledger.append(
                EventType.REAP_CARD_FROZEN,
                subject_id=objective.reap.card_id,
                objective_id=objective_id,
                payload={"reason": "objective completed"},
            )
        completed = objective.model_copy(update={"status": ObjectiveStatus.COMPLETED})
        with self.repos.db.transaction():
            self.repos.objectives.update(
                completed, record_id=objective_id, expected_version=version
            )
            self.ledger.append(
                EventType.OBJECTIVE_COMPLETED,
                subject_id=objective_id,
                objective_id=objective_id,
                payload={"runway_usd": str(self.gate.runway_usd(objective_id))},
            )
        return completed

    # Audit

    def provenance(self, objective_id: str) -> ProvenanceGraph:
        """Draw the objective's provenance graph from the ledger.

        Args:
            objective_id: The objective.

        Returns:
            The graph.
        """
        return build_graph(self.ledger.events(objective_id=objective_id))

    # Reap callbacks

    def receive_webhook(self, event: WebhookEvent) -> bool:
        """Record a verified Reap notification once; deliveries are at least once.

        Args:
            event: The verified envelope.

        Returns:
            True if new, False if it was a duplicate.
        """
        seen = self.ledger.events(types=[EventType.REAP_WEBHOOK_RECEIVED])
        if any(e.subject_id == event.id for e in seen):
            return False
        card_id = event.data.get("cardId") or event.data.get("id")
        objective_id = next(
            (
                o.id
                for o in self.repos.objectives.list()
                if o.reap is not None and o.reap.card_id == card_id
            ),
            None,
        )
        refs = {"reap_transaction": str(event.data["id"])} if "cardId" in event.data else {}
        self.ledger.append(
            EventType.REAP_WEBHOOK_RECEIVED,
            subject_id=event.id,
            objective_id=objective_id,
            refs=refs,
            payload={"type": event.type, "status": event.data.get("status")},
        )
        return True

    def authorise(self, request: CardAuthorizationRequest) -> ExternalAuthDecision:
        """Answer Reap's real-time authorisation request from the gate's claimed intents.

        Args:
            request: The verified request data.

        Returns:
            APPROVE only for an exact match with a claimed purchase.
        """
        answer: ExternalAuthorisation = self.gate.authorise_external(
            card_id=request.card_id,
            amount_usd=request.amount,
            merchant_name=request.merchant.name,
        )
        if answer.approve:
            return ExternalAuthDecision(decision="APPROVE")
        return ExternalAuthDecision(decision="DECLINE", reason="TRANSACTION_NOT_ALLOWED")

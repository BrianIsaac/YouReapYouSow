"""The agent buys the prize for the winner through Reap's Agentic module, behind the gate.

The purchase is the engine's agentic path, unchanged: an objective whose budget and caps
are the pool's ceiling (gross less the disclosed buffer), a need from the prize's purchase
file, search, details, variant, a landed quote, the authority gate's decision on that
quote's ``finalAmount``, the claim, the checkout under the claim's idempotency key, and
reads until the order id. Every step lands on the ledger.

When Reap answers the checkout with its hosted approval page, the purchase waits on the
card holder: it carries the page and its expiry, and ``follow`` re-reads the checkout
until it completes, fails or the page expires unused.

The preview quote at start-up is a plain read of the catalogue and a quote, with no
objective and no checkout, so the pool's disclosure can show the landed price.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import JsonValue

from youreapyousow.authority.lifecycle import LifecycleRules
from youreapyousow.control import ControlPlane, ControlPlaneError, GrantTerms
from youreapyousow.domain import Disposition, IntentState, ObjectiveKind, PurchaseIntent
from youreapyousow.game.models import CENT, PrizePurchase
from youreapyousow.purchase import PurchaseConfig
from youreapyousow.reap.client import ReapClient, ReapError, ReapTransportError
from youreapyousow.reap.models import (
    CreateItemsQuoteRequest,
    EnrollmentStatus,
    ProductDetailsRequest,
    QuoteItem,
)

QUOTE_TRIES = 3
READ_TIMEOUT_S = 20.0
_OPEN_STATES = frozenset(
    {IntentState.EXECUTING, IntentState.AWAITING_APPROVAL, IntentState.OUTCOME_UNKNOWN}
)


@dataclass(frozen=True)
class PreviewQuote:
    """The prize's landed quote, read at start-up for the disclosure.

    Attributes:
        name: The product's name.
        merchant: The merchant.
        list_price: The variant's price.
        image_url: The product's image, if the catalogue gives one.
        items: The items subtotal.
        shipping: The shipping.
        tax: The tax.
        final_amount: The landed total.
        expires_at: When Reap's quote expires.
    """

    name: str
    merchant: str
    list_price: Decimal
    image_url: str | None
    items: Decimal
    shipping: Decimal
    tax: Decimal
    final_amount: Decimal
    expires_at: str


async def preview_quote(reap: ReapClient, purchase: PurchaseConfig) -> PreviewQuote:
    """Search for the prize, resolve its default variant and land a quote; buy nothing.

    Args:
        reap: The Reap backend.
        purchase: The prize's purchase file.

    Returns:
        The preview.

    Raises:
        ControlPlaneError: If the file has no search or nothing matches it.
    """
    if purchase.search is None:
        raise ControlPlaneError("the prize file has no catalogue search")
    found = await reap.search_products(purchase.search.request())
    wanted = purchase.search.query.lower()
    products = sorted(found.products, key=lambda p: wanted not in p.name.lower())
    if not products:
        raise ControlPlaneError(f"no product found for {purchase.search.query!r}")
    summary = products[0]
    details = await reap.product_details(ProductDetailsRequest(product_ids=[summary.id]))
    if not details.products:
        raise ControlPlaneError(f"no details for {summary.name}")
    variant = details.products[0].default_variant
    quote = await reap.create_quote(
        CreateItemsQuoteRequest(
            email=purchase.email,
            items=[QuoteItem(variant_id=variant.id, quantity=purchase.quantity)],
            shipping_address=purchase.shipping_address,
        ),
        idempotency_key=f"preview:{uuid.uuid4().hex}",
    )
    b = quote.amount_breakdown
    return PreviewQuote(
        name=summary.name,
        merchant=summary.merchant.name,
        list_price=Decimal(str(variant.price.amount)),
        image_url=summary.image_url,
        items=Decimal(str(b.items_subtotal.amount)),
        shipping=Decimal(str(b.shipping.amount)) if b.shipping else Decimal(0),
        tax=Decimal(str(b.tax.amount.amount)) if b.tax else Decimal(0),
        final_amount=Decimal(str(b.final_amount.amount)),
        expires_at=quote.expires_at,
    )


def prize_block(preview: PreviewQuote, source: str) -> dict[str, JsonValue]:
    """Render the preview as the polled state's prize block.

    Args:
        preview: The preview.
        source: ``sandbox``, ``kwal`` or ``mock``.

    Returns:
        The block.
    """

    def m(value: Decimal) -> str:
        return str(value.quantize(CENT))

    return {
        "name": preview.name,
        "merchant": preview.merchant,
        "list_price": m(preview.list_price),
        "image_url": preview.image_url,
        "quote": {
            "final_amount": m(preview.final_amount),
            "items": m(preview.items),
            "shipping": m(preview.shipping),
            "tax": m(preview.tax),
            "expires_at": preview.expires_at,
            "source": source,
        },
    }


class PrizeBuyer:
    """Runs the engine's agentic purchase for the prize under the pool's ceiling."""

    def __init__(
        self,
        control: ControlPlane,
        purchase: PurchaseConfig,
        *,
        backend: str,
        enrollment_id: str | None,
        fallback: "PrizeBuyer | None" = None,
    ) -> None:
        """Wire the buyer.

        Args:
            control: The engine's control plane.
            purchase: The prize's purchase file.
            backend: ``sandbox``, ``kwal`` or ``mock``, for the screen.
            enrollment_id: The operator's ACTIVE enrolment on the sandbox, if set.
            fallback: Buys on the mock when the sandbox enrolment is not ACTIVE.
        """
        self.control = control
        self.purchase = purchase
        self.backend = backend
        self.enrollment_id = enrollment_id
        self.fallback = fallback

    async def enrolment_active(self) -> bool:
        """Read the operator's enrolment at Reap now.

        Returns:
            True only when it reads ``ACTIVE``.
        """
        if self.enrollment_id is None:
            return False
        try:
            enrolment = await asyncio.wait_for(
                self.control.reap.get_enrollment(self.enrollment_id), timeout=20
            )
        except (ReapError, ReapTransportError, TimeoutError):
            return False
        return enrolment.status == EnrollmentStatus.ACTIVE

    async def buy(
        self, *, ceiling: Decimal, winner: str, on_step: Callable[[PrizePurchase], None]
    ) -> PrizePurchase:
        """Buy the prize: quote, gate, claim, checkout, read until the order id.

        Args:
            ceiling: The pool less its buffer: the budget, per-purchase and daily cap.
            winner: The winner's name, for the objective's statement.
            on_step: Called with the purchase as each step begins.

        Returns:
            The purchase: ``PURCHASED`` with the order id, or ``FAILED`` with why.
        """
        state = PrizePurchase(status="BUYING", step="search", backend=self.backend, ceiling=ceiling)
        if (
            self.backend == "sandbox"
            and self.fallback is not None
            and not await self.enrolment_active()
        ):
            note = (
                "Bought on the local mock of Reap: the operator's sandbox enrolment is not "
                "ACTIVE yet, and the sandbox refuses a checkout without one."
            )

            def noted(purchase: PrizePurchase) -> None:
                on_step(purchase.model_copy(update={"note": note}))

            bought = await self.fallback.buy(ceiling=ceiling, winner=winner, on_step=noted)
            return bought.model_copy(update={"note": note})
        if self.backend == "sandbox" and self.enrollment_id is None:
            failed = state.model_copy(
                update={
                    "status": "FAILED",
                    "error": "No ACTIVE Reap enrolment: the operator completes Reap's hosted "
                    "card page and sets REAP_ENROLLMENT_ID.",
                }
            )
            on_step(failed)
            return failed
        on_step(state)
        try:
            return await self._buy(state, ceiling, winner, on_step)
        except (ReapError, ReapTransportError, ControlPlaneError, ValueError) as error:
            failed = state.model_copy(
                update={"status": "FAILED", "error": f"{type(error).__name__}: {error}"[:300]}
            )
            on_step(failed)
            return failed

    async def _buy(
        self,
        state: PrizePurchase,
        ceiling: Decimal,
        winner: str,
        on_step: Callable[[PrizePurchase], None],
    ) -> PrizePurchase:
        control = self.control
        objective = await control.create_objective(
            kind=ObjectiveKind.SERVICE_FLEET,
            statement=f"Buy the prize for {winner}",
            constraints=[],
            budget_usd=ceiling,
            grant=GrantTerms(
                allowed_providers=(),
                per_transaction_cap_usd=ceiling,
                daily_cap_usd=ceiling,
                ttl=timedelta(hours=1),
                allowed_merchants=self.purchase.merchants,
                attempts_per_need=self.purchase.grant.attempts_per_need,
                quote_margin_s=self.purchase.grant.quote_margin_s,
            ),
            lifecycle=LifecycleRules(),
            enrollment_id=self.enrollment_id,
        )
        need = control.raise_need(
            objective.id,
            self.purchase.need_spec(),
            reason=f"The group's prize for its winner, {winner}",
            refs={},
        )
        state = state.model_copy(update={"step": "quote"})
        on_step(state)
        quotes = []
        for attempt in range(QUOTE_TRIES):
            try:
                quotes = await control.gather_quotes(need.id)
                break
            except ReapTransportError:
                if attempt == QUOTE_TRIES - 1:
                    raise
        if not quotes:
            raise ControlPlaneError("no candidate matched the prize and landed a quote")
        cheapest = min(quotes, key=lambda q: q.final_amount.amount)
        final = Decimal(str(cheapest.final_amount.amount))
        state = state.model_copy(update={"step": "gate", "quote_final_amount": final})
        on_step(state)
        intent, decision = control.propose_purchase(
            quote_id=cheapest.id,
            provider=cheapest.merchant,
            offer_id=cheapest.variant.id,
            amount_usd=final,
            rationale=f"The prize promised to the group, landed at {final}, within the pool's "
            f"ceiling of {ceiling}.",
            options_considered=[q.id for q in quotes],
        )
        gate = {
            "disposition": decision.disposition.value,
            "rule": decision.rule,
            "reason": decision.reason,
        }
        state = state.model_copy(update={"gate": gate, "intent_id": intent.id})
        if decision.disposition != Disposition.ALLOW:
            refused = state.model_copy(
                update={
                    "status": "FAILED",
                    "error": f"The gate did not allow it: {decision.reason}",
                }
            )
            on_step(refused)
            return refused
        state = state.model_copy(update={"step": "checkout"})
        on_step(state)
        done = await control.execute_purchase(intent.id)
        if done.state == IntentState.AWAITING_APPROVAL and done.checkout_id is not None:
            waiting = await self._awaiting(state, done.checkout_id)
            on_step(waiting)
            return waiting
        if done.state != IntentState.COMPLETED or done.order_id is None:
            failed = state.model_copy(
                update={
                    "status": "FAILED",
                    "step": "reading",
                    "checkout_id": done.checkout_id,
                    "error": f"The checkout ended {done.state.value}.",
                }
            )
            on_step(failed)
            return failed
        final_amount = done.final_amount_usd if done.final_amount_usd is not None else final
        bought = state.model_copy(
            update={
                "status": "PURCHASED",
                "step": "done",
                "order_id": done.order_id,
                "checkout_id": done.checkout_id,
                "final_amount": final_amount,
            }
        )
        on_step(bought)
        return bought

    async def _awaiting(self, state: PrizePurchase, checkout_id: str) -> PrizePurchase:
        """Read the checkout's approval page: the card holder's one tap.

        Args:
            state: The purchase at the checkout step.
            checkout_id: The checkout awaiting approval.

        Returns:
            The purchase awaiting approval, with the page and its expiry when readable.
        """
        url: str | None = None
        expires_at: datetime | None = None
        try:
            checkout = await asyncio.wait_for(
                self.control.reap.get_checkout(checkout_id), timeout=READ_TIMEOUT_S
            )
        except (ReapError, ReapTransportError, TimeoutError):
            checkout = None
        action = checkout.next_action if checkout is not None else None
        if action is not None:
            url = action.url
            expires_at = datetime.fromisoformat(action.expires_at) if action.expires_at else None
        return state.model_copy(
            update={
                "status": "AWAITING_APPROVAL",
                "step": "approval",
                "checkout_id": checkout_id,
                "approval_url": url,
                "approval_expires_at": expires_at,
            }
        )

    async def follow(self, purchase: PrizePurchase, *, now: datetime) -> PrizePurchase:
        """Re-read a purchase awaiting approval through the control plane, once.

        Args:
            purchase: The purchase as last recorded.
            now: The time to judge the approval page's expiry by.

        Returns:
            ``PURCHASED`` once the checkout completed, ``FAILED`` with why once it failed,
            expired or its page expired unused, else the purchase unchanged.
        """
        if purchase.status != "AWAITING_APPROVAL" or purchase.intent_id is None:
            return purchase
        try:
            intent = await asyncio.wait_for(
                self.control.purchase_status(purchase.intent_id), timeout=READ_TIMEOUT_S
            )
        except TimeoutError:
            return purchase
        settled = self._settled(purchase, intent, now)
        if settled is not None:
            return settled
        expires_at = purchase.approval_expires_at
        if expires_at is not None and now >= expires_at:
            return purchase.model_copy(
                update={
                    "status": "FAILED",
                    "error": "The approval page expired unused at "
                    f"{expires_at:%H:%M} UTC; the agent can open a fresh checkout.",
                }
            )
        return purchase

    def _settled(
        self, purchase: PrizePurchase, intent: PurchaseIntent, now: datetime
    ) -> PrizePurchase | None:
        if intent.state in _OPEN_STATES:
            return None
        if intent.state == IntentState.COMPLETED and intent.order_id is not None:
            final = intent.final_amount_usd
            return purchase.model_copy(
                update={
                    "status": "PURCHASED",
                    "step": "done",
                    "order_id": intent.order_id,
                    "final_amount": final if final is not None else purchase.quote_final_amount,
                    "approved_at": now,
                    "approval_url": None,
                }
            )
        return purchase.model_copy(
            update={
                "status": "FAILED",
                "step": "reading",
                "error": f"The checkout ended {intent.state.value} before the order was placed.",
            }
        )

    async def retry(
        self,
        previous: PrizePurchase,
        *,
        ceiling: Decimal,
        winner: str,
        now: datetime,
        on_step: Callable[[PrizePurchase], None],
    ) -> PrizePurchase:
        """Open a fresh checkout after the last one's approval page expired unused.

        The last checkout is read once more first, so a late approval is recorded rather
        than paid for twice. A fresh checkout needs a fresh quote: Reap refuses a second
        checkout on a quote that already has one. The purchase runs the whole path again,
        through the same gate, under a new intent and so a new idempotency key.

        Args:
            previous: The purchase as last recorded, ``FAILED``.
            ceiling: The pool less its buffer.
            winner: The winner's name.
            now: The time to judge the last approval page by.
            on_step: Called with the purchase as each step begins.

        Returns:
            The purchase: awaiting approval again, purchased, or failed with why.
        """
        if previous.intent_id is not None:
            try:
                intent = await asyncio.wait_for(
                    self.control.purchase_status(previous.intent_id), timeout=READ_TIMEOUT_S
                )
            except TimeoutError:
                intent = None
            if intent is not None and intent.state == IntentState.COMPLETED:
                late = self._settled(previous, intent, now)
                if late is not None:
                    on_step(late)
                    return late
        return await self.buy(ceiling=ceiling, winner=winner, on_step=on_step)

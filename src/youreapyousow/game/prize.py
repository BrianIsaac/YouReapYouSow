"""The agent buys the prize for the winner through Reap's Agentic module, behind the gate.

The purchase is the engine's agentic path, unchanged: an objective whose budget and caps
are the pool's ceiling (gross less the disclosed buffer), a need from the prize's purchase
file, search, details, variant, a landed quote, the authority gate's decision on that
quote's ``finalAmount``, the claim, the checkout under the claim's idempotency key, and
reads until the order id. Every step lands on the ledger.

The preview quote at start-up is a plain read of the catalogue and a quote, with no
objective and no checkout, so the pool's disclosure can show the landed price.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from pydantic import JsonValue

from youreapyousow.authority.lifecycle import LifecycleRules
from youreapyousow.control import ControlPlane, ControlPlaneError, GrantTerms
from youreapyousow.domain import Disposition, IntentState, ObjectiveKind
from youreapyousow.game.models import CENT, PrizePurchase
from youreapyousow.purchase import PurchaseConfig
from youreapyousow.reap.client import ReapClient, ReapError, ReapTransportError
from youreapyousow.reap.models import (
    CreateItemsQuoteRequest,
    ProductDetailsRequest,
    QuoteItem,
)

QUOTE_TRIES = 3


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
    ) -> None:
        """Wire the buyer.

        Args:
            control: The engine's control plane.
            purchase: The prize's purchase file.
            backend: ``sandbox``, ``kwal`` or ``mock``, for the screen.
            enrollment_id: The operator's ACTIVE enrolment on the sandbox, if set.
        """
        self.control = control
        self.purchase = purchase
        self.backend = backend
        self.enrollment_id = enrollment_id

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

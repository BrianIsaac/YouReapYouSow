"""The purchase that waits on the card holder's approval, followed until it ends."""

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.game.test_finish import play, runtime
from youreapyousow.api.app import Runtime
from youreapyousow.clock import ManualClock
from youreapyousow.game.models import GroupStatus, PrizePurchase
from youreapyousow.game.prize import PrizeBuyer
from youreapyousow.game.view import ledger_view
from youreapyousow.ledger.events import EventType
from youreapyousow.reap.client import ReapMock

pytestmark = pytest.mark.anyio

__all__ = ["runtime"]


def _clock(runtime: Runtime) -> ManualClock:
    clock = runtime.clock
    assert isinstance(clock, ManualClock)
    return clock


def _reap(runtime: Runtime) -> ReapMock:
    reap = runtime.control.reap
    assert isinstance(reap, ReapMock)
    return reap


def _buyer(runtime: Runtime) -> PrizeBuyer:
    """A buyer whose checkout waits on the hosted approval page, as the sandbox's does."""
    assert runtime.purchase is not None
    runtime.control.settings = dataclasses.replace(
        runtime.control.settings, simulate_completed_when_allowed=False
    )
    return PrizeBuyer(runtime.control, runtime.purchase, backend="mock", enrollment_id=None)


async def _awaiting(runtime: Runtime) -> tuple[PrizeBuyer, PrizePurchase]:
    play(runtime)
    _clock(runtime).advance(seconds=21)
    runtime.game.finalize()
    buyer = _buyer(runtime)
    waiting = await buyer.buy(
        ceiling=runtime.game.pool().ceiling,
        winner="Alice",
        on_step=runtime.game.purchase_progress,
    )
    runtime.game.record_purchase(waiting)
    return buyer, waiting


def _summaries(runtime: Runtime, kind: EventType) -> list[str]:
    events = runtime.control.ledger.events(types=[kind])
    return [str(ledger_view(e, {})["summary"]) for e in events]


async def test_a_checkout_needing_approval_waits_with_the_page(runtime: Runtime) -> None:
    """REQUIRES_ACTION: awaiting approval with the page and its expiry, not a failure."""
    _, waiting = await _awaiting(runtime)
    assert waiting.status == "AWAITING_APPROVAL", waiting.error
    assert waiting.error is None
    assert waiting.approval_url is not None
    assert waiting.checkout_id is not None
    assert waiting.approval_url.endswith(waiting.checkout_id)
    assert waiting.approval_expires_at is not None
    assert waiting.approval_expires_at > _clock(runtime)()
    # The mock's quote lives 2 minutes and its page 15: the quote's expiry is the deadline.
    assert waiting.approval_expires_at <= _clock(runtime)() + timedelta(minutes=2)
    assert waiting.intent_id is not None
    assert waiting.quote_final_amount == Decimal("57.49")
    assert waiting.gate is not None
    assert waiting.gate["disposition"] == "allow"
    group = runtime.game.group()
    assert group.status == GroupStatus.FINALIZED
    assert runtime.game.awaiting_purchase() == waiting
    [line] = _summaries(runtime, EventType.CHECKOUT_AWAITING_APPROVAL)
    assert line == (
        "Checkout opened on the local mock of Reap; waiting for the card holder's approval. "
        f"Checkout {waiting.checkout_id}."
    )
    assert not runtime.control.ledger.events(types=[EventType.PRIZE_PURCHASE_FAILED])


async def test_following_an_unapproved_checkout_keeps_it_waiting(runtime: Runtime) -> None:
    """Re-read before the card holder taps: unchanged, nothing recorded."""
    buyer, waiting = await _awaiting(runtime)
    again = await buyer.follow(waiting, now=_clock(runtime)())
    assert again == waiting


async def test_an_approved_checkout_becomes_the_order(runtime: Runtime) -> None:
    """REQUIRES_ACTION then COMPLETED: purchased with the order id, the group fulfilled."""
    buyer, waiting = await _awaiting(runtime)
    assert waiting.checkout_id is not None
    _reap(runtime).agentic.approve_checkout(waiting.checkout_id)
    _clock(runtime).advance(seconds=1)
    now = _clock(runtime)()
    bought = await buyer.follow(waiting, now=now)
    assert bought.status == "PURCHASED"
    assert bought.order_id is not None
    assert bought.final_amount == Decimal("57.49")
    assert bought.approved_at == now
    assert bought.approval_url is None
    group = runtime.game.record_purchase(bought)
    assert group.status == GroupStatus.FULFILLED
    assert runtime.game.awaiting_purchase() is None
    [line] = _summaries(runtime, EventType.PRIZE_PURCHASED)
    assert line == (
        f"Bought for Alice: order {bought.order_id}, 57.49 USD, approved by the card holder "
        f"at {now:%H:%M} UTC. A test charge on the local mock of Reap; the pool is test USDC."
    )
    assert runtime.control.ledger.verify_chain().ok


async def test_an_expired_checkout_fails_and_the_group_stays_final(runtime: Runtime) -> None:
    """Reap expired the checkout unapproved: failed with why, the pool still reserved."""
    buyer, waiting = await _awaiting(runtime)
    _clock(runtime).advance(seconds=15 * 60 + 1)
    failed = await buyer.follow(waiting, now=_clock(runtime)())
    assert failed.status == "FAILED"
    assert failed.step == "approval"
    assert failed.error is not None
    assert failed.error.startswith("No approval came before ")
    group = runtime.game.record_purchase(failed)
    assert group.status == GroupStatus.FINALIZED
    assert runtime.game.pool().entries == 3
    [line] = _summaries(runtime, EventType.CHECKOUT_EXPIRED)
    assert line == f"Checkout {waiting.checkout_id} expired before the card holder approved it."


async def test_a_page_past_its_expiry_fails_even_while_reap_still_waits(runtime: Runtime) -> None:
    """Reap may still read REQUIRES_ACTION; past the page's expiry the wait ends anyway."""
    buyer, waiting = await _awaiting(runtime)
    lapsed = waiting.model_copy(
        update={"approval_expires_at": _clock(runtime)() - timedelta(seconds=1)}
    )
    failed = await buyer.follow(lapsed, now=_clock(runtime)())
    assert failed.status == "FAILED"
    assert failed.error is not None
    assert "the quote and the checkout expired" in failed.error


async def test_retry_opens_a_fresh_checkout_on_a_fresh_quote(runtime: Runtime) -> None:
    """After the page expired: a new intent, quote and checkout, the same gate."""
    buyer, waiting = await _awaiting(runtime)
    assert waiting.approval_expires_at is not None
    _clock(runtime).advance(seconds=(waiting.approval_expires_at - _clock(runtime)()).seconds + 1)
    failed = await buyer.follow(waiting, now=_clock(runtime)())
    runtime.game.record_purchase(failed)
    again = await buyer.retry(
        failed,
        ceiling=runtime.game.pool().ceiling,
        winner="Alice",
        now=_clock(runtime)(),
        on_step=runtime.game.purchase_progress,
    )
    assert again.status == "AWAITING_APPROVAL"
    assert again.checkout_id not in (None, waiting.checkout_id)
    assert again.intent_id not in (None, waiting.intent_id)
    assert again.gate is not None
    assert again.gate["disposition"] == "allow"
    assert len(runtime.control.ledger.events(types=[EventType.CHECKOUT_AWAITING_APPROVAL])) == 2


async def test_retry_records_a_late_approval_instead_of_paying_twice(runtime: Runtime) -> None:
    """The last checkout completed after all: its order is recorded, no new checkout."""
    buyer, waiting = await _awaiting(runtime)
    assert waiting.checkout_id is not None
    _reap(runtime).agentic.approve_checkout(waiting.checkout_id)
    _clock(runtime).advance(seconds=1)
    failed = waiting.model_copy(update={"status": "FAILED", "error": "The page expired."})
    bought = await buyer.retry(
        failed,
        ceiling=runtime.game.pool().ceiling,
        winner="Alice",
        now=_clock(runtime)(),
        on_step=lambda p: None,
    )
    assert bought.status == "PURCHASED"
    assert bought.order_id is not None
    assert len(runtime.control.ledger.events(types=[EventType.CHECKOUT_AWAITING_APPROVAL])) == 1

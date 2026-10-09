"""The finish: freeze, disputes, the winner by the tie-break, the prize bought behind the gate."""

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import START
from tests.game.helpers import DRAFTS
from youreapyousow.api.app import Runtime, build_runtime
from youreapyousow.clock import ManualClock
from youreapyousow.config import Settings
from youreapyousow.game.coach import CoachTurn
from youreapyousow.game.models import GroupStatus, PrizePurchase, ScoreState
from youreapyousow.game.prize import PrizeBuyer, preview_quote
from youreapyousow.game.rubric import standings
from youreapyousow.game.service import EvidenceIn, GameError
from youreapyousow.ledger.events import EventType
from youreapyousow.market.service import MarketMode

pytestmark = pytest.mark.anyio

WEEK = 60.0


@pytest.fixture
async def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    """The whole runtime on the mock, on a manual clock, the agentic path.

    Yields:
        The runtime, its group open.
    """
    monkeypatch.setenv("REAP_PURCHASE_PATH", "agentic")
    settings = Settings.model_validate(
        {
            "database_path": tmp_path / "db.sqlite",
            "snapshot_path": tmp_path / "snap.json",
            "market_mode": MarketMode.MOCK,
            "evidence_dir": tmp_path / "evidence",
        }
    )
    clock = ManualClock(START)
    value = build_runtime(settings, clock=clock)
    try:
        yield value
    finally:
        await value.aclose()


def _clock(runtime: Runtime) -> ManualClock:
    clock = runtime.clock
    assert isinstance(clock, ManualClock)
    return clock


def _to_start(runtime: Runtime) -> None:
    starts_at = runtime.game.group().starts_at
    assert starts_at is not None
    clock = _clock(runtime)
    clock.advance(seconds=(starts_at - clock()).total_seconds())
    runtime.game.tick()


def play(runtime: Runtime) -> list[str]:
    """Run a challenge to its dispute window: Alice 35, Ben 15, Chloe 0.

    Args:
        runtime: The runtime.

    Returns:
        The player ids.
    """
    game = runtime.game
    ids = [game.join(n).id for n in ("Alice", "Ben", "Chloe")]
    for pid, draft in zip(ids, DRAFTS, strict=True):
        game.record_intake(pid, "goal", CoachTurn("ok", draft, "fake"))
        game.lock_contract(pid)
    for pid in ids:
        game.accept(pid)
    _to_start(runtime)
    game.checkin(ids[0], milestone=0, value=Decimal(8), evidence=EvidenceIn(b"a", "image/jpeg"))
    game.checkin(ids[1], milestone=0, value=Decimal(1), evidence=None)
    _clock(runtime).advance(seconds=WEEK + 1)
    game.checkin(ids[0], milestone=1, value=Decimal(11), evidence=EvidenceIn(b"b", "image/jpeg"))
    _clock(runtime).advance(seconds=3 * WEEK)
    game.tick()
    return ids


async def test_the_end_freezes_standings_into_the_dispute_window(runtime: Runtime) -> None:
    """At the deadline: standings frozen on the ledger, then the window opens."""
    play(runtime)
    group = runtime.game.group()
    assert group.status == GroupStatus.DISPUTE_WINDOW
    assert group.result is not None
    assert [s.score for s in group.result.standings] == [35, 15, 0]
    frozen = runtime.control.ledger.events(types=[EventType.STANDINGS_FROZEN])
    assert len(frozen) == 1


async def test_a_dispute_withholds_points_and_a_review_reinstates_them(runtime: Runtime) -> None:
    """Ben disputes Alice's milestone 2: 20 points withheld until reinstated."""
    alice, ben, _ = play(runtime)
    game = runtime.game
    target = next(e for e in game.score_events() if e.participant_id == alice and e.milestone == 1)
    disputed = game.dispute(ben, target.event_id, "the photo is blurry")
    assert disputed.state == ScoreState.DISPUTED
    assert standings(game.players(), game.score_events())[0].score == 15
    game.review(target.event_id, reinstate=True)
    assert standings(game.players(), game.score_events())[0].score == 35
    assert game.replay_scores() == game.score_events()


async def test_finalize_waits_for_the_window_then_names_the_winner(runtime: Runtime) -> None:
    """The window must close first; the winner is first by the tie-break."""
    play(runtime)
    with pytest.raises(GameError) as early:
        runtime.game.finalize()
    assert early.value.code == "DISPUTE_WINDOW_OPEN"
    _clock(runtime).advance(seconds=21)
    group = runtime.game.finalize()
    assert group.status == GroupStatus.FINALIZED
    assert group.result is not None
    assert group.result.standings[0].name == "Alice"
    finalized = runtime.control.ledger.events(types=[EventType.GROUP_FINALIZED])[0]
    assert finalized.payload["winner_name"] == "Alice"
    with pytest.raises(GameError) as late:
        runtime.game.dispute(play_ids(runtime)[1], "sev_x", "too late")
    assert late.value.code == "DISPUTE_WINDOW_CLOSED"


def play_ids(runtime: Runtime) -> list[str]:
    """Return the players' ids.

    Args:
        runtime: The runtime.

    Returns:
        The ids, by seat.
    """
    return [p.id for p in runtime.game.players()]


async def test_the_agent_buys_the_prize_behind_the_gate_and_the_group_is_fulfilled(
    runtime: Runtime,
) -> None:
    """On the mock: 57.49 landed under a 67.50 ceiling, allowed, an order id on the ledger."""
    play(runtime)
    _clock(runtime).advance(seconds=21)
    runtime.game.finalize()
    assert runtime.purchase is not None
    buyer = PrizeBuyer(runtime.control, runtime.purchase, backend="mock", enrollment_id=None)
    steps: list[PrizePurchase] = []

    def on_step(p: PrizePurchase) -> None:
        steps.append(p)
        runtime.game.purchase_progress(p)

    bought = await buyer.buy(ceiling=runtime.game.pool().ceiling, winner="Alice", on_step=on_step)
    assert bought.status == "PURCHASED", bought.error
    assert bought.order_id is not None
    assert bought.quote_final_amount == Decimal("57.49")
    assert bought.gate is not None
    assert bought.gate["disposition"] == "allow"
    assert [s.step for s in steps][:4] == ["search", "quote", "gate", "checkout"]
    group = runtime.game.record_purchase(bought)
    assert group.status == GroupStatus.FULFILLED
    ledger = runtime.control.ledger
    purchased = ledger.events(types=[EventType.PRIZE_PURCHASED])[0]
    assert purchased.payload["order_id"] == bought.order_id
    assert purchased.payload["winner_name"] == "Alice"
    assert ledger.events(types=[EventType.CHECKOUT_COMPLETED])
    assert ledger.verify_chain().ok


async def test_a_pool_that_cannot_cover_the_quote_is_refused_by_the_gate(runtime: Runtime) -> None:
    """A 50.00 ceiling under a 57.49 quote: refused, the group stays finalised for a retry."""
    play(runtime)
    _clock(runtime).advance(seconds=21)
    runtime.game.finalize()
    assert runtime.purchase is not None
    buyer = PrizeBuyer(runtime.control, runtime.purchase, backend="mock", enrollment_id=None)
    refused = await buyer.buy(ceiling=Decimal("50.00"), winner="Alice", on_step=lambda p: None)
    assert refused.status == "FAILED"
    assert refused.gate is not None
    assert refused.gate["disposition"] == "refuse"
    group = runtime.game.record_purchase(refused)
    assert group.status == GroupStatus.FINALIZED
    assert runtime.control.ledger.events(types=[EventType.PRIZE_PURCHASE_FAILED])


async def test_the_preview_quote_reads_the_landed_price(runtime: Runtime) -> None:
    """The start-up preview: the keyboard, 49.99 plus 7.50, nothing bought."""
    assert runtime.purchase is not None
    preview = await preview_quote(runtime.reap, runtime.purchase)
    assert preview.name == "Keychron B40 Wireless Keyboard"
    assert preview.final_amount == Decimal("57.49")
    assert preview.shipping == Decimal("7.50")


async def test_no_verified_progress_means_no_winner_and_refunds(runtime: Runtime) -> None:
    """Nobody scored: cancelled at finalise, every entry refunded."""
    game = runtime.game
    ids = [game.join(n).id for n in ("Alice", "Ben", "Chloe")]
    for pid, draft in zip(ids, DRAFTS, strict=True):
        game.record_intake(pid, "goal", CoachTurn("ok", draft, "fake"))
        game.lock_contract(pid)
    for pid in ids:
        game.accept(pid)
    _to_start(runtime)
    _clock(runtime).advance(seconds=4 * WEEK + 22)
    game.tick()
    _clock(runtime).advance(seconds=21)
    assert game.finalize().status == GroupStatus.CANCELLED


async def test_the_sandbox_without_an_enrolment_refuses_before_any_call(runtime: Runtime) -> None:
    """No enrolment on the sandbox: failed with the operator's step named, nothing created."""
    assert runtime.purchase is not None
    buyer = PrizeBuyer(runtime.control, runtime.purchase, backend="sandbox", enrollment_id=None)
    failed = await buyer.buy(ceiling=Decimal("67.50"), winner="Alice", on_step=lambda p: None)
    assert failed.status == "FAILED"
    assert failed.error is not None
    assert "REAP_ENROLLMENT_ID" in failed.error
    assert not runtime.control.ledger.events(types=[EventType.OBJECTIVE_CREATED])


async def test_the_mock_ignores_a_sandbox_enrolment_in_the_settings(tmp_path: Path) -> None:
    """A sandbox enrolment id left in .env never reaches the mock's purchase."""
    settings = Settings.model_validate(
        {
            "database_path": tmp_path / "db.sqlite",
            "snapshot_path": tmp_path / "snap.json",
            "market_mode": MarketMode.MOCK,
            "reap_enrollment_id": "dc791bae-f504-4011-ae48-7cd7cafd7a46",
        }
    )
    runtime = build_runtime(settings)
    try:
        assert runtime.buyer is not None
        assert runtime.buyer.enrollment_id is None
    finally:
        await runtime.aclose()

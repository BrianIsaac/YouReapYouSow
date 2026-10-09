"""The challenge's routes through the real app on the mock: join to a running challenge."""

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.conftest import START
from tests.game.helpers import DRAFTS
from tests.game.test_coach import FakeChat
from youreapyousow.api.app import Runtime, build_runtime, create_app
from youreapyousow.clock import ManualClock
from youreapyousow.config import Settings
from youreapyousow.game.coach import Coach, CoachTurn
from youreapyousow.game.llm import Link
from youreapyousow.game.models import ChatTurn
from youreapyousow.game.verifier import Verifier
from youreapyousow.market.service import MarketMode
from youreapyousow.reap.client import ReapMock

pytestmark = pytest.mark.anyio


class ScriptedCoach(Coach):
    """A coach that proposes the handover's three contracts in turn."""

    def __init__(self) -> None:
        """Start at the first draft."""
        super().__init__([], duration_days=28)
        self.drafts = list(DRAFTS)

    async def respond(self, transcript: tuple[ChatTurn, ...], message: str) -> CoachTurn:
        """Propose the next draft.

        Args:
            transcript: Ignored.
            message: Ignored.

        Returns:
            The turn.
        """
        del transcript, message
        return CoachTurn("Here is your contract.", self.drafts.pop(0), "fake")


@dataclass
class Running:
    """The app, its runtime and its manual clock."""

    http: httpx.AsyncClient
    runtime: Runtime
    clock: ManualClock


@pytest.fixture
async def running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Running]:
    """The app on the mock with a scripted coach, a fake photo reader and a manual clock.

    Yields:
        The running app.
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

    async def factory() -> Runtime:
        runtime = build_runtime(settings, clock=clock)
        runtime.coach = ScriptedCoach()
        reading = json.dumps({"shows": True, "count": 9, "note": "nine push-ups"})
        runtime.verifier = Verifier([Link("openai:fake", FakeChat("m", [reading] * 5))])
        return runtime

    app: FastAPI = create_app(factory)
    async with app.router.lifespan_context(app):
        runtime: Runtime = app.state.runtime
        await asyncio.gather(*runtime.tasks)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://game") as client:
            yield Running(client, runtime, clock)


@pytest.fixture
def http(running: Running) -> httpx.AsyncClient:
    """The client on the running app.

    Args:
        running: The running app.

    Returns:
        The client.
    """
    return running.http


async def test_three_players_join_talk_lock_and_accept(running: Running) -> None:
    """The happy path to ACTIVE at the fixed start, with the state the screen polls."""
    http = running.http
    state = (await http.get("/api/state")).json()
    assert state["group"]["status"] == "OPEN_FOR_JOINING"
    assert state["clock"]["label"] == "Demo time: 1 minute = 1 week"
    assert state["pool"]["stand_in"].startswith("The pool is test USDC in the Kwal vault")
    assert state["pool"]["at_capacity"] == {
        "entries": 3,
        "gross": "75.00",
        "buffer": "7.50",
        "ceiling": "67.50",
        "surplus": "10.01",
    }
    assert state["pool"]["vault_source"].startswith("configured stand-in balance")
    ids: list[str] = []
    for name in ("Alice", "Ben", "Chloe"):
        answer = await http.post("/api/join", json={"name": name})
        assert answer.status_code == 200
        ids.append(str(answer.json()["player_id"]))
    for pid in ids:
        turn = (await http.post("/api/intake", json={"player_id": pid, "message": "hi"})).json()
        assert turn["done"] is True
        assert [m["max_points"] for m in turn["contract"]["milestones"]] == [15, 20, 25, 40]
    edited = await http.put("/api/contract", json={"player_id": ids[0], "target_value": 22})
    assert edited.json()["contract"]["target"]["value"] == 22
    for pid in ids:
        assert (await http.post("/api/contract/lock", json={"player_id": pid})).status_code == 200
    for pid in ids:
        assert (await http.post("/api/accept", json={"player_id": pid})).status_code == 200
    waiting = (await http.get("/api/state")).json()
    assert waiting["group"]["status"] == "READY_FOR_ACCEPTANCE"
    assert all(p["accepted"] for p in waiting["players"])
    running.clock.advance(seconds=3600)
    state = (await http.get("/api/state")).json()
    assert state["group"]["status"] == "ACTIVE"
    assert state["group"]["rubric_locked"] is True
    assert state["pool"]["gross"] == "75.00"
    assert state["pool"]["ceiling"] == "67.50"
    assert state["players"][0]["contract"]["milestones"][0]["due_at"] is not None
    assert state["ledger_intact"] is True
    ledger = (await http.get("/api/ledger")).json()
    assert ledger["chain_intact"] is True
    assert any(e["type"] == "group.started" for e in ledger["events"])


async def test_a_refusal_has_a_code_and_a_line(http: httpx.AsyncClient) -> None:
    """A wrong-state action and a malformed body both answer the error shape."""
    answer = await http.post("/api/accept", json={"player_id": "ply_nobody"})
    assert answer.status_code == 409
    assert answer.json()["error"]["code"] == "WRONG_STATE"
    malformed = await http.post("/api/join", json={})
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "INVALID_REQUEST"


async def _active(http: httpx.AsyncClient, clock: ManualClock) -> list[str]:
    ids: list[str] = []
    for name in ("Alice", "Ben", "Chloe"):
        ids.append(str((await http.post("/api/join", json={"name": name})).json()["player_id"]))
    for pid in ids:
        await http.post("/api/intake", json={"player_id": pid, "message": "hi"})
        await http.post("/api/contract/lock", json={"player_id": pid})
    for pid in ids:
        await http.post("/api/accept", json={"player_id": pid})
    clock.advance(seconds=3600)
    assert (await http.get("/api/state")).json()["group"]["status"] == "ACTIVE"
    return ids


async def test_a_photo_check_in_is_read_advisorily_and_scored_by_the_rubric(
    running: Running,
) -> None:
    """A multipart photo: the advisory beside it, 15 points, the leaderboard moved."""
    http = running.http
    alice, ben, _ = await _active(http, running.clock)
    answer = await http.post(
        "/api/checkin",
        data={"player_id": alice, "milestone": "0", "value": "9"},
        files={"file": ("pushups.jpg", b"\xff\xd8 photo", "image/jpeg")},
    )
    assert answer.status_code == 200
    event = answer.json()["event"]
    assert event["state"] == "VERIFIED"
    assert event["delta"] == 15
    assert event["advisory"]["model"] == "openai:fake"
    assert event["advisory"]["agrees"] is True
    photo = await http.get(f"/api/evidence/{event['evidence_id']}")
    assert photo.content == b"\xff\xd8 photo"
    log = await http.post(
        "/api/checkin", data={"player_id": ben, "milestone": "0", "value": "1", "note": "5 km"}
    )
    assert log.json()["event"]["evidence_kind"] == "log"
    state = log.json()["state"]
    assert [row["name"] for row in state["leaderboard"]] == ["Alice", "Ben", "Chloe"]
    missing = await http.post(
        "/api/checkin", data={"player_id": alice, "milestone": "1", "value": "11"}
    )
    assert missing.json()["error"]["code"] == "MILESTONE_NOT_OPEN"


async def test_finalize_buys_the_prize_and_the_state_shows_the_order(running: Running) -> None:
    """Through the routes on the mock: the winner, the order id, the stand-in line, FULFILLED."""
    http = running.http
    alice, *_ = await _active(http, running.clock)
    await http.post(
        "/api/checkin",
        data={"player_id": alice, "milestone": "0", "value": "9"},
        files={"file": ("p.jpg", b"\xff\xd8 x", "image/jpeg")},
    )
    state = (await http.get("/api/state")).json()
    assert state["prize"]["quote"]["final_amount"] == "57.49"
    assert state["pool"]["surplus"] == "10.01"
    early = await http.post("/api/finalize", json={})
    assert early.json()["error"]["code"] == "WRONG_STATE"
    running.clock.advance(seconds=4 * 60 + 1)
    assert (await http.get("/api/state")).json()["group"]["status"] == "DISPUTE_WINDOW"
    running.clock.advance(seconds=21)
    answer = (await http.post("/api/finalize", json={})).json()
    result = answer["result"]
    assert result["winner"]["name"] == "Alice"
    purchase = result["purchase"]
    assert purchase["status"] == "PURCHASED", purchase["error"]
    assert purchase["order_id"]
    assert purchase["gate"]["disposition"] == "allow"
    assert purchase["stand_in"].startswith("The pool is test USDC")
    assert answer["state"]["group"]["status"] == "FULFILLED"
    tail = [e["type"] for e in answer["state"]["ledger_tail"]]
    assert "prize.purchased" in tail
    ledger = (await http.get("/api/ledger")).json()["events"]
    summaries = [e["summary"] for e in ledger]
    assert "Authority gate on the proposal: allow (all_rules_passed)." in summaries
    assert "Authority gate at the claim: allow (all_rules_passed)." in summaries
    assert "Reap quoted Keychron: 57.49 USD landed in Singapore." in summaries
    assert "The agent proposed buying the prize for Alice at 57.49 USD." in summaries


async def test_the_score_events_and_the_ledger_read_back_for_the_screen(running: Running) -> None:
    """``/api/events`` per player and ``/api/ledger`` after a position, each with summaries."""
    http = running.http
    alice, ben, _ = await _active(http, running.clock)
    await http.post("/api/checkin", data={"player_id": ben, "milestone": "0", "value": "1"})
    events = (await http.get("/api/events", params={"player_id": ben})).json()["events"]
    assert [e["state"] for e in events] == ["VERIFIED"]
    assert (await http.get("/api/events", params={"player_id": alice})).json()["events"] == []
    ledger = (await http.get("/api/ledger")).json()["events"]
    last = ledger[-1]
    assert last["type"] == "score.recorded"
    assert last["summary"] == "Ben: milestone 1 verified, +15."
    after = (await http.get("/api/ledger", params={"after_seq": last["seq"]})).json()
    assert after["events"] == []
    assert after["chain_intact"] is True


async def test_the_drops_are_listed_and_each_runs_its_own_group(running: Running) -> None:
    """Five drops, the featured first; a seat in one is keyed by its drop alone."""
    http = running.http
    drops = (await http.get("/api/drops")).json()["drops"]
    assert [d["drop_id"] for d in drops] == [
        "keychron-b40",
        "ugreen-mouse",
        "anker-hub",
        "prism-monitor",
        "boxgreen-snacks",
    ]
    assert drops[0]["featured"] is True
    assert [d["duration_days"] for d in drops] == [28, 7, 14, 28, 7]
    for d in drops:
        assert d["pool_at_capacity"]["surplus"] is not None
        assert float(d["pool_at_capacity"]["surplus"]) > 0
    mouse = next(d for d in drops if d["drop_id"] == "ugreen-mouse")
    assert mouse["prize"]["currency"] == "SGD"
    assert mouse["prize"]["live_purchase"] is False
    assert mouse["note"]
    joined = await http.post("/api/drops/ugreen-mouse/join", json={"name": "Dan"})
    assert joined.status_code == 200
    state = joined.json()["state"]
    assert state["drop"]["drop_id"] == "ugreen-mouse"
    assert state["drop"]["seats"]["taken"] == 1
    featured = (await http.get("/api/drops/keychron-b40")).json()
    assert featured["players"] == []
    unknown = await http.get("/api/drops/nothing")
    assert unknown.json()["error"]["code"] == "UNKNOWN_DROP"


async def test_a_republished_drop_starts_after_the_lead_given(running: Running) -> None:
    """Reset with a 90 second lead: a fresh group whose fixed start is 90 seconds away."""
    http = running.http
    state = (await http.post("/api/drops/anker-hub/reset", json={"lead_s": 90})).json()["state"]
    group = state["group"]
    assert group["status"] == "OPEN_FOR_JOINING"
    assert group["starts_at"] is not None
    assert state["drop"]["duration_days"] == 14


async def test_the_stand_in_vault_holds_every_drop_at_capacity(running: Running) -> None:
    """Five drops full at once reserve 432.00; the 500.00 stand-in covers them."""
    http = running.http
    drops = (await http.get("/api/drops")).json()["drops"]
    total = sum(float(d["pool_at_capacity"]["gross"]) for d in drops)
    assert total == 432.0
    for d in drops:
        for name in ("A", "B", "C"):
            answer = await http.post(f"/api/drops/{d['drop_id']}/join", json={"name": name})
            assert answer.status_code == 200, answer.json()


async def test_a_disputed_check_in_reads_as_one_line_and_is_reviewed_by_either_id(
    running: Running,
) -> None:
    """``/events`` folds a dispute into its check-in; review takes the dispute's own id too."""
    http = running.http
    alice, ben, _ = await _active(http, running.clock)
    await http.post(
        "/api/checkin",
        data={"player_id": alice, "milestone": "0", "value": "9"},
        files={"file": ("p.jpg", b"\\xff\\xd8 y", "image/jpeg")},
    )
    running.clock.advance(seconds=4 * 60 + 1)
    await http.get("/api/state")
    line = (await http.get("/api/events")).json()["events"][0]
    disputed = await http.post(
        "/api/dispute", json={"player_id": ben, "event_id": line["event_id"], "reason": "blurry"}
    )
    dispute_id = disputed.json()["event"]["event_id"]
    folded = (await http.get("/api/events")).json()["events"]
    assert len(folded) == 1
    assert folded[0]["state"] == "DISPUTED"
    assert folded[0]["delta"] == 0
    assert [h["state"] for h in folded[0]["history"]] == ["VERIFIED", "DISPUTED"]
    reviewed = await http.post(
        "/api/dispute/review", json={"event_id": dispute_id, "reinstate": True}
    )
    assert reviewed.status_code == 200
    again = (await http.get("/api/events")).json()["events"][0]
    assert (again["state"], again["delta"]) == ("VERIFIED", 15)


async def _finalised_awaiting(running: Running) -> dict[str, Any]:
    """Play to the finish with the featured drop's checkout waiting on its approval page.

    Args:
        running: The running app.

    Returns:
        The purchase as ``/api/finalize`` answers it.
    """
    http = running.http
    drop = running.runtime.featured
    drop.follow_every_s = 0.0
    control = drop.buyer.control
    control.settings = dataclasses.replace(control.settings, simulate_completed_when_allowed=False)
    alice, *_ = await _active(http, running.clock)
    await http.post(
        "/api/checkin",
        data={"player_id": alice, "milestone": "0", "value": "9"},
        files={"file": ("p.jpg", b"\xff\xd8 x", "image/jpeg")},
    )
    running.clock.advance(seconds=4 * 60 + 1)
    await http.get("/api/state")
    running.clock.advance(seconds=21)
    answer = (await http.post("/api/finalize", json={})).json()
    purchase: dict[str, Any] = answer["result"]["purchase"]
    return purchase


async def test_finalize_waits_on_approval_and_a_poll_records_the_order(running: Running) -> None:
    """REQUIRES_ACTION then COMPLETED through the routes: waiting, then FULFILLED on a poll."""
    http = running.http
    purchase = await _finalised_awaiting(running)
    assert purchase["status"] == "AWAITING_APPROVAL", purchase["error"]
    assert purchase["approval_url"].endswith(purchase["checkout_id"])
    assert purchase["approval_expires_at"]
    again = await http.post("/api/finalize", json={})
    assert again.json()["error"]["code"] == "PURCHASE_IN_PROGRESS"
    state = (await http.get("/api/state")).json()
    assert state["group"]["status"] == "FINALIZED"
    assert state["result"]["purchase"]["status"] == "AWAITING_APPROVAL"
    reap = running.runtime.featured.buyer.control.reap
    assert isinstance(reap, ReapMock)
    reap.agentic.approve_checkout(purchase["checkout_id"])
    running.clock.advance(seconds=1)
    state = (await http.get("/api/drops/keychron-b40")).json()
    bought = state["result"]["purchase"]
    assert bought["status"] == "PURCHASED"
    assert bought["order_id"]
    assert bought["approved_at"]
    assert state["group"]["status"] == "FULFILLED"
    summaries = [e["summary"] for e in state["ledger_tail"]]
    assert any(s.startswith("Bought for Alice: order ") for s in summaries)


async def test_retry_reopens_a_checkout_after_the_page_expired(running: Running) -> None:
    """The page expired unused: FAILED on a poll, then a fresh checkout waits again."""
    http = running.http
    early = await http.post("/api/drops/keychron-b40/purchase/retry", json={})
    assert early.json()["error"]["code"] == "WRONG_STATE"
    purchase = await _finalised_awaiting(running)
    open_page = await http.post("/api/drops/keychron-b40/purchase/retry", json={})
    assert open_page.json()["error"]["code"] == "PURCHASE_IN_PROGRESS"
    running.clock.advance(seconds=15 * 60 + 1)
    expired = (await http.get("/api/state")).json()["result"]["purchase"]
    assert expired["status"] == "FAILED"
    assert "expired" in expired["error"]
    answer = (await http.post("/api/drops/keychron-b40/purchase/retry", json={})).json()
    again = answer["result"]["purchase"]
    assert again["status"] == "AWAITING_APPROVAL", again["error"]
    assert again["checkout_id"] != purchase["checkout_id"]
    assert again["gate"]["disposition"] == "allow"
    assert answer["state"]["group"]["status"] == "FINALIZED"

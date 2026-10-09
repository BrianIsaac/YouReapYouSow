"""The challenge's routes through the real app on the mock: join to a running challenge."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

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


async def test_three_players_join_talk_lock_and_accept(http: httpx.AsyncClient) -> None:
    """The happy path to ACTIVE through the routes, with the state the screen polls."""
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


async def _active(http: httpx.AsyncClient) -> list[str]:
    ids: list[str] = []
    for name in ("Alice", "Ben", "Chloe"):
        ids.append(str((await http.post("/api/join", json={"name": name})).json()["player_id"]))
    for pid in ids:
        await http.post("/api/intake", json={"player_id": pid, "message": "hi"})
        await http.post("/api/contract/lock", json={"player_id": pid})
    for pid in ids:
        await http.post("/api/accept", json={"player_id": pid})
    return ids


async def test_a_photo_check_in_is_read_advisorily_and_scored_by_the_rubric(
    http: httpx.AsyncClient,
) -> None:
    """A multipart photo: the advisory beside it, 15 points, the leaderboard moved."""
    alice, ben, _ = await _active(http)
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
    alice, *_ = await _active(http)
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


async def test_the_score_events_and_the_ledger_read_back_for_the_screen(running: Running) -> None:
    """``/api/events`` per player and ``/api/ledger`` after a position, each with summaries."""
    http = running.http
    alice, ben, _ = await _active(http)
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

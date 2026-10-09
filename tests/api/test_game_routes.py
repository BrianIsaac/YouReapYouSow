"""The challenge's routes through the real app on the mock: join to a running challenge."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tests.game.helpers import DRAFTS
from youreapyousow.api.app import Runtime, build_runtime, create_app
from youreapyousow.config import Settings
from youreapyousow.game.coach import Coach, CoachTurn
from youreapyousow.game.models import ChatTurn
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


@pytest.fixture
async def http(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The app on the mock with a scripted coach.

    Yields:
        A client on the app.
    """
    settings = Settings.model_validate(
        {
            "database_path": tmp_path / "db.sqlite",
            "snapshot_path": tmp_path / "snap.json",
            "market_mode": MarketMode.MOCK,
            "evidence_dir": tmp_path / "evidence",
        }
    )

    async def factory() -> Runtime:
        runtime = build_runtime(settings)
        runtime.coach = ScriptedCoach()
        return runtime

    app: FastAPI = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://game") as client:
            yield client


async def test_three_players_join_talk_lock_and_accept(http: httpx.AsyncClient) -> None:
    """The happy path to ACTIVE through the routes, with the state the screen polls."""
    state = (await http.get("/api/state")).json()
    assert state["group"]["status"] == "OPEN_FOR_JOINING"
    assert state["clock"]["label"] == "Demo time: 1 minute = 1 week"
    assert state["pool"]["stand_in"].startswith("The pool is test USDC in the Kwal vault")
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

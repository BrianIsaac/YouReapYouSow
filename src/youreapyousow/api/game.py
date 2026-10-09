"""The challenge's HTTP routes, under ``/api``: what the room screen calls and polls.

Every route moves the group's clock first (``tick``). A refusal is a 4xx with
``{"error": {"code", "message"}}``. Mutations are serialised by one lock; the model calls
(the coach, the photo reader) happen outside it, so a slow model never blocks the room.
"""

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, JsonValue

from youreapyousow.game.models import GroupStatus, PrizePurchase
from youreapyousow.game.service import MAX_EVIDENCE_BYTES, EvidenceIn, GameError
from youreapyousow.game.view import (
    contract_view,
    ledger_view,
    result_view,
    score_view,
    state_view,
)

if TYPE_CHECKING:
    from youreapyousow.api.app import Runtime

router = APIRouter(prefix="/api")


def runtime_of(request: Request) -> "Runtime":
    """Return the app's runtime.

    Args:
        request: The request.

    Returns:
        The runtime.
    """
    runtime: Runtime = request.app.state.runtime
    return runtime


def refused(error: GameError) -> JSONResponse:
    """Render a refusal.

    Args:
        error: The refusal.

    Returns:
        The response.
    """
    return JSONResponse(
        {"error": {"code": error.code, "message": error.message}}, status_code=error.status
    )


def state_of(runtime: "Runtime") -> dict[str, JsonValue]:
    """Build the polled state after applying the clock.

    Args:
        runtime: The runtime.

    Returns:
        The state.
    """
    game = runtime.game
    game.tick()
    events = game.ledger.events(after_seq=max(0, _last_seq(runtime) - 12))
    return state_view(game, runtime.prize_info(), events)


def _last_seq(runtime: "Runtime") -> int:
    rows = runtime.game.db.read("SELECT MAX(seq) AS seq FROM events")
    value: Any = rows[0]["seq"] if rows else 0
    return int(value or 0)


class JoinBody(BaseModel):
    """``POST /api/join``."""

    name: str = Field(max_length=200)


class PlayerBody(BaseModel):
    """A body that names the player."""

    player_id: str


class IntakeBody(PlayerBody):
    """``POST /api/intake``."""

    message: str = Field(min_length=1, max_length=2000)


class DisputeBody(PlayerBody):
    """``POST /api/dispute``."""

    event_id: str
    reason: str = Field(default="", max_length=500)


class ReviewBody(BaseModel):
    """``POST /api/dispute/review``: the reviewer's verdict."""

    event_id: str
    reinstate: bool


class ContractBody(PlayerBody):
    """``PUT /api/contract``: the fields the player may edit."""

    goal_statement: str | None = None
    baseline_value: float | None = None
    target_value: float | None = None
    unit: str | None = None
    milestone_targets: list[float] | None = None


@router.get("/state")
async def get_state(request: Request) -> JSONResponse:
    """Return the polled state.

    Args:
        request: The request.

    Returns:
        The state.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        return JSONResponse(state_of(runtime))


@router.post("/join")
async def join(body: JoinBody, request: Request) -> JSONResponse:
    """Reserve a seat and an entry.

    Args:
        body: The name.
        request: The request.

    Returns:
        The new player id and the state.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        try:
            player = runtime.game.join(body.name)
        except GameError as error:
            return refused(error)
        return JSONResponse({"player_id": player.id, "state": state_of(runtime)})


@router.post("/intake")
async def intake(body: IntakeBody, request: Request) -> JSONResponse:
    """Talk to the coach.

    Args:
        body: The player and the message.
        request: The request.

    Returns:
        The coach's reply, the contract when proposed, and whether it is.
    """
    runtime = runtime_of(request)
    try:
        async with runtime.game_lock:
            player = runtime.game.intake_player(body.player_id)
        turn = await runtime.coach.respond(player.transcript, body.message)
        async with runtime.game_lock:
            saved = runtime.game.record_intake(body.player_id, body.message, turn)
            group = runtime.game.group()
    except GameError as error:
        return refused(error)
    contract = None if saved.contract is None else contract_view(saved.contract, group)
    return JSONResponse(
        {
            "reply": turn.reply,
            "contract": contract,
            "done": contract is not None,
            "model": turn.model,
        }
    )


@router.put("/contract")
async def edit_contract(body: ContractBody, request: Request) -> JSONResponse:
    """Edit the player's proposed contract.

    Args:
        body: The edits.
        request: The request.

    Returns:
        The edited contract.
    """
    runtime = runtime_of(request)
    changes = body.model_dump(exclude_none=True, exclude={"player_id"})
    async with runtime.game_lock:
        try:
            contract = runtime.game.edit_contract(body.player_id, changes)
        except GameError as error:
            return refused(error)
        return JSONResponse({"contract": contract_view(contract, runtime.game.group())})


@router.post("/contract/lock")
async def lock_contract(body: PlayerBody, request: Request) -> JSONResponse:
    """Lock the player's contract.

    Args:
        body: The player.
        request: The request.

    Returns:
        The locked contract.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        try:
            contract = runtime.game.lock_contract(body.player_id)
        except GameError as error:
            return refused(error)
        return JSONResponse({"contract": contract_view(contract, runtime.game.group())})


@router.post("/accept")
async def accept(body: PlayerBody, request: Request) -> JSONResponse:
    """Accept the group agreement.

    Args:
        body: The player.
        request: The request.

    Returns:
        The state.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        try:
            runtime.game.accept(body.player_id)
        except GameError as error:
            return refused(error)
        return JSONResponse({"state": state_of(runtime)})


@router.post("/decline")
async def decline(body: PlayerBody, request: Request) -> JSONResponse:
    """Decline the group agreement, cancelling the group.

    Args:
        body: The player.
        request: The request.

    Returns:
        The state.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        try:
            runtime.game.decline(body.player_id)
        except GameError as error:
            return refused(error)
        return JSONResponse({"state": state_of(runtime)})


@router.post("/reset")
async def reset(request: Request) -> JSONResponse:
    """Open a fresh group, refunding an unfinished one.

    Args:
        request: The request.

    Returns:
        The state.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        runtime.game.reset()
        return JSONResponse({"state": state_of(runtime)})


@router.get("/ledger")
async def ledger(request: Request, after_seq: int = 0) -> JSONResponse:
    """Return the ledger after a position, and whether the chain is intact.

    Args:
        request: The request.
        after_seq: Only events after this position.

    Returns:
        The events.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        names = {p.id: p.name for p in runtime.game.players()}
        events = runtime.game.ledger.events(after_seq=after_seq)
        return JSONResponse(
            {
                "chain_intact": runtime.game.ledger.verify_chain().ok,
                "events": [ledger_view(e, names) for e in events],
            }
        )


@router.get("/events")
async def score_events(request: Request, player_id: str | None = None) -> JSONResponse:
    """Return the group's score events, optionally one player's.

    Args:
        request: The request.
        player_id: Only this player's, when given.

    Returns:
        The events.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        events = runtime.game.score_events()
        if player_id is not None:
            events = [e for e in events if e.participant_id == player_id]
        return JSONResponse({"events": [score_view(e) for e in events]})


@router.post("/checkin")
async def checkin(
    request: Request,
    *,
    player_id: Annotated[str, Form()],
    milestone: Annotated[int, Form()],
    value: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> JSONResponse:
    """Check in against a milestone, with a photo, a clip, or a log entry.

    The rules that need no model are checked first; a photo is then read by the vision
    model outside the lock, and the rubric decides the points.

    Args:
        request: The request.
        player_id: The player.
        milestone: The milestone index, from 0.
        value: The value the player claims.
        note: The player's note.
        file: The photo or clip, if any.

    Returns:
        The score event and the state.
    """
    runtime = runtime_of(request)
    try:
        claimed = Decimal(value)
    except InvalidOperation:
        return refused(GameError("INVALID_VALUE", "The value must be a number.", 422))
    evidence = None
    if file is not None and file.filename:
        content = await file.read(MAX_EVIDENCE_BYTES + 1)
        if content:
            evidence = EvidenceIn(content, file.content_type or "application/octet-stream")
    try:
        async with runtime.game_lock:
            contract = runtime.game.checkin_contract(player_id, milestone)
            if evidence is not None:
                evidence.kind()
        advisory = None
        if evidence is not None:
            advisory = await runtime.verifier.read(evidence, contract, milestone, claimed)
        async with runtime.game_lock:
            event = runtime.game.checkin(
                player_id,
                milestone=milestone,
                value=claimed,
                evidence=evidence,
                note=note,
                advisory=advisory,
            )
            return JSONResponse({"event": score_view(event), "state": state_of(runtime)})
    except GameError as error:
        return refused(error)


@router.get("/evidence/{evidence_id}", response_model=None)
async def evidence_file(evidence_id: str, request: Request) -> Response:
    """Return a stored photo or clip.

    Args:
        evidence_id: The evidence id.
        request: The request.

    Returns:
        The file, or a 404.
    """
    path = runtime_of(request).game.evidence_path(evidence_id)
    if path is None:
        return refused(GameError("UNKNOWN_EVIDENCE", "No such evidence.", 404))
    return FileResponse(path)


@router.post("/dispute")
async def dispute(body: DisputeBody, request: Request) -> JSONResponse:
    """Dispute a verified check-in inside the window.

    Args:
        body: The player, the check-in and why.
        request: The request.

    Returns:
        The dispute event.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        try:
            event = runtime.game.dispute(body.player_id, body.event_id, body.reason)
        except GameError as error:
            return refused(error)
        return JSONResponse({"event": score_view(event)})


@router.post("/dispute/review")
async def review(body: ReviewBody, request: Request) -> JSONResponse:
    """Resolve a disputed check-in (the reviewer's power).

    Args:
        body: The check-in and the verdict.
        request: The request.

    Returns:
        The review event.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        try:
            event = runtime.game.review(body.event_id, reinstate=body.reinstate)
        except GameError as error:
            return refused(error)
        return JSONResponse({"event": score_view(event)})


@router.post("/finalize")
async def finalize(request: Request) -> JSONResponse:
    """Name the winner, then have the agent buy the prize through Reap behind the gate.

    Args:
        request: The request.

    Returns:
        The result with the purchase, and the state.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        try:
            group = runtime.game.finalize()
        except GameError as error:
            return refused(error)
        result = group.result
        if group.status != GroupStatus.FINALIZED or result is None:
            return JSONResponse({"result": None, "state": state_of(runtime)})
        if result.purchase is not None and result.purchase.status == "BUYING":
            return refused(GameError("PURCHASE_IN_PROGRESS", "The agent is already buying."))
        if runtime.buyer is None:
            return refused(GameError("PURCHASE_FAILED", "No prize purchase file is loaded.", 503))
        winner = next(s for s in result.standings if s.player_id == result.winner_id)
        ceiling = runtime.game.pool().ceiling
        runtime.game.purchase_progress(
            PrizePurchase(status="BUYING", step="search", backend=runtime.buyer.backend)
        )
    bought = await runtime.buyer.buy(
        ceiling=ceiling, winner=winner.name, on_step=runtime.game.purchase_progress
    )
    async with runtime.game_lock:
        group = runtime.game.record_purchase(bought)
        players = runtime.game.players()
        body = None if group.result is None else result_view(group.result, players)
        return JSONResponse({"result": body, "state": state_of(runtime)})

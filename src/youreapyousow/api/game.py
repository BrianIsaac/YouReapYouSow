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

from youreapyousow.game.drops import Drop
from youreapyousow.game.models import GroupStatus, PrizePurchase
from youreapyousow.game.service import MAX_EVIDENCE_BYTES, EvidenceIn, GameError, GameService
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


def drop_of(runtime: "Runtime", drop_id: str | None) -> Drop:
    """Return the drop a route names, or the featured drop for the un-keyed routes.

    Args:
        runtime: The runtime.
        drop_id: The drop's id, or None.

    Returns:
        The drop.

    Raises:
        GameError: If no such drop is on offer.
    """
    if drop_id is None:
        return runtime.featured
    drop = runtime.drops.get(drop_id)
    if drop is None:
        raise GameError("UNKNOWN_DROP", "No such drop is on offer.", 404)
    return drop


def state_of(drop: Drop) -> dict[str, JsonValue]:
    """Build a drop's polled state after applying the clock.

    Args:
        drop: The drop.

    Returns:
        The state, with the drop's summary.
    """
    service = drop.service
    service.tick()
    group_ids = set(service.group_ids())
    events = [
        e
        for e in service.ledger.events(after_seq=max(0, _last_seq(service) - 400))
        if e.objective_id in group_ids or e.objective_id not in _all_groups(service)
    ]
    state = state_view(service, dict(drop.prize), events)
    state["drop"] = drop.summary()
    return state


def _all_groups(service: GameService) -> set[str]:
    rows = service.db.read("SELECT id FROM records WHERE kind = 'game_group'")
    return {str(r["id"]) for r in rows}


def _last_seq(service: GameService) -> int:
    rows = service.db.read("SELECT MAX(seq) AS seq FROM events")
    value: Any = rows[0]["seq"] if rows else 0
    return int(value or 0)


class ResetBody(BaseModel):
    """``POST /api/drops/{drop_id}/reset``: when the republished drop starts."""

    lead_s: float | None = Field(default=None, gt=0, le=86400)


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


@router.get("/drops")
async def list_drops(request: Request) -> JSONResponse:
    """Return the drops on offer.

    Args:
        request: The request.

    Returns:
        Each drop's summary, the featured one first.
    """
    runtime = runtime_of(request)
    async with runtime.game_lock:
        summaries: list[JsonValue] = []
        for index, drop in enumerate(runtime.drops.values()):
            drop.service.tick()
            summary = drop.summary()
            summary["featured"] = index == 0
            summaries.append(summary)
        return JSONResponse({"drops": summaries})


@router.get("/state")
@router.get("/drops/{drop_id}")
async def get_state(request: Request, drop_id: str | None = None) -> JSONResponse:
    """Return the polled state.

    Args:
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The state.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        return JSONResponse(state_of(drop))


@router.post("/join")
@router.post("/drops/{drop_id}/join")
async def join(body: JoinBody, request: Request, drop_id: str | None = None) -> JSONResponse:
    """Reserve a seat and an entry.

    Args:
        body: The name.
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The new player id and the state.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        try:
            player = drop.service.join(body.name)
        except GameError as error:
            return refused(error)
        return JSONResponse({"player_id": player.id, "state": state_of(drop)})


@router.post("/intake")
@router.post("/drops/{drop_id}/intake")
async def intake(body: IntakeBody, request: Request, drop_id: str | None = None) -> JSONResponse:
    """Talk to the coach.

    Args:
        body: The player and the message.
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The coach's reply, the contract when proposed, and whether it is.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    try:
        async with runtime.game_lock:
            player = drop.service.intake_player(body.player_id)
        turn = await runtime.coach.respond(player.transcript, body.message)
        async with runtime.game_lock:
            saved = drop.service.record_intake(body.player_id, body.message, turn)
            group = drop.service.group()
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
@router.put("/drops/{drop_id}/contract")
async def edit_contract(
    body: ContractBody, request: Request, drop_id: str | None = None
) -> JSONResponse:
    """Edit the player's proposed contract.

    Args:
        body: The edits.
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The edited contract.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    changes = body.model_dump(exclude_none=True, exclude={"player_id"})
    async with runtime.game_lock:
        try:
            contract = drop.service.edit_contract(body.player_id, changes)
        except GameError as error:
            return refused(error)
        return JSONResponse({"contract": contract_view(contract, drop.service.group())})


@router.post("/contract/lock")
@router.post("/drops/{drop_id}/contract/lock")
async def lock_contract(
    body: PlayerBody, request: Request, drop_id: str | None = None
) -> JSONResponse:
    """Lock the player's contract.

    Args:
        body: The player.
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The locked contract.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        try:
            contract = drop.service.lock_contract(body.player_id)
        except GameError as error:
            return refused(error)
        return JSONResponse({"contract": contract_view(contract, drop.service.group())})


@router.post("/accept")
@router.post("/drops/{drop_id}/accept")
async def accept(body: PlayerBody, request: Request, drop_id: str | None = None) -> JSONResponse:
    """Accept the group agreement.

    Args:
        body: The player.
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The state.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        try:
            drop.service.accept(body.player_id)
        except GameError as error:
            return refused(error)
        return JSONResponse({"state": state_of(drop)})


@router.post("/decline")
@router.post("/drops/{drop_id}/decline")
async def decline(body: PlayerBody, request: Request, drop_id: str | None = None) -> JSONResponse:
    """Decline the group agreement, cancelling the group.

    Args:
        body: The player.
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The state.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        try:
            drop.service.decline(body.player_id)
        except GameError as error:
            return refused(error)
        return JSONResponse({"state": state_of(drop)})


@router.post("/reset")
@router.post("/drops/{drop_id}/reset")
async def reset(
    request: Request, body: ResetBody | None = None, drop_id: str | None = None
) -> JSONResponse:
    """Republish the drop with a new date range, refunding an unfinished group.

    Args:
        request: The request.
        body: The lead until the new start, optional.
        drop_id: The drop; the featured drop when None.

    Returns:
        The state.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        drop.service.reset(None if body is None else body.lead_s)
        return JSONResponse({"state": state_of(drop)})


@router.get("/ledger")
async def ledger(request: Request, after_seq: int = 0, drop_id: str | None = None) -> JSONResponse:
    """Return the ledger after a position, and whether the chain is intact.

    Args:
        request: The request.
        after_seq: Only events after this position.
        drop_id: Only this drop's groups' events, and the purchases, when given.

    Returns:
        The events.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        names = {p.id: p.name for d in runtime.drops.values() for p in d.service.players()}
        events = drop.service.ledger.events(after_seq=after_seq)
        if drop_id is not None:
            mine = set(drop.service.group_ids())
            every = _all_groups(drop.service)
            events = [e for e in events if e.objective_id in mine or e.objective_id not in every]
        return JSONResponse(
            {
                "chain_intact": drop.service.ledger.verify_chain().ok,
                "events": [ledger_view(e, names) for e in events],
            }
        )


@router.get("/events")
@router.get("/drops/{drop_id}/events")
async def score_events(
    request: Request, drop_id: str | None = None, player_id: str | None = None
) -> JSONResponse:
    """Return one line per check-in as it stands now, with its history; optionally one player's.

    Args:
        request: The request.
        drop_id: The drop; the featured drop when None.
        player_id: Only this player's, when given.

    Returns:
        The events.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        lines = drop.service.checkins()
        if player_id is not None:
            lines = [(c, h) for c, h in lines if c.participant_id == player_id]
        return JSONResponse(
            {
                "events": [
                    score_view(current) | {"history": [score_view(e) for e in history]}
                    for current, history in lines
                ]
            }
        )


@router.post("/checkin")
@router.post("/drops/{drop_id}/checkin")
async def checkin(
    request: Request,
    *,
    drop_id: str | None = None,
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
        drop_id: The drop; the featured drop when None.
        player_id: The player.
        milestone: The milestone index, from 0.
        value: The value the player claims.
        note: The player's note.
        file: The photo or clip, if any.

    Returns:
        The score event and the state.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
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
            contract = drop.service.checkin_contract(player_id, milestone)
            if evidence is not None:
                evidence.kind()
        advisory = None
        if evidence is not None:
            advisory = await runtime.verifier.read(evidence, contract, milestone, claimed)
        async with runtime.game_lock:
            event = drop.service.checkin(
                player_id,
                milestone=milestone,
                value=claimed,
                evidence=evidence,
                note=note,
                advisory=advisory,
            )
            return JSONResponse({"event": score_view(event), "state": state_of(drop)})
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
@router.post("/drops/{drop_id}/dispute")
async def dispute(body: DisputeBody, request: Request, drop_id: str | None = None) -> JSONResponse:
    """Dispute a verified check-in inside the window.

    Args:
        body: The player, the check-in and why.
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The dispute event.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        try:
            event = drop.service.dispute(body.player_id, body.event_id, body.reason)
        except GameError as error:
            return refused(error)
        return JSONResponse({"event": score_view(event)})


@router.post("/dispute/review")
@router.post("/drops/{drop_id}/dispute/review")
async def review(body: ReviewBody, request: Request, drop_id: str | None = None) -> JSONResponse:
    """Resolve a disputed check-in (the reviewer's power).

    Args:
        body: The check-in and the verdict.
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The review event.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        try:
            event = drop.service.review(body.event_id, reinstate=body.reinstate)
        except GameError as error:
            return refused(error)
        return JSONResponse({"event": score_view(event)})


@router.post("/finalize")
@router.post("/drops/{drop_id}/finalize")
async def finalize(request: Request, drop_id: str | None = None) -> JSONResponse:
    """Name the winner, then have the agent buy the prize through Reap behind the gate.

    Args:
        request: The request.
        drop_id: The drop; the featured drop when None.

    Returns:
        The result with the purchase, and the state.
    """
    runtime = runtime_of(request)
    drop = drop_of(runtime, drop_id)
    async with runtime.game_lock:
        try:
            group = drop.service.finalize()
        except GameError as error:
            return refused(error)
        result = group.result
        if group.status != GroupStatus.FINALIZED or result is None:
            return JSONResponse({"result": None, "state": state_of(drop)})
        if result.purchase is not None and result.purchase.status == "BUYING":
            return refused(GameError("PURCHASE_IN_PROGRESS", "The agent is already buying."))
        if result.purchase is not None and result.purchase.status == "AWAITING_APPROVAL":
            return refused(
                GameError(
                    "PURCHASE_IN_PROGRESS", "The purchase waits on the card holder's approval."
                )
            )
        winner = next(s for s in result.standings if s.player_id == result.winner_id)
        ceiling = drop.service.pool().ceiling
        drop.service.purchase_progress(
            PrizePurchase(status="BUYING", step="search", backend=drop.buyer.backend)
        )
    bought = await drop.buyer.buy(
        ceiling=ceiling, winner=winner.name, on_step=drop.service.purchase_progress
    )
    async with runtime.game_lock:
        group = drop.service.record_purchase(bought)
        players = drop.service.players()
        body = None if group.result is None else result_view(group.result, players)
        return JSONResponse({"result": body, "state": state_of(drop)})

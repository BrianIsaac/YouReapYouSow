"""What the room screen polls: the group, the clock, the prize, the pool, the players.

Every amount is a string with two decimals and every time an ISO 8601 string, so the
screen never does arithmetic on money.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import JsonValue

from youreapyousow.game.models import (
    GoalContract,
    Group,
    Player,
    PrizePurchase,
    Result,
    ScoreEvent,
    Standing,
)
from youreapyousow.game.rubric import Rubric, standings
from youreapyousow.game.service import STAND_IN, GameService, compute_pool, money
from youreapyousow.ledger.events import EventType, LedgerEvent

LEDGER_TAIL = 8


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _amount(value: Decimal | None) -> str | None:
    return None if value is None else money(value)


def _number(value: Decimal) -> float | int:
    return int(value) if value == value.to_integral_value() else float(value)


def demo_label(seconds_per_day: float) -> str:
    """Say what demo time means, for the screen.

    Args:
        seconds_per_day: Real seconds per challenge day.

    Returns:
        Such as ``Demo time: 1 minute = 1 week``.
    """
    week = seconds_per_day * 7
    if abs(week - 60) < 0.5:
        return "Demo time: 1 minute = 1 week"
    return f"Demo time: {week:g} seconds = 1 week"


def contract_view(contract: GoalContract, group: Group) -> dict[str, JsonValue]:
    """Render a contract with each milestone's window in real time.

    Args:
        contract: The contract.
        group: Its group, for the start time and the clock.

    Returns:
        The contract as the screen reads it.
    """
    per_day = group.terms.seconds_per_day
    milestones: list[JsonValue] = []
    previous_day = 0
    for m in contract.milestones:
        opens = due = None
        if group.started_at is not None:
            opens = group.started_at + timedelta(seconds=previous_day * per_day)
            due = group.started_at + timedelta(seconds=m.day * per_day)
        milestones.append(
            {
                "index": m.index,
                "day": m.day,
                "target": _number(m.target),
                "max_points": m.max_points,
                "opens_at": _iso(opens),
                "due_at": _iso(due),
            }
        )
        previous_day = m.day
    return {
        "participant_id": contract.participant_id,
        "goal_type": contract.goal_type.value,
        "goal_statement": contract.goal_statement,
        "baseline": {
            "value": _number(contract.baseline.value),
            "unit": contract.baseline.unit,
            "verified": contract.baseline.verified,
        },
        "target": {"value": _number(contract.target.value), "unit": contract.target.unit},
        "duration_days": contract.duration_days,
        "milestones": milestones,
        "evidence_policy": contract.evidence_policy.value,
        "total_max_points": contract.total_max_points,
        "rubric_version": contract.rubric_version,
        "comparability": contract.comparability,
        "model": contract.model,
        "prompt_version": contract.prompt_version,
        "status": contract.status.value,
    }


def standing_view(standing: Standing) -> dict[str, JsonValue]:
    """Render one leaderboard line.

    Args:
        standing: The line.

    Returns:
        The line as the screen reads it.
    """
    return {
        "player_id": standing.player_id,
        "name": standing.name,
        "score": standing.score,
        "verified_milestones": standing.verified_milestones,
        "last_verified_at": _iso(standing.last_verified_at),
        "rank": standing.rank,
    }


def score_view(event: ScoreEvent) -> dict[str, JsonValue]:
    """Render a score event.

    Args:
        event: The event.

    Returns:
        The event as the screen reads it.
    """
    advisory: JsonValue = None
    if event.advisory is not None:
        a = event.advisory
        advisory = {
            "model": a.model,
            "shows": a.shows,
            "count": None if a.count is None else _number(a.count),
            "note": a.note,
            "agrees": a.agrees,
        }
    return {
        "event_id": event.event_id,
        "participant_id": event.participant_id,
        "group_id": event.group_id,
        "milestone": event.milestone,
        "evidence_id": event.evidence_id,
        "evidence_kind": event.evidence_kind.value,
        "claimed_value": _number(event.claimed_value),
        "rubric_version": event.rubric_version,
        "at": event.at.isoformat(),
        "delta": event.delta,
        "state": event.state.value,
        "reason": event.reason,
        "supersedes": event.supersedes,
        "advisory": advisory,
    }


def purchase_view(purchase: PrizePurchase) -> dict[str, JsonValue]:
    """Render the prize purchase.

    Args:
        purchase: The purchase.

    Returns:
        The purchase as the screen reads it, with the stand-in line.
    """
    return {
        "status": purchase.status,
        "step": purchase.step,
        "backend": purchase.backend,
        "quote_final_amount": _amount(purchase.quote_final_amount),
        "ceiling": _amount(purchase.ceiling),
        "gate": dict(purchase.gate) if purchase.gate is not None else None,
        "order_id": purchase.order_id,
        "checkout_id": purchase.checkout_id,
        "final_amount": _amount(purchase.final_amount),
        "intent_id": purchase.intent_id,
        "error": purchase.error,
        "note": purchase.note,
        "approval_url": purchase.approval_url,
        "approval_expires_at": _iso(purchase.approval_expires_at),
        "approved_at": _iso(purchase.approved_at),
        "stand_in": STAND_IN,
    }


def result_view(result: Result, players: list[Player]) -> dict[str, JsonValue]:
    """Render the result.

    Args:
        result: The result.
        players: The players, for the winner's name.

    Returns:
        The result as the screen reads it.
    """
    winner: JsonValue = None
    for s in result.standings:
        if s.player_id == result.winner_id:
            winner = {"player_id": s.player_id, "name": s.name, "score": s.score}
    del players
    return {
        "winner": winner,
        "standings": [standing_view(s) for s in result.standings],
        "tie_break_applied": result.tie_break_applied,
        "purchase": None if result.purchase is None else purchase_view(result.purchase),
    }


def summarise(event: LedgerEvent, names: dict[str, str]) -> str:  # noqa: PLR0912
    """Say a ledger event in one sentence.

    Args:
        event: The event.
        names: Player names by id.

    Returns:
        The sentence.
    """
    p = event.payload
    who = (
        names.get(event.subject_id)
        or names.get(str(p.get("participant_id", "")))
        or str(p.get("name", ""))
    )
    match event.type:
        case EventType.GROUP_OPENED:
            return f"Group opened: {p.get('title')}, entry {p.get('entry_amount')} test USDC."
        case EventType.ENTRY_RESERVED:
            return f"{who} joined: entry {p.get('amount')} reserved against the vault."
        case EventType.ENTRY_REFUNDED:
            return f"{who}: entry {p.get('amount')} refunded."
        case EventType.CONTRACT_PROPOSED:
            return f"{who}: contract proposed by {p.get('model')}."
        case EventType.CONTRACT_LOCKED:
            return f"{who} locked their contract."
        case EventType.CONTRACT_ACCEPTED:
            return f"{who} accepted the group agreement."
        case EventType.RUBRIC_LOCKED:
            return f"Rubric {p.get('version')} locked."
        case EventType.GROUP_STARTED:
            return "The challenge started."
        case EventType.SCORE_RECORDED:
            return (
                f"{who}: milestone {int(str(p.get('milestone', 0))) + 1} "
                f"{str(p.get('state', '')).lower()}, {int(str(p.get('delta', 0))):+d}."
            )
        case EventType.SCORE_DISPUTED:
            return f"{who}: a check-in was disputed; its points are withheld."
        case EventType.SCORE_REVIEWED:
            return f"{who}: a disputed check-in was reviewed ({p.get('outcome')})."
        case EventType.STANDINGS_FROZEN:
            return "Standings frozen."
        case EventType.DISPUTE_WINDOW_OPENED:
            return "Dispute window opened."
        case EventType.GROUP_FINALIZED:
            return f"Winner: {p.get('winner_name')}."
        case EventType.QUOTE_LANDED:
            return f"Reap quoted {p.get('merchant')}: {_landed(p)} landed in Singapore."
        case EventType.POLICY_DECIDED:
            phase = "on the proposal" if p.get("phase") == "proposal" else "at the claim"
            return f"Authority gate {phase}: {p.get('disposition')} ({p.get('rule')})."
        case EventType.OBJECTIVE_CREATED:
            return "The agent's purchase objective: budget the pool's ceiling."
        case EventType.GRANT_ISSUED:
            return f"Authority granted: up to {p.get('per_transaction_cap_usd')} in one purchase."
        case EventType.REAP_ENROLLED:
            return f"Payment enrolment bound ({p.get('status')})."
        case EventType.NEED_RAISED:
            return "The prize raised as the agent's need."
        case EventType.CATALOGUE_SEARCHED:
            products = p.get("products")
            count = len(products) if isinstance(products, list) else 0
            return f"Reap catalogue searched: {count} products."
        case EventType.CATALOGUE_DETAILED:
            return "Product details read."
        case EventType.CATALOGUE_VARIANT_RESOLVED:
            return "Variant resolved."
        case EventType.PURCHASE_PROPOSED:
            return f"The agent proposed buying for {p.get('amount')}."
        case EventType.PURCHASE_CLAIMED:
            return "Purchase claimed once, under an idempotency key."
        case EventType.CHECKOUT_CREATED:
            return "Checkout created at Reap."
        case EventType.CHECKOUT_AWAITING_APPROVAL:
            return (
                f"Checkout opened on {_reap_where(p)}; waiting for the card holder's "
                f"approval. Checkout {event.subject_id}."
            )
        case EventType.CHECKOUT_EXPIRED:
            return f"Checkout {event.subject_id} expired before the card holder approved it."
        case EventType.CHECKOUT_FAILED:
            return f"Checkout {event.subject_id} failed at Reap."
        case EventType.GROUP_FULFILLED:
            return (
                f"Group fulfilled; {p.get('surplus_refunded_pro_rata')} surplus refunded pro rata."
            )
        case EventType.GROUP_INTAKE:
            return "Every seat taken: the coach is open."
        case EventType.GROUP_READY:
            return "Every contract locked: the agreement is open."
        case EventType.GROUP_REFUNDING:
            return "Refunding every entry."
        case EventType.CHECKOUT_COMPLETED:
            return f"Checkout completed: order {p.get('order_id')}."
        case EventType.PRIZE_PURCHASED:
            approved = p.get("approved_at")
            where = "Reap's sandbox" if p.get("backend") == "sandbox" else "the local mock of Reap"
            if isinstance(approved, str):
                return (
                    f"Bought for {p.get('winner_name')}: order {p.get('order_id')}, "
                    f"{p.get('final_amount')} USD, approved by the card holder at "
                    f"{datetime.fromisoformat(approved):%H:%M} UTC. A test charge on "
                    f"{where}; the pool is test USDC."
                )
            return f"Prize purchased for {p.get('winner_name')}: order {p.get('order_id')}."
        case EventType.PRIZE_PURCHASE_FAILED:
            return f"Prize purchase failed: {p.get('error')}."
        case EventType.GROUP_CANCELLED:
            return "Group cancelled; every entry refunded."
        case _:
            return event.type.value.replace("_", " ").replace(".", ": ")


def _reap_where(payload: dict[str, JsonValue]) -> str:
    host = str(payload.get("approval_host") or "")
    return "Reap's sandbox" if "sandbox" in host else "the local mock of Reap"


def _landed(payload: dict[str, JsonValue]) -> str:
    breakdown = payload.get("breakdown")
    if isinstance(breakdown, dict):
        final = breakdown.get("final_amount")
        if isinstance(final, dict):
            return f"{final.get('amount')} {final.get('currency', '')}".strip()
    return "a landed price"


def ledger_view(event: LedgerEvent, names: dict[str, str]) -> dict[str, JsonValue]:
    """Render a ledger event for the tail.

    Args:
        event: The event.
        names: Player names by id.

    Returns:
        The event as the screen reads it.
    """
    return {
        "seq": event.seq,
        "event_id": event.event_id,
        "type": event.type.value,
        "at": event.at.isoformat(),
        "subject_id": event.subject_id,
        "summary": summarise(event, names),
        "hash": event.hash[:16],
    }


def rubric_view(rubric: Rubric, group: Group) -> dict[str, JsonValue]:
    """Render the rubric.

    Args:
        rubric: The rubric.
        group: The group, for when it was locked.

    Returns:
        The rubric as the screen reads it.
    """
    return {
        "version": rubric.version,
        "total_points": rubric.total_points,
        "milestone_points": list(rubric.milestone_points),
        "rules": list(rubric.rules),
        "tie_break": rubric.tie_break,
        "locked_at": _iso(group.rubric_locked_at),
    }


def _capacity(service: GameService) -> dict[str, JsonValue]:
    terms = service.terms
    full = compute_pool(
        terms.entry_amount, terms.max_players, terms.buffer_rate, service.prize_quote
    )
    return {
        "entries": full.entries,
        "gross": money(full.gross),
        "buffer": money(full.buffer),
        "ceiling": money(full.ceiling),
        "surplus": _amount(full.surplus),
    }


def state_view(
    service: GameService, prize: dict[str, JsonValue], ledger_events: list[LedgerEvent]
) -> dict[str, JsonValue]:
    """Build the whole polled state.

    Args:
        service: The game.
        prize: The prize block (name, merchant, price, quote).
        ledger_events: The ledger tail, newest last.

    Returns:
        The state, as the contract describes it.
    """
    group = service.group()
    players = service.players()
    events = service.score_events()
    board = {s.player_id: s for s in standings(players, events)}
    names = {p.id: p.name for p in players}
    now = service.clock()
    per_day = group.terms.seconds_per_day
    day = None
    if group.started_at is not None:
        day = round(max(0.0, (now - group.started_at).total_seconds()) / per_day, 2)
    pool = service.pool()
    chain = service.ledger.verify_chain()
    player_views: list[JsonValue] = []
    for p in players:
        s = board.get(p.id)
        player_views.append(
            {
                "player_id": p.id,
                "name": p.name,
                "seat": p.seat,
                "entry": p.entry.value,
                "intake": p.intake.value,
                "accepted": p.accepted_at is not None,
                "contract": None if p.contract is None else contract_view(p.contract, group),
                "score": s.score if s else 0,
                "verified_milestones": s.verified_milestones if s else 0,
                "last_verified_at": _iso(s.last_verified_at) if s else None,
                "rank": s.rank if s else None,
            }
        )
    return {
        "group": {
            "id": group.id,
            "title": group.terms.title,
            "status": group.status.value,
            "min_players": group.terms.min_players,
            "max_players": group.terms.max_players,
            "entry_amount": money(group.terms.entry_amount),
            "currency": group.terms.currency,
            "enrolment_deadline": group.enrolment_deadline.isoformat(),
            "duration_days": group.terms.duration_days,
            "starts_at": _iso(group.starts_at),
            "started_at": _iso(group.started_at),
            "ends_at": _iso(group.ends_at),
            "dispute_window_ends_at": _iso(group.dispute_window_ends_at),
            "rubric_version": group.rubric_version,
            "rubric_locked": group.rubric_locked_at is not None,
            "cancelled_reason": group.cancelled_reason,
        },
        "clock": {
            "now": now.isoformat(),
            "seconds_per_day": per_day,
            "label": demo_label(per_day),
            "day": day,
        },
        "prize": prize,
        "pool": {
            "entries": pool.entries,
            "gross": money(pool.gross),
            "prize_quote": _amount(pool.prize_quote),
            "buffer": money(pool.buffer),
            "ceiling": money(pool.ceiling),
            "surplus": _amount(pool.surplus),
            "stand_in": STAND_IN,
            "disclosure": (
                f"Entry {money(group.terms.entry_amount)} test USDC. Pool = "
                f"{group.terms.max_players} x {money(group.terms.entry_amount)}. The agent may "
                f"spend up to the pool less a {int(group.terms.buffer_rate * 100)}% buffer on "
                "the prize, its shipping and tax. Refunded in full if the group does not "
                "start. Surplus is refunded pro rata."
            ),
            "at_capacity": _capacity(service),
            "vault_balance": money(service.vault_balance),
            "vault_source": service.vault_source,
        },
        "players": player_views,
        "rubric": rubric_view(service.rubric, group),
        "leaderboard": [standing_view(s) for s in board.values()],
        "result": None if group.result is None else result_view(group.result, players),
        "ledger_tail": [ledger_view(e, names) for e in ledger_events[-LEDGER_TAIL:]],
        "ledger_intact": chain.ok,
    }

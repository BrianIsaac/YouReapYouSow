"""What ``/status`` says about Reap: the backend, the enrolment, the catalogue, the checkout.

On the agentic path (``api/app.py``) it names the operator's enrolment, where the
catalogue answers from, how a checkout the gate allows completes (``simulated-complete``
with the sandbox's ``X-Simulate-Checkout: COMPLETED``, ``hosted-approval`` without it,
the vocabulary of the dashboard and the report) and the ``purchase:`` file in force. On
the mock it also lists what the mock assumes where Reap's docs are silent. On the
dormant card path it names the authorisation mode.

The enrolment shown is the one purchases are charged to: on the agentic sandbox
``REAP_ENROLLMENT_ID``, read from Reap at each request, so a revoked or unfinished one
shows at once; on Kwal the participant's set-up card, read from Kwal at each request,
with the setup, what the card can spend from the vault and the session's expiry (never
an address); otherwise the latest ``reap.enrolled`` on the ledger, as it was last read.
Nothing here raises: a purchase file that does not load, or a Reap that does not answer,
is shown as such.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import JsonValue

from youreapyousow.config import Settings
from youreapyousow.control import PurchasePath
from youreapyousow.kwal.client import KwalClient
from youreapyousow.kwal.models import KwalFunding, KwalSetup
from youreapyousow.ledger.events import EventType
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.purchase import (
    DEFAULT_PURCHASE_CONFIG,
    PURCHASE_CONFIG_DIR,
    ScenarioError,
    load_purchase,
)
from youreapyousow.reap.client import ReapClient, ReapError, ReapMock, ReapTransportError
from youreapyousow.reap.models import EnrollmentStatus

ENROLMENT_READ_TIMEOUT_S = 3.0
REPO_ROOT = PURCHASE_CONFIG_DIR.parent

MOCK_ASSUMPTIONS: tuple[str, ...] = (
    "a quote lives 120 s (Reap: 'Quotes are short-lived')",
    "tax is a flat rate per country",
    "finalAmount is the whole breakdown summed (Reap's own examples leave the tax out)",
    "a completed checkout's finalAmount equals its quote's",
    "the completion header returns the checkout COMPLETED on create",
    "order ids read MOCK-ORDER-nnnnnn",
    "the hosted card and approval pages are local",
)
"""The mock's headline assumptions, from ``reap/mock/agentic.py``'s docstring."""

_CATALOGUE_NOTES = {
    "mock": (
        "the mock's bundled catalogues (compute, parts, headphones): fictional merchants, "
        "illustrative prices"
    ),
    "sandbox": "Reap's sandbox catalogue",
    "kwal": "Reap's agentic catalogue through Kwal's participant gateway",
}
_ENROLMENT_NOTES = {
    "mock": "none yet: the mock enrols its own published test card when an objective starts",
    "sandbox": (
        "none yet: create one with `uv run python -m scripts.swap_check --enrol`, complete "
        "Reap's hosted page, then set REAP_ENROLLMENT_ID in .env and restart"
    ),
}
_LIVE_SOURCE = "REAP_ENROLLMENT_ID, read from Reap just now"
_KWAL_SOURCE = "the Kwal participant's set-up card, read from Kwal just now"
_NOTES = {
    "mock": "local mock with Reap's documented shapes",
    "sandbox": "Reap sandbox",
    "kwal": (
        "Payward's Kwal gateway over Reap's agentic sandbox: the participant's own card "
        "and vault pay"
    ),
}


def purchase_config_path(settings: Settings) -> Path:
    """Return the ``purchase:`` file in force.

    Args:
        settings: The settings.

    Returns:
        ``PURCHASE_CONFIG``, or the scenario's default file.
    """
    return settings.purchase_config or DEFAULT_PURCHASE_CONFIG


def shown_path(path: Path) -> str:
    """Show a configuration file as the operator names it.

    Args:
        path: The file.

    Returns:
        The path relative to the repository when it lies inside it, else as given.
    """
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def _purchase(settings: Settings) -> tuple[dict[str, JsonValue], bool | None]:
    """Describe the purchase file in force, and whether it sends the sandbox header.

    Args:
        settings: The settings.

    Returns:
        The description, and the header setting (None when the file does not load).
    """
    path = purchase_config_path(settings)
    try:
        purchase = load_purchase(path)
    except ScenarioError as error:
        return {"config": shown_path(path), "error": str(error)}, None
    except (OSError, yaml.YAMLError) as error:
        return {"config": shown_path(path), "error": f"{path}: {error}"}, None
    if purchase.search is not None:
        what = purchase.search.query
    elif purchase.external_checkout is not None:
        what = purchase.external_checkout.checkout_url
    else:
        what = None
    described: dict[str, JsonValue] = {
        "config": shown_path(path),
        "shape": purchase.shape.value,
        "route": purchase.route.value,
        "what": what,
        "merchants": [*purchase.merchants],
    }
    return described, purchase.checkout.simulate_completed_when_allowed


async def _configured_enrolment(
    reap: ReapClient, enrollment_id: str, source: str = _LIVE_SOURCE
) -> dict[str, JsonValue]:
    """Read the enrolment purchases are charged to, live.

    Args:
        reap: The Reap backend.
        enrollment_id: ``REAP_ENROLLMENT_ID``, or the Kwal participant card's.
        source: Where it was read, as shown.

    Returns:
        Its status and card, or why it could not be read.
    """
    failed: dict[str, JsonValue] = {
        "id": enrollment_id,
        "status": None,
        "active": False,
        "source": source,
    }
    try:
        async with asyncio.timeout(ENROLMENT_READ_TIMEOUT_S):
            enrollment = await reap.get_enrollment(enrollment_id)
    except ReapError as error:
        return {**failed, "error": error.code}
    except (ReapTransportError, TimeoutError) as error:
        return {**failed, "error": f"Reap not reached: {error or 'timed out'}"}
    card = enrollment.payment_method
    return {
        "id": enrollment.id,
        "status": enrollment.status.value,
        "active": enrollment.status == EnrollmentStatus.ACTIVE,
        "network": card.network if card else None,
        "last4": card.last4 if card else None,
        "source": source,
    }


def _ledger_enrolment(ledger: Ledger, backend: str) -> dict[str, JsonValue]:
    """Describe the latest enrolment the ledger recorded, as it was last read.

    Args:
        ledger: The ledger.
        backend: ``mock`` or ``sandbox``, for the note when there is none.

    Returns:
        The enrolment, or a note on how one comes about.
    """
    events = ledger.events(types=[EventType.REAP_ENROLLED])
    if not events:
        return {
            "id": None,
            "status": None,
            "active": False,
            "source": None,
            "note": _ENROLMENT_NOTES[backend],
        }
    latest = events[-1]
    status = latest.payload.get("status")
    return {
        "id": latest.subject_id,
        "status": status,
        "active": status == EnrollmentStatus.ACTIVE.value,
        "network": latest.payload.get("network"),
        "last4": latest.payload.get("last4"),
        "source": f"objective {latest.objective_id}, as last read at {latest.at.isoformat()}",
    }


async def _kwal_part[T](read: Awaitable[T], show: Callable[[T], dict[str, JsonValue]]) -> JsonValue:
    try:
        async with asyncio.timeout(ENROLMENT_READ_TIMEOUT_S):
            return show(await read)
    except ReapError as error:
        return {"error": error.code}
    except (ReapTransportError, TimeoutError) as error:
        return {"error": f"Kwal not reached: {error or 'timed out'}"}


def _setup(setup: KwalSetup) -> dict[str, JsonValue]:
    return {
        "state": setup.state.name,
        "step": setup.step,
        "card": setup.card_status,
        "deposit_observed": setup.deposit_observed,
    }


def _funding(funding: KwalFunding) -> dict[str, JsonValue]:
    available = funding.available
    return {
        "state": funding.state.name,
        "card_spendable_usdc": f"{available.value:.{available.decimals}f}"
        if available is not None
        else None,
    }


async def _kwal_status(kwal: KwalClient) -> dict[str, JsonValue]:
    """Describe the Kwal participant: its session, its setup and what its card can spend.

    Args:
        kwal: The Kwal backend.

    Returns:
        The ``kwal`` object of ``/status``; no address is shown.
    """
    session = kwal.session
    return {
        "session": {
            "file": str(session.path),
            "expires_at": datetime.fromtimestamp(session.expires_at, UTC).isoformat(),
        },
        "setup": await _kwal_part(kwal.setup(), _setup),
        "funding": await _kwal_part(kwal.funding(), _funding),
    }


async def reap_status(
    settings: Settings, reap: ReapClient, ledger: Ledger, path: PurchasePath
) -> dict[str, JsonValue]:
    """Describe the Reap side of the running control plane.

    Args:
        settings: The settings the server started with.
        reap: The Reap backend.
        ledger: The ledger, for the latest enrolment.
        path: The purchase path the control plane runs.

    Returns:
        The ``reap`` object of ``/status``.
    """
    backend = reap.backend
    status: dict[str, JsonValue] = {
        "backend": backend,
        "real_reap": backend != "mock",
        "note": _NOTES[backend],
        "purchase_path": path.value,
    }
    if path == PurchasePath.CARD:
        status["authorization_mode"] = (
            reap.engine.authorization_mode.value
            if isinstance(reap, ReapMock)
            else "set by Reap at project setup"
        )
        return status
    purchase, simulate = _purchase(settings)
    enrollment_id = settings.reap_enrollment_id if backend == "sandbox" else None
    if isinstance(reap, KwalClient):
        status["enrolment"] = await _configured_enrolment(reap, reap.enrolment_id, _KWAL_SOURCE)
    elif enrollment_id is not None:
        status["enrolment"] = await _configured_enrolment(reap, enrollment_id)
    else:
        status["enrolment"] = _ledger_enrolment(ledger, backend)
    status["catalogue"] = {"source": backend, "note": _CATALOGUE_NOTES[backend]}
    if reap.card_spend:
        status["checkout_mode"] = "card-spend"
    else:
        status["checkout_mode"] = (
            None if simulate is None else ("simulated-complete" if simulate else "hosted-approval")
        )
    status["purchase"] = purchase
    if isinstance(reap, KwalClient):
        status["kwal"] = await _kwal_status(reap)
    if backend == "mock":
        status["mock_assumptions"] = [*MOCK_ASSUMPTIONS]
    return status

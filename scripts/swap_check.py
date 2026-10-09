"""Say which steps of the sandbox swap are done, for the ``.env`` in this directory.

    uv run python -m scripts.swap_check                  # where the swap stands
    uv run python -m scripts.swap_check --enrol          # create the operator's enrolment
    uv run python -m scripts.swap_check --external       # card path only, External mode

It reads ``.env`` and the process environment, runs the server's own startup check on
those values, loads the ``purchase:`` file in force, asks the local server's ``/status``
which backend and purchase path it runs, and reads what an earlier run left in ``var/``.
On the agentic path (the default) it then makes two reads at Reap, on the backend the
values name: ``GET /agentic/enrollments/{id}`` for ``REAP_ENROLLMENT_ID`` (sandbox only),
and the purchase file's catalogue search (``POST /agentic/products/search``, read-only).
On the mock both go to an in-process mock; no call leaves the machine. The tunnel,
webhook and External-mode steps apply to the dormant card path only.

On ``REAP_BACKEND=kwal`` (Payward's Kwal gateway, which needs no Reap key) there is no
Reap key and no Reap enrolment (steps 1 and 8 are ``n/a``); three steps take their
place, shown on Kwal only: ``K1`` the participant session the skill saved, ``K2`` its
vault and card set up (``GET /kwal/participant/v1/status``) and ``K3`` the vault funded
(``GET /kwal/participant/v1/funding``). No address or token is printed.

Each step is ``done``, ``TODO`` or ``n/a``, numbered as in ``docs/operations.md``. It
exits 0 when no step is ``TODO``, else 1. It never prints a secret: variables are named,
their values never shown.

``--enrol`` creates the operator's ``EXTERNAL`` enrolment on the sandbox under the key
``enr:operator:<attempt>`` and prints Reap's hosted card page, to be completed by hand,
and the ``REAP_ENROLLMENT_ID`` line to add to ``.env``. A rerun with the same
``--attempt`` replays the same enrolment (Reap keeps a key's first answer for 24 hours);
a new attempt number creates a new one, for a page that expired. It refuses on the mock,
on the card path and when ``REAP_ENROLLMENT_ID`` is already set.
"""

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from youreapyousow.api.status import purchase_config_path, shown_path
from youreapyousow.config import ConfigError, Settings
from youreapyousow.control import OPERATOR_KEY, PurchasePath
from youreapyousow.kwal.client import KwalClient
from youreapyousow.kwal.session import KwalSessionError, load_session
from youreapyousow.ledger.events import EventType
from youreapyousow.purchase import PurchaseConfig, ScenarioError, load_purchase
from youreapyousow.reap.client import (
    ReapClient,
    ReapError,
    ReapMock,
    ReapSandbox,
    ReapTransportError,
)
from youreapyousow.reap.models import (
    ClientReferenceOwner,
    CreateExternalEnrollmentRequest,
    EnrollmentStatus,
    ExternalEnrollmentCreated,
    Presentation,
)

SETTING_KEYS = frozenset(name.upper() for name in Settings.model_fields)
SWAP_KEYS = frozenset(
    {
        "REAP_BACKEND",
        "REAP_API_KEY",
        "REAP_BASE_URL",
        "REAP_PURCHASE_PATH",
        "REAP_ENROLLMENT_ID",
        "PURCHASE_CONFIG",
        "REAP_WEBHOOK_SECRET",
        "REAP_AUTHORIZATION_SECRET",
    }
)
"""The variables the swap sets in ``.env``; a shell export of one would override it."""
READ_KEYS = SETTING_KEYS | {"AGENT_STRATEGY"}
"""Every variable read: the settings, and the loop's own choice of strategy."""
DEFAULT_DATABASE = "var/youreapyousow.db"
_CARD_ONLY = "the agentic path polls Reap: no tunnel, no webhook"
_PURCHASE = "Purchase file in force loads"
_ENROLMENT = "Enrolment in .env and ACTIVE"
_SEARCH = "First catalogue search answers"
_TUNNEL = "Card path only: tunnel to port 8000"
_WEBHOOK = "Card path only: notification webhook secret in .env"
_REQUEST = "Card path, External mode only: REQUEST endpoint secret in .env"
_NOT_CARD = {
    "4": "the card path buys leases",
    "8": "the card path pays by card",
    "9": "the card path searches no catalogue",
}


@dataclass(frozen=True)
class Step:
    """One step of the swap and whether it is done.

    Attributes:
        number: The step's number in ``docs/operations.md``.
        title: What the step is.
        state: ``done``, ``TODO`` or ``n/a``.
        detail: What was found, never a secret's value.
    """

    number: str
    title: str
    state: str
    detail: str


@dataclass(frozen=True)
class Stored:
    """What the server's database already holds from earlier runs.

    Attributes:
        operator: Whether a card-path Reap operator (user and account) is stored.
        backends: The Reap backends its cards were issued and its enrolments read on.
    """

    operator: bool
    backends: frozenset[str]


@dataclass(frozen=True)
class PurchaseRead:
    """The ``purchase:`` file in force, as loaded.

    Attributes:
        config: The file, as shown.
        label: Its shape and route, when it loaded.
        error: Why it did not load, if it did not.
        ships: Whether it names a delivery address (Kwal quotes every item to one).
    """

    config: str
    label: str | None
    error: str | None
    ships: bool = False


@dataclass(frozen=True)
class EnrolmentRead:
    """``REAP_ENROLLMENT_ID`` as Reap answered for it.

    Attributes:
        id: The enrolment.
        status: Its status, or None when it could not be read.
        detail: Its card (network and last four digits), or why it was not read.
    """

    id: str
    status: str | None
    detail: str


@dataclass(frozen=True)
class SearchRead:
    """The purchase file's catalogue search, as Reap answered it.

    Attributes:
        backend: Where it was asked: ``mock`` or ``sandbox``.
        query: The query; None on the checkout-URL route, which searches nothing.
        products: How many products the first page held.
        merchants: How many merchants sold them.
        error: Reap's error code, or why Reap was not reached.
    """

    backend: str
    query: str | None
    products: int = 0
    merchants: int = 0
    error: str | None = None


@dataclass(frozen=True)
class KwalRead:
    """The Kwal participant as its gateway answered, without any address.

    Attributes:
        session: Where the session is and when it expires, or why it is unusable.
        session_ok: Whether the session can be used.
        setup: The setup state's short name, or None when not read.
        setup_detail: The step, or the card and deposit, or why it was not read.
        funding: The funding state's short name, or None when not read.
        spendable: What the card can spend from the vault, in USDC.
        error: Why the gateway did not answer, if it did not.
    """

    session: str
    session_ok: bool
    setup: str | None = None
    setup_detail: str = ""
    funding: str | None = None
    spendable: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Enrolled:
    """An enrolment ``--enrol`` created, with its hosted card page.

    Attributes:
        id: The enrolment id, for ``REAP_ENROLLMENT_ID``.
        status: Its status on creation.
        url: Reap's hosted card page, if a step is outstanding.
        expires_at: When that page expires, if Reap says.
    """

    id: str
    status: str
    url: str | None
    expires_at: str | None


@dataclass(frozen=True)
class Observed:
    """Everything the check looked at.

    Attributes:
        dotenv: Values in ``.env``.
        environ: The process environment's settings variables.
        external: Card path only: True if the project is in External mode, False if
            Managed, None if not yet known.
        cloudflared: Whether ``cloudflared`` is on the path.
        tunnel_running: Whether a ``cloudflared tunnel`` process is running.
        env_ignored: Whether git ignores ``.env``.
        status: The local server's ``/status``, or None if it did not answer.
        stored: The database's Reap state, or None if there is no database.
        purchase: The purchase file in force, or None if not read.
        enrolment: ``REAP_ENROLLMENT_ID`` as Reap answered, or None if not read.
        search: The first catalogue search, or None if not made.
        kwal: The Kwal participant as its gateway answered, on Kwal only.
    """

    dotenv: Mapping[str, str]
    environ: Mapping[str, str]
    external: bool | None
    cloudflared: bool
    tunnel_running: bool
    env_ignored: bool
    status: Mapping[str, Any] | None
    stored: Stored | None = None
    purchase: PurchaseRead | None = None
    enrolment: EnrolmentRead | None = None
    search: SearchRead | None = None
    kwal: KwalRead | None = None


def parse_env(text: str) -> dict[str, str]:
    """Read ``KEY=VALUE`` lines as the settings loader does, ignoring comments.

    Args:
        text: The file's contents.

    Returns:
        The values, with surrounding quotes removed.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


def effective(observed: Observed, key: str) -> str | None:
    """Return the value the server will see: the process environment wins over ``.env``.

    Args:
        observed: What was looked at.
        key: The variable.

    Returns:
        The value, or None if unset or empty.
    """
    value = observed.environ.get(key, observed.dotenv.get(key))
    return value or None


def values_of(dotenv: Mapping[str, str], environ: Mapping[str, str]) -> dict[str, str]:
    """Merge the settings variables as the server reads them, empty values left out.

    Args:
        dotenv: Values in ``.env``.
        environ: The process environment.

    Returns:
        Each settings variable that has a value, the environment's winning.
    """
    merged = {**dotenv, **environ}
    return {key: value for key, value in merged.items() if key in SETTING_KEYS and value}


def startup_refusal(observed: Observed) -> str | None:
    """Run the server's own startup check on the values it will see.

    Args:
        observed: What was looked at.

    Returns:
        The refusal, or None if the server would start.
    """
    try:
        Settings.from_values(values_of(observed.dotenv, observed.environ)).check()
    except ConfigError as error:
        return str(error)
    return None


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'s' * (number != 1)}"


def _set(observed: Observed, key: str) -> str:
    source = "environment" if observed.environ.get(key) else ".env"
    return f"{key} set in {source}" if effective(observed, key) else f"{key} not set"


def _model(observed: Observed) -> tuple[bool, str]:
    done, provider = _provider(observed)
    strategy = effective(observed, "AGENT_STRATEGY") or "deterministic (unset)"
    return done, f"{provider}; AGENT_STRATEGY={strategy}"


def _provider(observed: Observed) -> tuple[bool, str]:
    provider = effective(observed, "MODEL_PROVIDER")
    keyed = effective(observed, "FEATHERLESS_API_KEY") is not None
    if provider == "featherless" or (provider is None and keyed):
        if not keyed:
            return False, "MODEL_PROVIDER=featherless needs FEATHERLESS_API_KEY in .env"
        return True, f"featherless: {_set(observed, 'FEATHERLESS_API_KEY')}"
    if provider == "local":
        return True, "local: start the llama.cpp server first (docs/market-and-ml.md)"
    if provider == "none":
        return True, "none: the deterministic ranking decides"
    if provider is None:
        return True, (
            "automatic: the local server if it answers, else the deterministic ranking "
            "(no FEATHERLESS_API_KEY)"
        )
    return False, f"MODEL_PROVIDER {provider} is not featherless, local or none"


def _fresh(stored: Stored | None, backend: str) -> tuple[bool, str]:
    if stored is None:
        return True, "no database yet"
    other = stored.backends - {backend}
    if other:
        left = ", ".join(sorted(other))
        return False, f"var/ holds Reap state from {left}: rm -rf var/ before restarting"
    held = f"{backend} state only" if stored.backends else "no Reap state"
    return True, f"database holds {held}"


def _restarted(observed: Observed, backend: str, path: str) -> tuple[bool, str]:
    if observed.status is None:
        return False, "server not answering on /status: start it"
    reap = observed.status.get("reap", {})
    running = (reap.get("backend"), reap.get("purchase_path"))
    shown = f"/status shows backend {running[0]}, path {running[1]}"
    if running == (backend, path):
        return True, shown
    return False, f"{shown}; .env names {backend}, {path}: restart"


def _enrolment(observed: Observed, backend: str) -> tuple[bool | None, str]:
    if backend == "kwal":
        return None, "Kwal charges the participant's own set-up card (K2)"
    if backend != "sandbox":
        return None, "the mock enrols its own test card when an objective starts"
    configured = effective(observed, "REAP_ENROLLMENT_ID")
    if configured is None:
        return False, (
            "REAP_ENROLLMENT_ID not set: run swap_check --enrol, finish Reap's hosted page "
            "by hand, add the id to .env, restart"
        )
    read = observed.enrolment
    if read is None:
        return False, f"REAP_ENROLLMENT_ID {configured} not read: the sandbox needs its key"
    if read.status is None:
        return False, f"REAP_ENROLLMENT_ID {read.id} not read: {read.detail}"
    if read.status == EnrollmentStatus.ACTIVE.value:
        return True, (
            f"REAP_ENROLLMENT_ID {read.id} is ACTIVE ({read.detail}); bound to each new objective"
        )
    if read.status == EnrollmentStatus.REQUIRES_ACTION.value:
        return (
            False,
            f"REAP_ENROLLMENT_ID {read.id} is REQUIRES_ACTION: finish Reap's hosted card page",
        )
    return False, (
        f"REAP_ENROLLMENT_ID {read.id} is {read.status}: enrol again with "
        "swap_check --enrol --attempt <next>"
    )


def _search(observed: Observed, backend: str) -> tuple[bool | None, str]:
    purchase = observed.purchase
    if purchase is not None and purchase.error is not None:
        return False, "not searched: the purchase file does not load (step 4)"
    read = observed.search
    if read is None:
        if backend == "sandbox" and effective(observed, "REAP_API_KEY") is None:
            return False, "not searched: the sandbox needs REAP_API_KEY first"
        if backend == "kwal":
            return False, "not searched: Kwal needs a usable session first (K1)"
        return False, "not searched"
    if read.query is None:
        return None, "the checkout_url route quotes its cart; nothing to search"
    if read.error is not None:
        return False, f'"{read.query}" refused: {read.error}'
    if read.products == 0:
        return False, (
            f'"{read.query}": no product on {read.backend}: edit the query '
            "(uv run python -m scripts.catalogue_probe)"
        )
    return True, (
        f'"{read.query}": {_count(read.products, "product")} from '
        f"{_count(read.merchants, 'merchant')} ({read.backend})"
    )


def _purchase(observed: Observed, backend: str) -> tuple[bool, str]:
    purchase = observed.purchase
    if purchase is None:
        return False, "not read"
    if purchase.error is not None:
        return False, purchase.error
    if backend == "kwal" and not purchase.ships:
        return False, (
            f"{purchase.config}: Kwal quotes every item to an address: set shipping_address "
            "in the file (the gateway refuses a quote without one)"
        )
    return True, f"{purchase.config}: {purchase.label}"


def _card_steps(observed: Observed) -> list[tuple[str, str, bool | None, str]]:
    tunnel = "running" if observed.tunnel_running else "not running"
    installed = "on the path" if observed.cloudflared else "NOT installed"
    if observed.external is None:
        request: tuple[bool | None, str] = (
            None,
            "mode not known yet: ask Reap, then rerun with --external if so",
        )
    elif observed.external:
        request = (
            effective(observed, "REAP_AUTHORIZATION_SECRET") is not None,
            _set(observed, "REAP_AUTHORIZATION_SECRET"),
        )
    else:
        request = (None, "Managed mode: no REQUEST endpoint")
    return [
        (
            "C1",
            _TUNNEL,
            observed.cloudflared and observed.tunnel_running,
            f"cloudflared {installed}; tunnel {tunnel}",
        ),
        (
            "C2",
            _WEBHOOK,
            effective(observed, "REAP_WEBHOOK_SECRET") is not None,
            _set(observed, "REAP_WEBHOOK_SECRET"),
        ),
        ("C3", _REQUEST, request[0], request[1]),
    ]


_KWAL_SETUP_NEXT = {
    "NOT_STARTED": "start it with the skill: python3 scripts/register.py setup --owner-address "
    "<the owner wallet>",
    "PENDING": "resume it with the skill: python3 scripts/register.py setup",
    "NEEDS_OPERATOR": "ask Kwal's operator; do not register again",
}


def _kwal_steps(read: KwalRead | None) -> list[tuple[str, str, bool | None, str]]:
    if read is None:
        return [
            ("K1", "Kwal session saved, private and unexpired", False, "not read"),
            ("K2", "Kwal vault and card set up", False, "not read"),
            ("K3", "Kwal vault funded", False, "not read"),
        ]
    if read.setup is None:
        setup: tuple[bool, str] = (False, f"not read: {read.error or 'no session'}")
    elif read.setup == "READY":
        setup = (True, f"READY: {read.setup_detail}")
    elif read.setup_detail == "at deposit_observation":
        setup = (
            False,
            f"{read.setup} {read.setup_detail}: the vault and card exist and wait for the "
            "first deposit: fund the vault (K3), then python3 scripts/register.py setup",
        )
    else:
        setup = (False, f"{read.setup} {read.setup_detail}: {_KWAL_SETUP_NEXT[read.setup]}")
    spend = f"the card can spend {read.spendable} USDC"
    if read.funding is None:
        funded: tuple[bool, str] = (False, f"not read: {read.error or 'no session'}")
    elif read.funding == "READY":
        funded = (True, spend)
    else:
        funded = (
            False,
            f"{read.funding}: {spend}; send test USDC to the vault "
            "(python3 scripts/register.py funding)",
        )
    return [
        ("K1", "Kwal session saved, private and unexpired", read.session_ok, read.session),
        ("K2", "Kwal vault and card set up", setup[0], setup[1]),
        ("K3", "Kwal vault funded", funded[0], funded[1]),
    ]


def _swap_steps(
    observed: Observed, backend: str, path: str
) -> list[tuple[str, str, bool | None, str]]:
    ignored = "gitignored" if observed.env_ignored else "NOT gitignored: do not write a key into it"
    count = len(observed.dotenv)
    found = f"has {count} value{'s' * (count != 1)}" if count else "is missing or empty"
    override = sorted(
        key
        for key, value in observed.environ.items()
        if key in SWAP_KEYS and observed.dotenv.get(key) != value
    )
    switched = f"REAP_BACKEND={backend}"
    if override:
        switched += f"; the shell overrides .env for {', '.join(override)}: unset them"
    refusal = startup_refusal(observed)
    kwal = backend == "kwal"
    key: tuple[bool | None, str] = (
        (None, "Kwal needs no Reap key: the participant's own session pays (K1)")
        if kwal
        else (effective(observed, "REAP_API_KEY") is not None, _set(observed, "REAP_API_KEY"))
    )
    target = "kwal" if kwal else "sandbox"
    steps: list[tuple[str, str, bool | None, str]] = [
        (
            "0",
            ".env exists and git ignores it",
            bool(observed.dotenv) and observed.env_ignored,
            f".env {found}; {ignored}",
        ),
        ("1", "Sandbox key in .env", key[0], key[1]),
        (
            "2",
            f"Backend switched to {target}",
            backend == target and not override,
            switched,
        ),
        ("3", "Fresh var/ for the new backend", *_fresh(observed.stored, backend)),
        ("4", _PURCHASE, *_purchase(observed, backend)),
        ("5", "Agent's model provider", *_model(observed)),
        (
            "6",
            "Server would start on these values",
            refusal is None,
            "startup check passes" if refusal is None else f"it would refuse: {refusal}",
        ),
        ("7", "Restarted: /status shows these values", *_restarted(observed, backend, path)),
        ("8", _ENROLMENT, *_enrolment(observed, backend)),
        ("9", _SEARCH, *_search(observed, backend)),
    ]
    return steps + _kwal_steps(observed.kwal) if kwal else steps


def assess(observed: Observed) -> list[Step]:
    """Judge each swap step from what was observed.

    On the agentic path the card steps (``C1`` to ``C3``) are ``n/a``; on the card path
    the purchase file, the enrolment and the search are.

    Args:
        observed: What was looked at.

    Returns:
        The steps in the order the operator takes them.
    """
    backend = effective(observed, "REAP_BACKEND") or "mock"
    path = effective(observed, "REAP_PURCHASE_PATH") or PurchasePath.AGENTIC.value
    judged = _swap_steps(observed, backend, path)
    if path == PurchasePath.CARD.value:
        judged = [
            (number, title, None, _NOT_CARD[number])
            if number in _NOT_CARD
            else (number, title, done, detail)
            for number, title, done, detail in judged
        ]
        judged += _card_steps(observed)
    else:
        judged += [
            (n, t, None, _CARD_ONLY)
            for n, t in (("C1", _TUNNEL), ("C2", _WEBHOOK), ("C3", _REQUEST))
        ]
    return [
        Step(number, title, "n/a" if done is None else ("done" if done else "TODO"), detail)
        for number, title, done, detail in judged
    ]


def render(steps: list[Step]) -> str:
    """Write the steps as the terminal shows them.

    Args:
        steps: The judged steps.

    Returns:
        One line per step, then a summary.
    """
    lines = [f"[{s.state:>4}] {s.number}  {s.title}: {s.detail}" for s in steps]
    todo = [s.number for s in steps if s.state == "TODO"]
    lines.append("All steps done." if not todo else f"Not done: step {', '.join(todo)}.")
    return "\n".join(lines)


def _tunnel_running() -> bool:
    found = subprocess.run(["pgrep", "-f", "cloudflared tunnel"], capture_output=True, check=False)
    return found.returncode == 0


def _env_ignored(path: Path) -> bool:
    found = subprocess.run(
        ["git", "check-ignore", "-q", str(path)], capture_output=True, check=False
    )
    return found.returncode == 0


def _status(base_url: str) -> dict[str, Any] | None:
    try:
        response = httpx.get(f"{base_url}/status", timeout=5)
        body: dict[str, Any] = response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None
    return body


def read_stored(database: Path) -> Stored | None:
    """Read, without writing, what an earlier run left in the server's database.

    Args:
        database: The SQLite file.

    Returns:
        The stored Reap state, or None if there is no database.
    """
    if not database.exists():
        return None
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        operator = connection.execute(
            "SELECT COUNT(*) FROM records WHERE kind = 'reap_operator'"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT payload FROM events WHERE type IN (?, ?)",
            (EventType.REAP_CARD_ISSUED.value, EventType.REAP_ENROLLED.value),
        ).fetchall()
    except sqlite3.Error:
        return Stored(operator=True, backends=frozenset({"an unreadable database"}))
    finally:
        connection.close()
    backends = frozenset(str(json.loads(payload).get("backend", "")) for (payload,) in rows)
    return Stored(operator=operator > 0, backends=backends)


def reap_for(settings: Settings) -> ReapClient | None:
    """Build the Reap backend the settings name, as the server does.

    Args:
        settings: The settings.

    Returns:
        The sandbox client, the Kwal client, the in-process mock, or None for the sandbox
        without a key or Kwal without a usable session.
    """
    if settings.reap_backend == "sandbox":
        if settings.reap_api_key is None:
            return None
        return ReapSandbox(api_key=settings.reap_api_key, base_url=settings.reap_base_url)
    if settings.reap_backend == "kwal":
        try:
            return KwalClient.from_settings(settings)
        except KwalSessionError:
            return None
    return ReapMock()


def read_purchase(settings: Settings) -> tuple[PurchaseRead, PurchaseConfig | None]:
    """Load the purchase file in force.

    Args:
        settings: The settings naming it.

    Returns:
        What was read, and the block when it loaded.
    """
    path = purchase_config_path(settings)
    shown = shown_path(path)
    try:
        purchase = load_purchase(path)
    except ScenarioError as error:
        return PurchaseRead(shown, None, str(error)), None
    except (OSError, yaml.YAMLError) as error:
        return PurchaseRead(shown, None, f"{path}: {error}"), None
    label = f"shape {purchase.shape.value}, {purchase.route.value} route"
    ships = purchase.need_spec().shipping_address is not None
    return PurchaseRead(shown, label, None, ships=ships), purchase


async def read_enrolment(reap: ReapClient, enrollment_id: str) -> EnrolmentRead:
    """Read an enrolment at Reap: ``GET /agentic/enrollments/{id}``.

    Args:
        reap: The Reap backend.
        enrollment_id: The enrolment.

    Returns:
        Its status and card, or why it was not read.
    """
    try:
        enrollment = await reap.get_enrollment(enrollment_id)
    except ReapError as error:
        return EnrolmentRead(enrollment_id, None, error.code)
    except ReapTransportError as error:
        return EnrolmentRead(enrollment_id, None, f"Reap not reached: {error}")
    card = enrollment.payment_method
    detail = f"{card.network} ending {card.last4}" if card else "no card yet"
    return EnrolmentRead(enrollment.id, enrollment.status.value, detail)


async def read_search(reap: ReapClient, purchase: PurchaseConfig) -> SearchRead:
    """Run the purchase file's catalogue search once: ``POST /agentic/products/search``.

    Args:
        reap: The Reap backend.
        purchase: The purchase block.

    Returns:
        How many products and merchants answered, or why none did.
    """
    if purchase.search is None:
        return SearchRead(reap.backend, None)
    query = purchase.search.query
    try:
        response = await reap.search_products(purchase.search.request())
    except ReapError as error:
        return SearchRead(reap.backend, query, error=error.code)
    except ReapTransportError as error:
        return SearchRead(reap.backend, query, error=f"Reap not reached: {error}")
    merchants = {p.merchant.name for p in response.products}
    return SearchRead(reap.backend, query, len(response.products), len(merchants))


async def read_kwal(settings: Settings) -> KwalRead:
    """Read the Kwal participant: its session, its setup and what its card can spend.

    Args:
        settings: The settings naming the session.

    Returns:
        What was read; never an address or the token.
    """
    path = settings.kwal_credentials_path()
    try:
        session = load_session(path)
    except KwalSessionError as error:
        return KwalRead(session=str(error), session_ok=False)
    expires = datetime.fromtimestamp(session.expires_at, UTC).isoformat()
    if session.expired(datetime.now(UTC)):
        return KwalRead(session=f"credentials at {path} expired {expires}", session_ok=False)
    shown = f"credentials at {path}, expires {expires}"
    kwal = KwalClient.from_settings(settings)
    try:
        setup = await kwal.setup()
        funding = await kwal.funding()
    except ReapError as error:
        return KwalRead(session=shown, session_ok=True, error=error.code)
    except ReapTransportError as error:
        return KwalRead(session=shown, session_ok=True, error=f"Kwal not reached: {error}")
    finally:
        await kwal.aclose()
    if setup.state.name == "READY":
        deposit = "deposit observed" if setup.deposit_observed else "no deposit yet"
        detail = f"card {setup.card_status}, {deposit}"
    else:
        detail = f"at {setup.step}" if setup.step else "at no reported step"
    available = funding.available
    return KwalRead(
        session=shown,
        session_ok=True,
        setup=setup.state.name,
        setup_detail=detail,
        funding=funding.state.name,
        spendable=f"{available.value:.{available.decimals}f}" if available else None,
    )


async def enrol(
    reap: ReapClient, settings: Settings, purchase: PurchaseConfig, *, attempt: int
) -> Enrolled:
    """Create the operator's ``EXTERNAL`` enrolment: ``POST /agentic/enrollments``.

    Args:
        reap: The Reap backend.
        settings: The settings, for the operator's email.
        purchase: The purchase block, for the https return URL.
        attempt: Which attempt; its key is ``enr:operator:<attempt>``.

    Returns:
        The enrolment and its hosted card page.
    """
    created = await reap.create_enrollment(
        CreateExternalEnrollmentRequest(
            owner=ClientReferenceOwner(id=OPERATOR_KEY, email=settings.operator_email),
            presentation=Presentation(return_url=purchase.return_url),
        ),
        idempotency_key=f"enr:{OPERATOR_KEY}:{attempt}",
    )
    action = created.next_action if isinstance(created, ExternalEnrollmentCreated) else None
    return Enrolled(
        id=created.id,
        status=created.status.value,
        url=action.url if action else None,
        expires_at=action.expires_at if action else None,
    )


async def _reads(
    settings: Settings, purchase: PurchaseConfig | None
) -> tuple[EnrolmentRead | None, SearchRead | None]:
    reap = reap_for(settings)
    if reap is None:
        return None, None
    try:
        enrolment = (
            await read_enrolment(reap, settings.reap_enrollment_id)
            if settings.reap_backend == "sandbox" and settings.reap_enrollment_id is not None
            else None
        )
        search = await read_search(reap, purchase) if purchase is not None else None
    finally:
        await reap.aclose()
    return enrolment, search


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in READ_KEYS}


def observe(
    env_file: Path,
    *,
    external: bool | None,
    base_url: str,
    which: Callable[[str], str | None] = shutil.which,
) -> Observed:
    """Look at this machine: ``.env``, the environment, the server, ``var/`` and Reap.

    Args:
        env_file: The ``.env`` the server reads.
        external: Card path only: the project's authorisation mode, if known.
        base_url: Where the local server answers.
        which: Finds a program on the path.

    Returns:
        What was observed.
    """
    dotenv = parse_env(env_file.read_text()) if env_file.exists() else {}
    environ = _environment()
    values = values_of(dotenv, environ)
    database = Path(values.get("DATABASE_PATH", DEFAULT_DATABASE))
    purchase_read: PurchaseRead | None = None
    enrolment: EnrolmentRead | None = None
    search: SearchRead | None = None
    try:
        settings: Settings | None = Settings.from_values(values)
    except ConfigError:
        settings = None
    kwal: KwalRead | None = None
    if settings is not None and settings.reap_purchase_path == PurchasePath.AGENTIC:
        purchase_read, purchase = read_purchase(settings)
        enrolment, search = asyncio.run(_reads(settings, purchase))
        if settings.reap_backend == "kwal":
            kwal = asyncio.run(read_kwal(settings))
    return Observed(
        dotenv=dotenv,
        environ=environ,
        external=external,
        cloudflared=which("cloudflared") is not None,
        tunnel_running=_tunnel_running(),
        env_ignored=_env_ignored(env_file),
        status=_status(base_url),
        stored=read_stored(database),
        purchase=purchase_read,
        enrolment=enrolment,
        search=search,
        kwal=kwal,
    )


def _enrol_main(env_file: Path, attempt: int) -> int:
    """Create the operator's enrolment and say what to do with it.

    Args:
        env_file: The ``.env`` the server reads.
        attempt: Which attempt, for the idempotency key.

    Returns:
        0 when an enrolment was created or replayed, 1 when Reap refused, 2 when the
        values do not call for one.
    """
    dotenv = parse_env(env_file.read_text()) if env_file.exists() else {}
    try:
        settings = Settings.from_values(values_of(dotenv, _environment()))
    except ConfigError as error:
        print(f"Not enrolling: {error}")
        return 2
    if settings.reap_purchase_path == PurchasePath.CARD:
        print("Not enrolling: the card path pays by card (REAP_PURCHASE_PATH=card).")
        return 2
    if settings.reap_backend != "sandbox":
        print(
            "Nothing to enrol: the mock enrols its own published test card when an "
            "objective starts. Set REAP_BACKEND=sandbox first."
        )
        return 2
    if settings.reap_enrollment_id is not None:
        print(
            f"REAP_ENROLLMENT_ID is already set ({settings.reap_enrollment_id}); run "
            "swap_check to read it, or remove it from .env to enrol again."
        )
        return 2
    purchase_read, purchase = read_purchase(settings)
    if purchase is None:
        print(f"Not enrolling: {purchase_read.error}")
        return 2
    reap = reap_for(settings)
    if reap is None:
        print("Not enrolling: --enrol needs REAP_API_KEY in .env first (step 1).")
        return 2

    async def create() -> Enrolled:
        try:
            return await enrol(reap, settings, purchase, attempt=attempt)
        finally:
            await reap.aclose()

    try:
        enrolled = asyncio.run(create())
    except (ReapError, ReapTransportError) as error:
        print(f"Reap did not enrol: {error}")
        return 1
    print(f"Enrolment {enrolled.id} (key enr:{OPERATOR_KEY}:{attempt}): {enrolled.status}.")
    if enrolled.url is not None:
        expiry = f" (expires {enrolled.expires_at})" if enrolled.expires_at else ""
        print("Open Reap's hosted card page by hand and enter one of Reap's published test cards:")
        print(f"  {enrolled.url}{expiry}")
    print("Then add this line to .env by editor, restart the server and rerun swap_check:")
    print(f"  REAP_ENROLLMENT_ID={enrolled.id}")
    print(
        f"A rerun with --attempt {attempt} shows this same page; if it expires, enrol with "
        f"--attempt {attempt + 1}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Print the swap's progress, or create the operator's enrolment.

    Args:
        argv: Arguments, or the process's own.

    Returns:
        The check: 0 when no step is ``TODO``, else 1. ``--enrol``: see ``_enrol_main``.
    """
    parser = argparse.ArgumentParser(description="Which sandbox swap steps are done.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--external", dest="external", action="store_true", default=None)
    mode.add_argument("--managed", dest="external", action="store_false")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--enrol", action="store_true", help="create the operator's enrolment on the sandbox"
    )
    parser.add_argument("--attempt", type=int, default=1, help="the enrolment attempt (key)")
    args = parser.parse_args(argv)
    if args.enrol:
        return _enrol_main(args.env_file, args.attempt)
    steps = assess(observe(args.env_file, external=args.external, base_url=args.base_url))
    print(render(steps))
    return 0 if all(s.state != "TODO" for s in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())

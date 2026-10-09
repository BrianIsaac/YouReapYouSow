"""The control plane's HTTP surface: status, the Reap webhook receiver, external auth.

Run it with ``uv run uvicorn youreapyousow.api.app:serve --factory``. With the default
mock backend, the mock delivers its signed webhooks into this same app in-process, so
the receiver path is exercised exactly as it is when Reap calls it through a tunnel.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import JsonValue, SecretStr, ValidationError

from youreapyousow.api import game as game_routes
from youreapyousow.api.status import reap_status
from youreapyousow.authority.gate import AuthorityGate
from youreapyousow.clock import Clock, utc_now
from youreapyousow.config import ConfigError, Settings
from youreapyousow.control import ControlPlane, Operator, mock_card_entry, need_judge_for
from youreapyousow.game.coach import Coach
from youreapyousow.game.drops import Drop, load_drops
from youreapyousow.game.llm import build_links
from youreapyousow.game.models import GroupTerms
from youreapyousow.game.prize import PrizeBuyer
from youreapyousow.game.service import GameError, GameService
from youreapyousow.game.verifier import Verifier
from youreapyousow.kwal.client import KwalClient
from youreapyousow.kwal.session import KwalSessionError
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.market.provisioning import MockProvisioner
from youreapyousow.market.service import MarketMode, MarketService
from youreapyousow.purchase import PURCHASE_CONFIG_DIR, PurchaseConfig, load_purchase
from youreapyousow.reap.client import ReapClient, ReapMock, ReapSandbox, httpx_delivery
from youreapyousow.reap.mock.engine import AuthorizationMode
from youreapyousow.reap.models import (
    CardAuthorizationRequest,
    CreateWebhookRequest,
    WebhookEvent,
)
from youreapyousow.reap.webhooks import SIGNATURE_HEADER, WebhookSignatureError, verify
from youreapyousow.repos import Repositories
from youreapyousow.store import Database

SELF_BASE_URL = "http://youreapyousow.local"
WEB_DIR = Path(__file__).resolve().parents[3] / "web"
NOTIFY_PATH = "/webhooks/reap"
AUTHORISE_PATH = "/webhooks/reap/authorization"


class IndentedJSONResponse(JSONResponse):
    """JSON indented for reading in a browser tab, as ``/status`` is read."""

    def render(self, content: object) -> bytes:
        """Serialise with two-space indentation.

        Args:
            content: The body.

        Returns:
            The encoded JSON.
        """
        return json.dumps(content, indent=2, ensure_ascii=False).encode()


@dataclass
class Runtime:
    """Everything the app serves, built once at startup.

    Attributes:
        settings: The configuration.
        control: The control plane.
        reap: The Reap backend.
        market: Price discovery.
        http: The shared outbound HTTP client.
        secrets: Webhook signing secret per receiving path.
        clock: Time source.
        coach: The intake coach.
        verifier: The photo reader.
        drops: The drops on offer, the featured one first.
        game_lock: Serialises the challenge's mutations.
        tasks: Background tasks to cancel on shutdown.
    """

    settings: Settings
    control: ControlPlane
    reap: ReapClient
    market: MarketService
    http: httpx.AsyncClient
    secrets: dict[str, SecretStr]
    clock: Clock
    coach: Coach
    verifier: Verifier
    drops: dict[str, Drop] = field(default_factory=dict[str, Drop])
    game_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])

    @property
    def featured(self) -> Drop:
        """Return the featured drop, the first listed: the un-keyed routes act on it.

        Returns:
            The drop.
        """
        return next(iter(self.drops.values()))

    @property
    def game(self) -> GameService:
        """Return the featured drop's group service.

        Returns:
            The service.
        """
        return self.featured.service

    @property
    def purchase(self) -> PurchaseConfig:
        """Return the featured drop's prize file.

        Returns:
            The purchase file.
        """
        return self.featured.purchase

    @property
    def buyer(self) -> PrizeBuyer:
        """Return the featured drop's buyer.

        Returns:
            The buyer.
        """
        return self.featured.buyer

    def prize_info(self) -> dict[str, JsonValue]:
        """Return the featured drop's prize block.

        Returns:
            The block.
        """
        return dict(self.featured.prize)

    async def preview_prizes(self) -> None:
        """Land every drop's prize quote for its disclosure, side by side."""
        await asyncio.gather(*(drop.preview() for drop in self.drops.values()))

    def set_vault(self, balance: Decimal, source: str) -> None:
        """Set the one vault's balance and its source on every drop.

        Args:
            balance: The test USDC entries are reserved against.
            source: Where it comes from, in one line.
        """
        for drop in self.drops.values():
            drop.service.vault_balance = balance
            drop.service.vault_source = source

    async def read_vault(self) -> None:
        """Read the vault's test USDC from Kwal when a session is saved; else keep the setting.

        On the mock the configured balance stands, so no test reaches a real gateway.
        """
        if self.settings.reap_backend == "mock":
            return
        try:
            kwal = KwalClient.from_settings(self.settings)
        except (ConfigError, KwalSessionError, OSError, ValueError):
            return
        try:
            funding = await asyncio.wait_for(kwal.funding(), timeout=20)
        except Exception as error:
            self.set_vault(
                self.game.vault_balance,
                f"The Kwal vault could not be read at start ({type(error).__name__}); entries "
                "are reserved against the configured stand-in balance",
            )
            return
        finally:
            await kwal.aclose()
        available = funding.available.value if funding.available is not None else Decimal(0)
        terms = self.game.terms
        if available >= terms.entry_amount * terms.max_players:
            self.set_vault(available, "Kwal vault on Ink Sepolia (test USDC, read at start)")
        else:
            self.set_vault(
                self.game.vault_balance,
                (
                    f"The Kwal vault reads {available.quantize(Decimal('0.01'))} test USDC "
                    "(not yet funded); entries are reserved against the configured stand-in balance"
                ),
            )

    async def attach(self, app: FastAPI) -> None:
        """Wire the mock's webhook delivery into this app and register its endpoints.

        With the sandbox, endpoints are registered once by the operator (see
        ``docs/operations.md``, the dormant card path) and their secrets come from
        ``.env``.

        Args:
            app: The running app.
        """
        if not isinstance(self.reap, ReapMock):
            return
        inbound = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=SELF_BASE_URL)
        self.reap.engine.delivery = httpx_delivery(inbound)
        notify = await self.reap.create_webhook(
            CreateWebhookRequest(name="control plane", url=f"{SELF_BASE_URL}{NOTIFY_PATH}")
        )
        self.secrets[NOTIFY_PATH] = SecretStr(notify.signing_secret)
        if self.reap.engine.authorization_mode == AuthorizationMode.EXTERNAL:
            request = await self.reap.create_webhook(
                CreateWebhookRequest(
                    name="authority gate", url=f"{SELF_BASE_URL}{AUTHORISE_PATH}", mode="REQUEST"
                )
            )
            self.secrets[AUTHORISE_PATH] = SecretStr(request.signing_secret)

    async def aclose(self) -> None:
        """Stop background work and release connections."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.reap.aclose()
        await self.http.aclose()
        self.control.repos.db.close()


def _reserved_elsewhere(runtime: Runtime, drop_id: str) -> Callable[[], Decimal]:
    def reserved() -> Decimal:
        return sum(
            (d.service.reserved() for key, d in runtime.drops.items() if key != drop_id),
            Decimal(0),
        )

    return reserved


def build_runtime(
    settings: Settings, *, clock: Clock = utc_now, http: httpx.AsyncClient | None = None
) -> Runtime:
    """Assemble the control plane from settings.

    Args:
        settings: The configuration; checked before anything is built.
        clock: Time source.
        http: Outbound client for the market (a mock transport in tests).

    Returns:
        The runtime, not yet attached to an app.
    """
    settings.check()
    db = Database(settings.database_path)
    repos = Repositories(db)
    ledger = Ledger(db, clock)
    reap: ReapClient
    secrets: dict[str, SecretStr] = {}
    if settings.reap_backend == "sandbox" and settings.reap_api_key is not None:
        reap = ReapSandbox(
            api_key=settings.reap_api_key, base_url=settings.reap_base_url, timeout_s=30
        )
        if settings.reap_webhook_secret is not None:
            secrets[NOTIFY_PATH] = settings.reap_webhook_secret
        if settings.reap_authorization_secret is not None:
            secrets[AUTHORISE_PATH] = settings.reap_authorization_secret
    elif settings.reap_backend == "kwal":
        reap = KwalClient.from_settings(settings)
    else:
        reap = ReapMock(clock=clock, authorization_mode=settings.reap_authorization_mode)
    outbound = http or httpx.AsyncClient(follow_redirects=True)
    market = MarketService(
        outbound,
        mode=settings.market_mode,
        timeout_s=settings.market_timeout_s,
        snapshot_path=settings.snapshot_path,
        clock=clock,
    )
    provisioner = MockProvisioner()
    control = ControlPlane(
        repos=repos,
        ledger=ledger,
        gate=AuthorityGate(repos, ledger, clock, need_judge=need_judge_for(repos)),
        reap=reap,
        market=market,
        provisioner=provisioner,
        operator=Operator(email=settings.operator_email, phone=settings.operator_phone),
        clock=clock,
        card_entry=mock_card_entry(reap.agentic) if isinstance(reap, ReapMock) else None,
    )
    coach = Coach(build_links(settings, outbound, "coach"), duration_days=28)
    runtime = Runtime(
        settings,
        control,
        reap,
        market,
        outbound,
        secrets,
        clock,
        coach,
        Verifier(build_links(settings, outbound, "vision")),
    )
    mock_control = control
    if not isinstance(reap, ReapMock):
        mock_reap = ReapMock(clock=clock)
        mock_control = ControlPlane(
            repos=repos,
            ledger=ledger,
            gate=AuthorityGate(repos, ledger, clock, need_judge=need_judge_for(repos)),
            reap=mock_reap,
            market=market,
            provisioner=provisioner,
            operator=Operator(email=settings.operator_email, phone=settings.operator_phone),
            clock=clock,
            card_entry=mock_card_entry(mock_reap.agentic),
        )
    # Only the sandbox knows the operator's enrolment; the mock enrols its own test card.
    enrollment = settings.reap_enrollment_id if settings.reap_backend == "sandbox" else None
    for config in load_drops():
        path = PURCHASE_CONFIG_DIR / config.purchase
        if not runtime.drops and settings.purchase_config is not None:
            path = settings.purchase_config
        purchase = load_purchase(path)
        live = config.live_purchase or isinstance(reap, ReapMock)
        drop_control = control if live else mock_control
        backend = settings.reap_backend if live else "mock"
        service = GameService(
            db=db,
            ledger=ledger,
            clock=clock,
            terms=GroupTerms(
                title=config.title,
                entry_amount=config.entry_amount,
                enrolment_window_s=settings.enrolment_window_s,
                duration_days=config.duration_days,
                seconds_per_day=settings.demo_clock,
                dispute_window_s=settings.dispute_window_s,
            ),
            vault_balance=settings.vault_balance_usdc,
            vault_source="configured stand-in balance (VAULT_BALANCE_USDC)",
            evidence_dir=settings.evidence_dir,
            drop_id=config.id,
            reserved_elsewhere=_reserved_elsewhere(runtime, config.id),
        )
        runtime.drops[config.id] = Drop(
            config=config,
            service=service,
            purchase=purchase,
            reap=drop_control.reap,
            backend=backend,
            buyer=PrizeBuyer(
                drop_control,
                purchase,
                backend=backend,
                enrollment_id=None if enrollment is None or not live else str(enrollment),
                fallback=PrizeBuyer(mock_control, purchase, backend="mock", enrollment_id=None)
                if backend == "sandbox"
                else None,
            ),
        )
        if not service.has_group():
            service.open_group()
    return runtime


async def _invalid(request: Request, error: Exception) -> JSONResponse:
    del request
    message = "invalid request"
    if isinstance(error, RequestValidationError):
        errors: list[dict[str, object]] = list(error.errors())
        if errors:
            loc = errors[0].get("loc")
            parts = (
                [str(p) for p in cast(tuple[object, ...], loc)[1:]]
                if isinstance(loc, tuple)
                else []
            )
            msg = str(errors[0].get("msg", message))
            message = f"{'.'.join(parts)}: {msg}" if parts else msg
    return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": message}}, status_code=422)


async def _refused(request: Request, error: Exception) -> JSONResponse:
    del request
    if not isinstance(error, GameError):
        raise error
    return game_routes.refused(error)


def _mount_game(app: FastAPI) -> None:
    """Add the challenge's routes, its error shape, and the room screen's files.

    Args:
        app: The app.
    """
    app.include_router(game_routes.router)
    app.add_exception_handler(RequestValidationError, _invalid)
    app.add_exception_handler(GameError, _refused)
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def create_app(factory: Callable[[], Awaitable[Runtime]]) -> FastAPI:
    """Build the app; the runtime is created inside the app's lifespan.

    Args:
        factory: Builds the runtime.

    Returns:
        The app.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        runtime = await factory()
        app.state.runtime = runtime
        await runtime.attach(app)
        await runtime.read_vault()
        runtime.tasks.append(asyncio.create_task(runtime.preview_prizes()))
        # Also on the mock market, where it reads the fixtures, so /status names every
        # connector and its source from the first request.
        await runtime.market.refresh()
        if runtime.market.mode == MarketMode.LIVE:
            runtime.tasks.append(
                asyncio.create_task(
                    runtime.market.refresh_forever(runtime.settings.market_refresh_s)
                )
            )
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(title="YouReapYouSow", lifespan=lifespan)

    def runtime_of(request: Request) -> Runtime:
        runtime: Runtime = request.app.state.runtime
        return runtime

    async def health() -> dict[str, bool]:
        return {"ok": True}

    async def status(request: Request) -> JSONResponse:
        runtime = runtime_of(request)
        chain = runtime.control.ledger.verify_chain()
        reap = await reap_status(
            runtime.settings, runtime.reap, runtime.control.ledger, runtime.control.purchase_path
        )
        return IndentedJSONResponse(
            {
                "reap": reap,
                "market": {
                    "mode": runtime.market.mode.value,
                    "connectors": runtime.market.status(),
                },
                "ledger": {"events": chain.events_checked, "chain_intact": chain.ok},
            }
        )

    async def verified_event(request: Request, path: str) -> tuple[Runtime, bytes] | JSONResponse:
        runtime = runtime_of(request)
        secret = runtime.secrets.get(path)
        if secret is None:
            return JSONResponse({"error": "endpoint not registered"}, status_code=404)
        body = await request.body()
        try:
            verify(
                secret.get_secret_value(),
                body,
                request.headers.get(SIGNATURE_HEADER),
                runtime.clock(),
            )
        except WebhookSignatureError as error:
            return JSONResponse({"error": str(error)}, status_code=401)
        return runtime, body

    async def notification(request: Request) -> JSONResponse:
        checked = await verified_event(request, NOTIFY_PATH)
        if isinstance(checked, JSONResponse):
            return checked
        runtime, body = checked
        event = WebhookEvent.model_validate_json(body)
        new = runtime.control.receive_webhook(event)
        return JSONResponse({"received": True, "duplicate": not new})

    async def authorisation(request: Request) -> JSONResponse:
        checked = await verified_event(request, AUTHORISE_PATH)
        if isinstance(checked, JSONResponse):
            return checked
        runtime, body = checked
        try:
            data = CardAuthorizationRequest.model_validate(
                WebhookEvent.model_validate_json(body).data
            )
        except ValidationError:
            return JSONResponse({"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"})
        decision = runtime.control.authorise(data)
        return JSONResponse(decision.to_wire())

    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/status", status, methods=["GET"])
    app.add_api_route(NOTIFY_PATH, notification, methods=["POST"])
    app.add_api_route(AUTHORISE_PATH, authorisation, methods=["POST"])
    _mount_game(app)
    return app


def serve() -> FastAPI:
    """Uvicorn factory: settings from the environment and ``.env``, checked first.

    A configuration that cannot work stops the process with one plain line naming
    what is missing, before uvicorn starts, rather than a traceback from the lifespan.

    Returns:
        The app.

    Raises:
        SystemExit: If the settings cannot work, with the reason.
    """
    try:
        settings = Settings.from_env()
        settings.check()
    except ConfigError as error:
        raise SystemExit(f"Refusing to start: {error}") from None

    async def factory() -> Runtime:
        return build_runtime(settings)

    return create_app(factory)

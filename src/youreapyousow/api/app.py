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

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError

from youreapyousow.api.status import reap_status
from youreapyousow.authority.gate import AuthorityGate
from youreapyousow.clock import Clock, utc_now
from youreapyousow.config import ConfigError, Settings
from youreapyousow.control import ControlPlane, Operator, mock_card_entry, need_judge_for
from youreapyousow.kwal.client import KwalClient
from youreapyousow.ledger.ledger import Ledger
from youreapyousow.market.provisioning import MockProvisioner
from youreapyousow.market.service import MarketMode, MarketService
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
        tasks: Background tasks to cancel on shutdown.
    """

    settings: Settings
    control: ControlPlane
    reap: ReapClient
    market: MarketService
    http: httpx.AsyncClient
    secrets: dict[str, SecretStr]
    clock: Clock
    tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])

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
        reap = ReapSandbox(api_key=settings.reap_api_key, base_url=settings.reap_base_url)
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
    return Runtime(settings, control, reap, market, outbound, secrets, clock)


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

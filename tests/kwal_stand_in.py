"""The Kwal gateway mock behind ``KwalClient``, standing in for Payward's gateway.

``build_runtime`` builds ``KwalClient.from_settings`` when ``REAP_BACKEND=kwal``; here that
client talks HTTP to the gateway mock in process (``kwal/mock.py``), so the runtime takes
its Kwal code paths without leaving the process. The participant's session is saved as the
skill saves it, outside any checkout and owner-only, so the startup check reads it as it
reads a real one.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from youreapyousow.clock import Clock
from youreapyousow.config import Settings
from youreapyousow.kwal.client import KwalClient
from youreapyousow.kwal.mock import (
    MOCK_BASE_URL,
    MOCK_TOKEN,
    KwalMockConfig,
    KwalMockEngine,
    create_kwal_mock_app,
)
from youreapyousow.kwal.session import load_session


def save_session(directory: Path, *, token: str = MOCK_TOKEN) -> Path:
    """Save a participant session as ``register.py`` does.

    Args:
        directory: Where, outside any checkout.
        token: The session token.

    Returns:
        The credentials file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "credentials.json"
    expires = int(datetime(2037, 10, 16, tzinfo=UTC).timestamp())
    body = {"service_url": MOCK_BASE_URL, "token": token, "expires_at": expires}
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    return path


def stand_in_kwal(
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    directory: Path,
    *,
    config: KwalMockConfig | None = None,
) -> tuple[KwalMockEngine, Path]:
    """Point every ``KwalClient.from_settings`` at the gateway mock.

    Args:
        monkeypatch: Replaces ``KwalClient.from_settings``.
        clock: The runtime's clock, shared by the mock.
        directory: Where to save the participant's session.
        config: The mock's modes.

    Returns:
        The gateway mock, and the session file ``PWS_CREDENTIALS_FILE`` names.
    """
    engine = KwalMockEngine(clock=clock, config=config)
    app = create_kwal_mock_app(engine)

    def from_settings(settings: Settings) -> KwalClient:
        return KwalClient(
            load_session(settings.kwal_credentials_path()),
            transport=httpx.ASGITransport(app=app),
            clock=clock,
        )

    monkeypatch.setattr(KwalClient, "from_settings", staticmethod(from_settings))
    return engine, save_session(directory / "pws")

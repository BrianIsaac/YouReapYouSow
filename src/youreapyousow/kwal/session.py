"""The Kwal participant session the skill's ``register.py`` saved, read and never shown.

The skill (``payward/kwal-skill`` at ``be5f52c``) saves one session per participant to
``$PWS_CREDENTIALS_FILE``, else ``$XDG_CONFIG_HOME/pws/agent-payment/credentials.json``,
else ``~/.config/pws/agent-payment/credentials.json`` (``references/setup.md:31-35``), as
``{"service_url", "token", "expires_at"}`` with mode 0600 (``scripts/session.py:332-356``).
The control plane only reads it: registering a participant, setting up its vault and card
and funding the vault are the owner's, with the skill. The same checks the skill makes
before it uses the file are made here: owner-only, outside every git checkout, an https
gateway (plain http for loopback only), an unexpired session. No message ever carries
the token or a value from the file.
"""

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import SecretStr

CREDENTIALS_FILE_ENV = "PWS_CREDENTIALS_FILE"
DEFAULT_SERVICE_URL = "https://api.sandbox.services.payward.com"
"""The UAT gateway the skill uses unless told otherwise (``scripts/transport.py:20``)."""

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_REGISTER = "register a participant with the Kwal skill first: python3 scripts/register.py register"


class KwalSessionError(ValueError):
    """The saved session cannot be used; the message never carries a secret."""


@dataclass(frozen=True)
class KwalSession:
    """A participant session.

    Attributes:
        service_url: The participant gateway's origin.
        token: The bearer token; secret.
        expires_at: Unix seconds after which the gateway refuses it.
        path: Where it was read from.
    """

    service_url: str
    token: SecretStr = field(repr=False)
    expires_at: int
    path: Path

    def expired(self, now: datetime) -> bool:
        """Whether the session has run out; there is no renewal (``SKILL.md:54``).

        Args:
            now: The current time.

        Returns:
            True from its expiry on.
        """
        return now.timestamp() >= self.expires_at


def default_credentials_path(environ: Mapping[str, str] = os.environ) -> Path:
    """Return where the skill saves the session, by its own order of precedence.

    Args:
        environ: The environment to read ``PWS_CREDENTIALS_FILE``, ``XDG_CONFIG_HOME``
            and ``HOME`` from.

    Returns:
        The credentials file's path.
    """
    named = environ.get(CREDENTIALS_FILE_ENV)
    if named:
        return Path(named).expanduser()
    config = environ.get("XDG_CONFIG_HOME")
    base = Path(config) if config else Path(environ.get("HOME", str(Path.home()))) / ".config"
    return base / "pws" / "agent-payment" / "credentials.json"


def normalise_service_url(service_url: str) -> str:
    """Check a gateway origin as the skill does (``scripts/transport.py:130-172``).

    Args:
        service_url: The saved origin.

    Returns:
        The origin, without a trailing slash.

    Raises:
        KwalSessionError: If it is not an https origin, or plain http to loopback.
    """
    parts = urlsplit(service_url.strip().rstrip("/"))
    try:
        host = parts.hostname
        _ = parts.port
    except ValueError as error:
        raise KwalSessionError("the saved service_url is not a usable URL") from error
    if not host:
        raise KwalSessionError("the saved service_url names no host")
    if parts.scheme != "https" and not (parts.scheme == "http" and host in LOOPBACK_HOSTS):
        raise KwalSessionError("the saved service_url must be https (http only for loopback)")
    if parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise KwalSessionError("the saved service_url must be a scheme, host and optional port")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _usable_token(token: str) -> bool:
    # The token becomes a header, where a space or control character would split it.
    return bool(token) and token.isascii() and token.isprintable() and " " not in token


def _inside_checkout(path: Path) -> bool:
    return any((directory / ".git").exists() for directory in path.resolve().parents)


def load_session(path: Path) -> KwalSession:
    """Read the saved participant session.

    Args:
        path: The credentials file.

    Returns:
        The session.

    Raises:
        KwalSessionError: If the file is missing, inside a checkout, readable by others,
            not the skill's shape, or names an unusable gateway.
    """
    if _inside_checkout(path):
        raise KwalSessionError(
            f"the Kwal credentials at {path} must be outside a repository checkout"
        )
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise KwalSessionError(f"the Kwal credentials at {path} are not a regular file")
        if stat.S_IMODE(info.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
            raise KwalSessionError(
                f"the Kwal credentials at {path} are readable by other users: chmod 600 them"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise KwalSessionError(f"no Kwal credentials at {path}: {_REGISTER}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KwalSessionError(f"the Kwal credentials at {path} are not readable") from error
    if not isinstance(raw, dict):
        raise KwalSessionError(f"the Kwal credentials at {path} are not a JSON object")
    fields = cast(dict[str, object], raw)
    token = fields.get("token")
    if not isinstance(token, str) or not _usable_token(token):
        raise KwalSessionError(f"the Kwal credentials at {path} hold no token")
    expires_at = fields.get("expires_at")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise KwalSessionError(f"the Kwal credentials at {path} hold no integer expiry")
    service_url = fields.get("service_url")
    if not isinstance(service_url, str):
        raise KwalSessionError(f"the Kwal credentials at {path} hold no service_url")
    return KwalSession(
        service_url=normalise_service_url(service_url),
        token=SecretStr(token),
        expires_at=expires_at,
        path=path,
    )

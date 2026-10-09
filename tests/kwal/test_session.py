"""The Kwal session the skill saved, read without ever showing its token."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from youreapyousow.kwal.session import (
    DEFAULT_SERVICE_URL,
    KwalSessionError,
    default_credentials_path,
    load_session,
)

TOKEN = "header.body.signature"
NOW = datetime(2037, 10, 9, 8, 42, 3, tzinfo=UTC)
LATER = int(NOW.timestamp()) + 3600


def saved(
    directory: Path,
    *,
    mode: int = 0o600,
    service_url: str = DEFAULT_SERVICE_URL,
    token: object = TOKEN,
    expires_at: object = LATER,
) -> Path:
    """Write a credentials file as ``register.py`` saves one.

    Returns:
        Its path.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "credentials.json"
    body = {"service_url": service_url, "token": token, "expires_at": expires_at}
    path.write_text(json.dumps(body))
    path.chmod(mode)
    return path


def test_a_saved_session_is_read_with_its_gateway_and_expiry(tmp_path: Path) -> None:
    """The file holds the gateway, the token and the expiry, as the skill writes them."""
    session = load_session(saved(tmp_path / "pws"))
    assert session.service_url == DEFAULT_SERVICE_URL
    assert session.token.get_secret_value() == TOKEN
    assert not session.expired(NOW)


def test_the_token_never_shows(tmp_path: Path) -> None:
    """Neither the session's text nor its repr carries the token."""
    session = load_session(saved(tmp_path / "pws"))
    assert TOKEN not in repr(session)
    assert TOKEN not in str(session)


def test_an_expired_session_says_so(tmp_path: Path) -> None:
    """There is no renewal; the expiry is reported, never extended."""
    session = load_session(saved(tmp_path / "pws", expires_at=int(NOW.timestamp())))
    assert session.expired(NOW)


def test_a_file_others_can_read_is_refused(tmp_path: Path) -> None:
    """A token readable by the group or others is refused, as the skill refuses it."""
    with pytest.raises(KwalSessionError, match="readable by other users"):
        load_session(saved(tmp_path / "pws", mode=0o640))


def test_a_file_inside_a_git_checkout_is_refused(tmp_path: Path) -> None:
    """A token inside a checkout could reach a commit."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(KwalSessionError, match="outside a repository checkout"):
        load_session(saved(tmp_path / "repo" / "config"))


def test_a_missing_file_names_the_skill_s_register_step(tmp_path: Path) -> None:
    """No session yet means the participant was never registered here."""
    with pytest.raises(KwalSessionError, match=r"register\.py register"):
        load_session(tmp_path / "pws" / "credentials.json")


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"token": ""}, "no token"),
        ({"token": "two words"}, "no token"),
        ({"expires_at": "soon"}, "no integer expiry"),
        ({"service_url": "http://gateway.example"}, "https"),
        ({"service_url": "https://gateway.example/path"}, "scheme, host and optional port"),
    ],
)
def test_an_unusable_file_is_refused_without_echoing_it(
    tmp_path: Path, change: dict[str, object], why: str
) -> None:
    """Each refusal says why in one line and never repeats a value from the file."""
    with pytest.raises(KwalSessionError, match=why) as refused:
        load_session(saved(tmp_path / "pws", **change))  # pyright: ignore[reportArgumentType]
    assert TOKEN not in str(refused.value)


def test_a_loopback_gateway_may_be_plain_http(tmp_path: Path) -> None:
    """Only loopback is accepted without TLS: the skill's own rule, for a local fixture."""
    session = load_session(saved(tmp_path / "pws", service_url="http://127.0.0.1:8735/"))
    assert session.service_url == "http://127.0.0.1:8735"


def test_the_default_path_follows_the_skill(tmp_path: Path) -> None:
    """``PWS_CREDENTIALS_FILE``, else ``$XDG_CONFIG_HOME/pws/...``, else ``~/.config/pws/...``."""
    named = tmp_path / "named.json"
    assert default_credentials_path({"PWS_CREDENTIALS_FILE": str(named)}) == named
    config = tmp_path / "config"
    assert default_credentials_path({"XDG_CONFIG_HOME": str(config)}) == (
        config / "pws" / "agent-payment" / "credentials.json"
    )
    assert default_credentials_path({"HOME": str(tmp_path)}) == (
        tmp_path / ".config" / "pws" / "agent-payment" / "credentials.json"
    )

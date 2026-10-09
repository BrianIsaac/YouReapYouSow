"""Tests for configuration."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from youreapyousow.config import ConfigError, Settings
from youreapyousow.control import PurchasePath
from youreapyousow.market.service import MarketMode

ENROLMENT = "6f1c2b9e-8a4d-4c3b-9e2f-1a2b3c4d5e6f"


def test_defaults_are_the_safe_local_setup() -> None:
    """Out of the box: mock Reap, live market with fallbacks."""
    settings = Settings()
    settings.check()
    assert settings.reap_backend == "mock"
    assert settings.market_mode == MarketMode.LIVE


def test_env_file_selects_the_agentic_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings come from ``.env`` in the working directory; the key alone starts the sandbox."""
    monkeypatch.delenv("REAP_PURCHASE_PATH")
    (tmp_path / ".env").write_text(
        f"REAP_BACKEND=sandbox\nREAP_API_KEY=sk\nREAP_PURCHASE_PATH=agentic\n"
        f"REAP_ENROLLMENT_ID={ENROLMENT}\n"
    )
    settings = Settings()
    settings.check()
    assert settings.reap_backend == "sandbox"
    assert settings.reap_purchase_path == PurchasePath.AGENTIC
    assert settings.reap_enrollment_id == ENROLMENT


def test_the_purchase_path_defaults_to_agentic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset, ``REAP_PURCHASE_PATH`` is the agentic path, as the control plane reads it."""
    monkeypatch.delenv("REAP_PURCHASE_PATH")
    assert Settings().reap_purchase_path == PurchasePath.AGENTIC
    assert Settings(reap_purchase_path=PurchasePath.CARD).reap_purchase_path == PurchasePath.CARD


def test_the_agentic_sandbox_without_its_key_refuses_to_start() -> None:
    """Selecting the sandbox without the key fails loudly, never falls back to the mock."""
    with pytest.raises(
        ConfigError, match=r"REAP_API_KEY in \.env.*operations\.md, the sandbox swap"
    ):
        Settings(reap_backend="sandbox", reap_purchase_path=PurchasePath.AGENTIC).check()


def test_the_agentic_sandbox_needs_no_webhook_secret() -> None:
    """The agentic flow polls; Reap documents no agentic webhook, so no secret is asked for."""
    settings = Settings(
        reap_backend="sandbox", reap_api_key=SecretStr("k"), reap_purchase_path=PurchasePath.AGENTIC
    )
    settings.check()
    assert settings.reap_webhook_secret is None


def test_the_card_path_on_the_sandbox_still_needs_its_webhook_secret() -> None:
    """The dormant card path learns of transactions by webhook, so its secret stays required."""
    card = PurchasePath.CARD
    with pytest.raises(ConfigError, match="REAP_API_KEY"):
        Settings(reap_backend="sandbox", reap_purchase_path=card).check()
    with pytest.raises(ConfigError, match="REAP_PURCHASE_PATH=card needs REAP_WEBHOOK_SECRET"):
        Settings(
            reap_backend="sandbox", reap_api_key=SecretStr("k"), reap_purchase_path=card
        ).check()
    Settings(
        reap_backend="sandbox",
        reap_api_key=SecretStr("k"),
        reap_webhook_secret=SecretStr("w"),
        reap_purchase_path=card,
    ).check()


def test_an_enrolment_id_must_be_a_reap_uuid() -> None:
    """A mistyped ``REAP_ENROLLMENT_ID`` is refused at once, not at the first checkout."""
    with pytest.raises(ValueError, match="reap_enrollment_id"):
        Settings(reap_enrollment_id="enr_123")


def test_the_enrolment_is_used_on_the_agentic_sandbox_only() -> None:
    """The operator's enrolment exists only at Reap: the mock enrols its own test card."""
    sandbox = Settings(
        reap_backend="sandbox",
        reap_api_key=SecretStr("k"),
        reap_enrollment_id=ENROLMENT,
        reap_purchase_path=PurchasePath.AGENTIC,
    )
    assert sandbox.sandbox_enrollment_id == ENROLMENT
    assert Settings(reap_enrollment_id=ENROLMENT).sandbox_enrollment_id is None
    card = sandbox.model_copy(update={"reap_purchase_path": PurchasePath.CARD})
    assert card.sandbox_enrollment_id is None
    assert sandbox.model_copy(update={"reap_enrollment_id": None}).sandbox_enrollment_id is None


def test_secrets_are_not_printed() -> None:
    """A key never appears in the settings' repr."""
    settings = Settings(reap_api_key=SecretStr("sk_live_do_not_print"))
    assert "sk_live_do_not_print" not in repr(settings)


def test_model_settings_default_to_featherless_glm_and_the_local_gemma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each provider's endpoint and model, with the default call budget."""
    monkeypatch.delenv("MODEL_PROVIDER")
    settings = Settings()
    assert settings.model_provider is None
    assert settings.featherless_api_key is None
    assert settings.featherless_base_url == "https://api.featherless.ai/v1"
    assert settings.featherless_model == "zai-org/GLM-5.3-Flash"
    assert settings.featherless_timeout_s == 4.0
    assert settings.local_model_base_url == "http://127.0.0.1:8090/v1"
    assert settings.local_model == "gemma-4-12b-it"
    assert settings.local_model_timeout_s == 8.0
    assert settings.model_budget_s == 8.0


def test_env_file_names_the_model_provider_and_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MODEL_PROVIDER`` and ``FEATHERLESS_API_KEY`` come from ``.env``; the key stays secret."""
    monkeypatch.delenv("MODEL_PROVIDER")
    (tmp_path / ".env").write_text(
        "MODEL_PROVIDER=featherless\nFEATHERLESS_API_KEY=fl_do_not_print\n"
    )
    settings = Settings()
    settings.check()
    assert settings.model_provider == "featherless"
    assert settings.featherless_api_key == SecretStr("fl_do_not_print")
    assert "fl_do_not_print" not in repr(settings)


def test_featherless_without_its_key_refuses_to_start() -> None:
    """Naming Featherless with no key fails loudly rather than reasoning without a model."""
    with pytest.raises(ConfigError, match="FEATHERLESS_API_KEY"):
        Settings(model_provider="featherless").check()
    Settings(model_provider="local").check()
    Settings(model_provider="none").check()


def test_an_unknown_model_provider_is_refused() -> None:
    """Only the three named providers exist."""
    with pytest.raises(ValueError, match="model_provider"):
        Settings(model_provider="openai")  # pyright: ignore[reportArgumentType]


def test_from_env_names_a_malformed_value_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value that does not parse is one line naming the variable, never the value."""
    monkeypatch.setenv("REAP_ENROLLMENT_ID", "sk_pasted_in_the_wrong_place")
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    with pytest.raises(ConfigError) as refused:
        Settings.from_env()
    message = str(refused.value)
    assert "REAP_ENROLLMENT_ID must be a UUID" in message
    assert "MODEL_PROVIDER " in message
    assert "sk_pasted_in_the_wrong_place" not in message
    assert "\n" not in message
    monkeypatch.delenv("REAP_ENROLLMENT_ID")
    monkeypatch.delenv("MODEL_PROVIDER")
    assert Settings.from_env().reap_backend == "mock"


def test_the_purchase_file_in_force_comes_from_purchase_config(tmp_path: Path) -> None:
    """``PURCHASE_CONFIG`` is read with the rest; unset, the scenario's default applies."""
    assert Settings().purchase_config is None
    (tmp_path / ".env").write_text("PURCHASE_CONFIG=configs/purchase-part.yaml\n")
    assert Settings().purchase_config == Path("configs/purchase-part.yaml")


def test_from_values_reads_only_what_it_is_given(tmp_path: Path) -> None:
    """``from_values`` ignores ``.env`` and the environment, as a check of other values must."""
    (tmp_path / ".env").write_text("REAP_BACKEND=sandbox\n")
    settings = Settings.from_values({"REAP_API_KEY": "sk", "MARKET_MODE": "mock", "OTHER": "x"})
    assert settings.reap_backend == "mock"
    assert settings.reap_purchase_path == PurchasePath.AGENTIC
    assert settings.reap_api_key == SecretStr("sk")
    assert settings.market_mode == MarketMode.MOCK
    with pytest.raises(ConfigError, match=r"^REAP_BACKEND "):
        Settings.from_values({"REAP_BACKEND": "production"})


def _kwal_session(directory: Path, *, expires_at: int = 4_102_444_800) -> Path:
    """Save a Kwal session as the skill's ``register.py`` does, outside any checkout."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "credentials.json"
    path.write_text(
        '{"service_url": "https://api.sandbox.services.payward.com", '
        f'"token": "a.b.c", "expires_at": {expires_at}}}'
    )
    path.chmod(0o600)
    return path


def test_kwal_starts_on_the_session_the_skill_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``REAP_BACKEND=kwal`` needs no Reap key: the participant's session is the credential."""
    monkeypatch.delenv("REAP_PURCHASE_PATH")
    session = _kwal_session(tmp_path / "pws")
    (tmp_path / ".env").write_text(f"REAP_BACKEND=kwal\nPWS_CREDENTIALS_FILE={session}\n")
    settings = Settings()
    settings.check()
    assert settings.reap_backend == "kwal"
    assert settings.kwal_credentials_path() == session
    assert settings.sandbox_enrollment_id is None


def test_kwal_without_a_usable_session_refuses_to_start(tmp_path: Path) -> None:
    """No session, or an expired one, is refused in one line, naming the skill's step."""
    missing = Settings(
        reap_backend="kwal",
        reap_purchase_path=PurchasePath.AGENTIC,
        pws_credentials_file=tmp_path / "none.json",
    )
    with pytest.raises(ConfigError, match=r"REAP_BACKEND=kwal.*register\.py register"):
        missing.check()
    expired = Settings(
        reap_backend="kwal",
        reap_purchase_path=PurchasePath.AGENTIC,
        pws_credentials_file=_kwal_session(tmp_path / "old", expires_at=1),
    )
    with pytest.raises(ConfigError, match="expired"):
        expired.check()


def test_kwal_offers_no_card_path(tmp_path: Path) -> None:
    """Kwal's card is the participant's; the dormant card path has nothing to call on it."""
    settings = Settings(
        reap_backend="kwal",
        reap_purchase_path=PurchasePath.CARD,
        pws_credentials_file=_kwal_session(tmp_path / "pws"),
    )
    with pytest.raises(ConfigError, match="REAP_PURCHASE_PATH=agentic"):
        settings.check()

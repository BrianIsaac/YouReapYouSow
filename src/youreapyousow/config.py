"""Configuration from the environment and ``.env``; secrets are never printed.

The startup check (``Settings.check``) follows the purchase path in force. On the
agentic path (the default) the sandbox needs only its key: the checkout is polled and
Reap documents no agentic webhook event (webhooks overview and changelog), so no tunnel
and no signing secret. On the dormant card path (``REAP_PURCHASE_PATH=card``) the
notification webhook's secret is still required. The agent's model is checked too: a
Featherless key only when Featherless is named.
"""

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from youreapyousow.clock import utc_now
from youreapyousow.control import PurchasePath
from youreapyousow.kwal.session import KwalSessionError, default_credentials_path, load_session
from youreapyousow.market.service import MarketMode
from youreapyousow.reap.client import SG_SANDBOX_URL
from youreapyousow.reap.mock.engine import AuthorizationMode
from youreapyousow.reap.models import UuidId

type ModelProvider = Literal["openai", "featherless", "local", "none"]

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
FEATHERLESS_VISION_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
OPENAI_BASE_URL = "https://api.openai.com/v1"
FEATHERLESS_MODEL = "zai-org/GLM-5.3-Flash"
LOCAL_MODEL_BASE_URL = "http://127.0.0.1:8090/v1"
LOCAL_MODEL = "gemma-4-12b-it"


class ConfigError(ValueError):
    """Raised when the configuration cannot work, rather than silently degrading."""


class Settings(BaseSettings):
    """Every setting, with the safe local default.

    Attributes:
        reap_backend: ``mock`` (default), ``sandbox``, or ``kwal``: Payward's Kwal
            participant gateway over Reap's agentic sandbox, paid by the participant's
            own card and vault (``docs/kwal-backend.md``).
        reap_api_key: Sandbox API key; required for ``sandbox``.
        reap_base_url: Sandbox host; Singapore by default.
        reap_purchase_path: ``agentic`` (default) or the dormant ``card`` path. The
            control plane reads the same variable itself; it is here so the startup
            check knows which credentials the path needs.
        reap_enrollment_id: The operator's agentic enrolment on the sandbox, completed by
            hand on Reap's hosted page; a UUID, as Reap's ids are.
        pws_credentials_file: The Kwal participant session the skill saved; the skill's
            own default path when unset (``kwal/session.py``). ``kwal`` only.
        purchase_config: ``PURCHASE_CONFIG``, the ``purchase:`` file in force; None for
            the scenario's default (``configs/purchase-compute.yaml``).
        reap_authorization_mode: For the mock only: ``MANAGED`` or ``EXTERNAL``. For
            the sandbox this is fixed by Reap at project setup.
        reap_webhook_secret: Signing secret of the sandbox NOTIFICATION endpoint; the
            card path only.
        reap_authorization_secret: Signing secret of the sandbox REQUEST endpoint; the
            card path in External mode only.
        market_mode: ``live`` (default, with cached and mock fallback) or ``mock``.
        market_timeout_s: Per-connector budget.
        market_refresh_s: Background refresh interval.
        database_path: SQLite file for the ledger and records.
        snapshot_path: Where market snapshots persist.
        operator_email: Email of the cardholder of record (the human operator).
        operator_phone: Phone of the cardholder of record.
        model_provider: Which model reasons for the agent: ``featherless``, ``local``
            or ``none`` (deterministic only). Unset, it is ``featherless`` when its key
            is set, else ``local`` when the local server answers, else ``none``.
        featherless_api_key: Featherless key; needed for ``featherless`` only.
        featherless_base_url: Featherless's OpenAI-compatible API root.
        featherless_model: The model asked on Featherless.
        featherless_timeout_s: One Featherless call's timeout, short of the budget so
            the local server can still answer.
        local_model_base_url: The local llama.cpp server's API root.
        local_model: The model the local server serves.
        local_model_timeout_s: One local call's timeout.
        model_budget_s: What one choice may spend on the model across every provider
            before the deterministic strategy decides instead.
        openai_api_key: OpenAI key; with it set and no ``MODEL_PROVIDER``, the coach and
            the photo reads ask OpenAI first and Featherless after.
        openai_base_url: OpenAI's API root.
        coach_model: The coach's first model on OpenAI.
        coach_fallback_model: The coach's second model on OpenAI.
        vision_model: The photo reader's first model on OpenAI.
        vision_fallback_model: The photo reader's second model on OpenAI.
        featherless_vision_model: The photo reader on Featherless, the last fallback.
        openai_timeout_s: One OpenAI call's timeout.
        demo_clock: Demo time: real seconds per challenge day (a minute is a week).
        entry_amount: One entry, in test USDC.
        vault_balance_usdc: The vault's balance when it cannot be read from Kwal.
        enrolment_window_s: Real seconds from a group opening to its deadline.
        dispute_window_s: Real seconds the dispute window stays open.
        duration_days: The challenge's length in challenge days.
        evidence_dir: Where check-in evidence is stored.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    reap_backend: Literal["mock", "sandbox", "kwal"] = "mock"
    reap_api_key: SecretStr | None = None
    reap_base_url: str = SG_SANDBOX_URL
    reap_purchase_path: PurchasePath = PurchasePath.AGENTIC
    reap_enrollment_id: UuidId | None = None
    pws_credentials_file: Path | None = None
    purchase_config: Path | None = None
    reap_authorization_mode: AuthorizationMode = AuthorizationMode.MANAGED
    reap_webhook_secret: SecretStr | None = None
    reap_authorization_secret: SecretStr | None = None
    market_mode: MarketMode = MarketMode.LIVE
    market_timeout_s: float = 2.0
    market_refresh_s: float = 300.0
    database_path: Path = Path("var/youreapyousow.db")
    snapshot_path: Path = Path("var/market-snapshot.json")
    operator_email: str = "operator@example.com"
    operator_phone: str = "+6500000000"
    model_provider: ModelProvider | None = None
    featherless_api_key: SecretStr | None = None
    featherless_base_url: str = FEATHERLESS_BASE_URL
    featherless_model: str = FEATHERLESS_MODEL
    featherless_timeout_s: float = 4.0
    local_model_base_url: str = LOCAL_MODEL_BASE_URL
    local_model: str = LOCAL_MODEL
    local_model_timeout_s: float = 8.0
    model_budget_s: float = 8.0
    openai_api_key: SecretStr | None = None
    openai_base_url: str = OPENAI_BASE_URL
    coach_model: str = "gpt-5.6-terra"
    coach_fallback_model: str = "gpt-5.6-sol"
    vision_model: str = "gpt-5.6-terra"
    vision_fallback_model: str = "gpt-6-luna"
    featherless_vision_model: str = FEATHERLESS_VISION_MODEL
    openai_timeout_s: float = 25.0
    demo_clock: float = Field(default=60 / 7, gt=0)
    entry_amount: Decimal = Field(default=Decimal("25.00"), gt=0)
    vault_balance_usdc: Decimal = Field(default=Decimal("500.00"), ge=0)
    enrolment_window_s: float = Field(default=3600.0, gt=0)
    dispute_window_s: float = Field(default=20.0, ge=0)
    duration_days: int = Field(default=28, ge=4)
    evidence_dir: Path = Path("var/evidence")

    @property
    def chat_provider(self) -> ModelProvider:
        """Return who answers the coach and the photo reads first.

        Returns:
            ``MODEL_PROVIDER`` when set; else ``openai`` when its key is set, else
            ``featherless`` when its key is set, else ``none``.
        """
        if self.model_provider is not None:
            return self.model_provider
        if self.openai_api_key is not None:
            return "openai"
        if self.featherless_api_key is not None:
            return "featherless"
        return "none"

    @classmethod
    def from_env(cls) -> "Settings":
        """Read the settings from the environment and ``.env``, refusing a malformed value.

        Returns:
            The settings, not yet checked as a whole (``check``).

        Raises:
            ConfigError: If a value does not parse, naming each variable and why in one
                line, never the value itself, which may be a key pasted into the wrong
                variable.
        """
        try:
            return cls()
        except ValidationError as error:
            raise _refusal(error) from None

    @classmethod
    def from_values(cls, values: Mapping[str, str]) -> "Settings":
        """Build the settings from these variables alone, as a check of other values must.

        Neither the process environment nor ``.env`` is read; variables that name no
        setting are ignored.

        Args:
            values: Variables by their environment name, such as ``REAP_BACKEND``.

        Returns:
            The settings, not yet checked as a whole (``check``).

        Raises:
            ConfigError: If a value does not parse, as ``from_env`` says it.
        """
        given = {name: values[name.upper()] for name in cls.model_fields if name.upper() in values}
        try:
            return _GivenValues.model_validate(given)
        except ValidationError as error:
            raise _refusal(error) from None

    @property
    def sandbox_enrollment_id(self) -> str | None:
        """Return the operator's enrolment when it is the one purchases are charged to.

        Returns:
            ``REAP_ENROLLMENT_ID`` on the agentic path against the sandbox; None on the
            mock, which enrols its own published test card, and on the card path.
        """
        if self.reap_backend != "sandbox" or self.reap_purchase_path != PurchasePath.AGENTIC:
            return None
        return self.reap_enrollment_id

    def kwal_credentials_path(self) -> Path:
        """Return where the Kwal participant session is read from.

        Returns:
            ``PWS_CREDENTIALS_FILE``, else the skill's default path.
        """
        return self.pws_credentials_file or default_credentials_path()

    def check(self) -> None:
        """Refuse combinations that cannot work.

        Raises:
            ConfigError: If the sandbox is selected without its key, the card path on the
                sandbox without its webhook secret, Kwal without a usable session or on
                the card path, or Featherless without its key.
        """
        self.check_model()
        if self.reap_backend == "kwal":
            self.check_kwal()
            return
        if self.reap_backend != "sandbox":
            return
        if self.reap_api_key is None:
            raise ConfigError(
                "REAP_BACKEND=sandbox needs REAP_API_KEY in .env; add the sandbox key, "
                "or set REAP_BACKEND=mock (docs/operations.md, the sandbox swap)"
            )
        if self.reap_purchase_path == PurchasePath.CARD and self.reap_webhook_secret is None:
            raise ConfigError(
                "REAP_BACKEND=sandbox with REAP_PURCHASE_PATH=card needs REAP_WEBHOOK_SECRET "
                "in .env; register the notification webhook through the tunnel and append "
                "its secret first, or set REAP_PURCHASE_PATH=agentic "
                "(docs/operations.md, the card path)"
            )

    def check_kwal(self) -> None:
        """Refuse Kwal without a usable participant session, or on the card path.

        Raises:
            ConfigError: If the session is missing, unreadable or expired, or
                ``REAP_PURCHASE_PATH=card``; never naming a value from the session.
        """
        if self.reap_purchase_path == PurchasePath.CARD:
            raise ConfigError(
                "REAP_BACKEND=kwal pays with the participant's own card: set "
                "REAP_PURCHASE_PATH=agentic (docs/kwal-backend.md)"
            )
        try:
            session = load_session(self.kwal_credentials_path())
        except KwalSessionError as error:
            raise ConfigError(f"REAP_BACKEND=kwal: {error} (docs/kwal-backend.md)") from None
        if session.expired(utc_now()):
            raise ConfigError(
                f"REAP_BACKEND=kwal: the Kwal session at {session.path} has expired and "
                "cannot be renewed; register a new participant (docs/kwal-backend.md)"
            )

    def check_model(self) -> None:
        """Refuse a model provider that cannot answer.

        Raises:
            ConfigError: If Featherless is named without its key.
        """
        if self.model_provider == "openai" and self.openai_api_key is None:
            raise ConfigError("MODEL_PROVIDER=openai needs OPENAI_API_KEY in .env")
        if self.model_provider == "featherless" and self.featherless_api_key is None:
            raise ConfigError(
                "MODEL_PROVIDER=featherless needs FEATHERLESS_API_KEY in .env; add the key, "
                "or set MODEL_PROVIDER=local or none (docs/market-and-ml.md)"
            )


class _GivenValues(Settings):
    """``Settings`` read from the values passed in, and from nothing else."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read the initial values only.

        Args:
            settings_cls: The settings class.
            init_settings: The values passed in.
            env_settings: The process environment; not read.
            dotenv_settings: ``.env``; not read.
            file_secret_settings: Secret files; not read.

        Returns:
            The one source.
        """
        return (init_settings,)


def _refusal(error: ValidationError) -> ConfigError:
    """Say in one line which variables do not parse, never their values.

    Args:
        error: The validation error.

    Returns:
        The refusal.
    """
    reasons = "; ".join(
        f"{str(e['loc'][0]).upper()} {e['msg'].removeprefix('Value error, ')}"
        for e in error.errors(include_input=False, include_url=False)
    )
    return ConfigError(f"{reasons} (docs/configuration.md)")

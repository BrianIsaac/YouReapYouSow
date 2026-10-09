"""The ``purchase:`` block: what the agentic path buys, from where, under what grant.

``PURCHASE_CONFIG`` names the file in force, from the environment or ``.env``, and
``configs/purchase-prize.yaml`` is the default. Swapping the product is editing or
choosing a file and restarting; no Python changes.

Choices made here:

* The grant block carries ``budget_usd``: the item's price decides the budget an
  objective needs, so it moves with the product, and the caps are checked against it at
  load time as the gate checks them at issue. It also carries ``ttl_hours``.
* A relative ``PURCHASE_CONFIG`` is read from the working directory.
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from youreapyousow.control import AgenticSettings, GrantTerms
from youreapyousow.procure.need import NeedSpec
from youreapyousow.reap.models import HttpsUrl

PURCHASE_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"
DEFAULT_PURCHASE_CONFIG = PURCHASE_CONFIG_DIR / "purchase-prize.yaml"


class ScenarioError(ValueError):
    """Raised when a purchase file is missing its block or does not validate."""


class _PurchaseBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PurchaseCheckout(_PurchaseBlock):
    """How a checkout is sent and waited for.

    Attributes:
        simulate_completed_when_allowed: Send the sandbox header on checkouts the gate
            allowed without the operator.
        poll_every_s: The first wait between checkout reads.
        poll_deadline_s: How long a checkout may stay ``PROCESSING``.
    """

    simulate_completed_when_allowed: bool = True
    poll_every_s: float = Field(default=1.0, gt=0)
    poll_deadline_s: float = Field(default=120.0, ge=0)


class PurchaseGrant(_PurchaseBlock):
    """The authority the objective's agent buys under, on the landed price.

    Attributes:
        budget_usd: The objective's total budget.
        per_purchase_usd: The largest single landed ``finalAmount``.
        daily_usd: The largest total per UTC day.
        approval_threshold_usd: Above this, the operator approves, if set.
        attempts_per_need: Attempts at one need that may end failed, expired or declined.
        quote_margin_s: How long before Reap's ``expiresAt`` a quote stops being bought.
        ttl_hours: How long the authority lasts.
    """

    budget_usd: Decimal = Field(gt=0)
    per_purchase_usd: Decimal = Field(gt=0)
    daily_usd: Decimal = Field(gt=0)
    approval_threshold_usd: Decimal | None = Field(default=None, gt=0)
    attempts_per_need: int = Field(default=3, ge=1)
    quote_margin_s: int = Field(default=15, ge=0)
    ttl_hours: float = Field(default=4.0, gt=0)

    @model_validator(mode="after")
    def _caps_hold_together(self) -> Self:
        if self.per_purchase_usd > self.daily_usd:
            raise ValueError("per_purchase_usd exceeds daily_usd")
        if self.daily_usd > self.budget_usd:
            raise ValueError("daily_usd exceeds budget_usd")
        return self


class PurchaseConfig(NeedSpec):
    """The ``purchase:`` block: the need's specification, the grant and the checkout.

    Attributes:
        merchants: The grant's merchant scope, as Reap names the merchants.
        return_url: The https URI Reap returns the browser to after a hosted step.
        checkout: How checkouts are sent and waited for.
        grant: The authority on the landed price.
    """

    merchants: tuple[str, ...] = Field(min_length=1)
    return_url: HttpsUrl
    checkout: PurchaseCheckout = PurchaseCheckout()
    grant: PurchaseGrant

    @model_validator(mode="after")
    def _cart_in_scope(self) -> Self:
        cart = self.external_checkout
        if cart is not None and cart.merchant not in self.merchants:
            raise ValueError(
                f"the cart's merchant {cart.merchant} is not in merchants {list(self.merchants)}"
            )
        return self

    def need_spec(self) -> NeedSpec:
        """Return what a need raised under this block buys.

        Returns:
            The need's specification.
        """
        return NeedSpec.model_validate(
            {name: getattr(self, name) for name in NeedSpec.model_fields}
        )

    @property
    def terms(self) -> GrantTerms:
        """Return the grant ``create_objective`` issues.

        Returns:
            Merchant-scoped terms with no providers: the agentic path buys from merchants.
        """
        grant = self.grant
        return GrantTerms(
            allowed_providers=(),
            per_transaction_cap_usd=grant.per_purchase_usd,
            daily_cap_usd=grant.daily_usd,
            ttl=timedelta(hours=grant.ttl_hours),
            approval_threshold_usd=grant.approval_threshold_usd,
            allowed_merchants=self.merchants,
            attempts_per_need=grant.attempts_per_need,
            quote_margin_s=grant.quote_margin_s,
        )

    @property
    def settings(self) -> AgenticSettings:
        """Return how the control plane enrols and checks out under this block.

        Returns:
            The settings, with the defaults for what the block does not set.
        """
        return AgenticSettings(
            return_url=self.return_url,
            simulate_completed_when_allowed=self.checkout.simulate_completed_when_allowed,
            poll_every_s=self.checkout.poll_every_s,
            poll_deadline_s=self.checkout.poll_deadline_s,
        )


class _PurchaseFile(BaseSettings):
    """``PURCHASE_CONFIG``: the purchase file in force."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    purchase_config: Path = DEFAULT_PURCHASE_CONFIG


def load_purchase(path: Path | None = None) -> PurchaseConfig:
    """Read and validate a ``purchase:`` block.

    Args:
        path: The file; ``PURCHASE_CONFIG`` (or the compute file) when None.

    Returns:
        The validated block.

    Raises:
        ScenarioError: If the file has no ``purchase:`` block or it does not validate.
    """
    path = path or _PurchaseFile().purchase_config
    raw: object = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "purchase" not in raw:
        raise ScenarioError(f"{path} has no purchase: block")
    try:
        return PurchaseConfig.model_validate(raw["purchase"])
    except ValidationError as error:
        raise ScenarioError(f"{path}: {error}") from error

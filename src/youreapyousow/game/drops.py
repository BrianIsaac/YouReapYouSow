"""The drops on offer: each its own prize, entry, seats, fixed date range and group.

``configs/drops.yaml`` lists them; the first is featured. A drop whose prize Reap's sandbox
cannot sell in USD (the pool's currency) is bought on the mock, and says so.
"""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from youreapyousow.game.models import CENT
from youreapyousow.game.prize import PrizeBuyer, preview_quote, prize_block
from youreapyousow.game.service import GameService, compute_pool, money
from youreapyousow.purchase import PURCHASE_CONFIG_DIR, PurchaseConfig
from youreapyousow.reap.client import ReapClient

DROPS_CONFIG = PURCHASE_CONFIG_DIR / "drops.yaml"


class DropConfig(BaseModel):
    """One drop as ``configs/drops.yaml`` writes it.

    Attributes:
        id: The drop's id, used in the routes.
        title: Its title.
        purchase: The prize's purchase file, relative to ``configs/``.
        list_price: The merchant sheet's price.
        currency: That price's currency.
        entry_amount: One entry, in test USDC.
        duration_days: The challenge's length in challenge days.
        live_purchase: Whether the prize is bought on the configured backend; else the mock.
        note: One line on why it is not, when it is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    title: str
    purchase: str
    list_price: Decimal
    currency: str = Field(min_length=3, max_length=3)
    entry_amount: Decimal = Field(gt=0)
    duration_days: int = Field(ge=4)
    live_purchase: bool = True
    note: str | None = None


class _DropsFile(BaseModel):
    drops: list[DropConfig] = Field(min_length=1)


def load_drops(path: Path = DROPS_CONFIG) -> list[DropConfig]:
    """Read the drops.

    Args:
        path: The drops file.

    Returns:
        The drops, the featured one first.
    """
    return _DropsFile.model_validate(yaml.safe_load(path.read_text())).drops


@dataclass
class Drop:
    """A drop at runtime: its group service, its prize, and who buys it.

    Attributes:
        config: The drop's configuration.
        service: Its group's state machine.
        purchase: The prize's purchase file.
        reap: The backend its prize is quoted and bought on.
        backend: That backend's name.
        buyer: Buys its prize behind the gate.
        prize: The prize block of its state.
    """

    config: DropConfig
    service: GameService
    purchase: PurchaseConfig
    reap: ReapClient
    backend: str
    buyer: PrizeBuyer
    prize: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])

    def __post_init__(self) -> None:
        """Start the prize block from the configuration, before any quote lands."""
        self.prize = {
            "name": self.purchase.search.query if self.purchase.search else self.config.title,
            "merchant": self.purchase.merchants[0],
            "list_price": money(self.config.list_price),
            "currency": self.config.currency,
            "image_url": None,
            "quote": None,
            "live_purchase": self.config.live_purchase,
        }

    async def preview(self) -> None:
        """Land the prize's quote for the disclosure; leave it unknown if the backend is slow."""
        for _ in range(3):
            try:
                preview = await asyncio.wait_for(preview_quote(self.reap, self.purchase), 40)
            except Exception:
                continue
            block = prize_block(preview, self.backend)
            block["list_price"] = money(self.config.list_price)
            block["currency"] = self.config.currency
            block["live_purchase"] = self.config.live_purchase
            self.prize = block
            self.service.prize_quote = preview.final_amount
            return

    def summary(self) -> dict[str, JsonValue]:
        """Render the drop for the list.

        Returns:
            The drop summary the contract describes.
        """
        service = self.service
        group = service.group()
        players = service.players()
        terms = group.terms
        full = compute_pool(
            terms.entry_amount, terms.max_players, terms.buffer_rate, service.prize_quote
        )
        return {
            "drop_id": self.config.id,
            "title": self.config.title,
            "prize": dict(self.prize),
            "entry_amount": money(terms.entry_amount),
            "currency": terms.currency,
            "seats": {"taken": len(players), "max": terms.max_players},
            "status": group.status.value,
            "starts_at": group.starts_at.isoformat() if group.starts_at else None,
            "ends_at": group.ends_at.isoformat() if group.ends_at else None,
            "duration_days": terms.duration_days,
            "pool_at_capacity": {
                "entries": full.entries,
                "gross": money(full.gross),
                "buffer": money(full.buffer),
                "ceiling": money(full.ceiling),
                "surplus": None if full.surplus is None else str(full.surplus.quantize(CENT)),
            },
            "note": self.config.note,
        }

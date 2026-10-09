"""Ranking landed quotes: fills the need, merchant in scope, lowest landed price."""

from datetime import UTC, datetime

from tests.factories import make_landed_quote, make_variant
from youreapyousow.domain import LandedQuote
from youreapyousow.procure.need import AttributeMatch, CatalogueSearch, Need, NeedSpec, Shape
from youreapyousow.procure.selection import best_quote, in_scope, rank_quotes

NEED = Need(
    id="need_1",
    objective_id="obj_1",
    spec=NeedSpec(
        shape=Shape.PART,
        search=CatalogueSearch(query="1TB NVMe"),
        match=AttributeMatch(attributes={"Capacity": ("1 TB", "2 TB"), "Interface": ("NVMe",)}),
        email="operator@example.com",
    ),
    reason="drive failing",
    raised_at=datetime(2037, 10, 9, tzinfo=UTC),
)
MERCHANTS = ("Northwind Components", "Kestrel Parts")


def _quote(quote_id: str, merchant: str, final: str, **options: str) -> LandedQuote:
    variant = make_variant(
        variant_id=f"var-{quote_id}",
        merchant=merchant,
        options={"Capacity": "1 TB", "Interface": "NVMe"} | options,
    )
    return make_landed_quote(quote_id=quote_id, variant=variant, final=final)


def test_cheapest_landed_price_wins_not_the_cheapest_item() -> None:
    """The order is by ``finalAmount``, which includes shipping and tax."""
    quotes = [
        _quote("kestrel", "Kestrel Parts", "79.00"),
        _quote("northwind", "Northwind Components", "77.00"),
        _quote("equivalent", "Northwind Components", "107.00", Capacity="2 TB"),
    ]
    ranked = rank_quotes(quotes, need=NEED, merchants=MERCHANTS)
    assert [r.quote.id for r in ranked] == ["northwind", "kestrel", "equivalent"]
    assert all(r.eligible for r in ranked)
    assert best_quote(quotes, need=NEED, merchants=MERCHANTS) == quotes[1]


def test_ineligible_quotes_rank_last_with_their_reason() -> None:
    """A wrong part or an out-of-scope merchant is never the choice, however cheap."""
    quotes = [
        _quote("sata", "Northwind Components", "40.00", Interface="SATA"),
        _quote("elsewhere", "Contoso Hardware", "50.00"),
        _quote("good", "kestrel   parts", "90.00"),
    ]
    ranked = rank_quotes(quotes, need=NEED, merchants=MERCHANTS)
    assert [(r.quote.id, r.eligible) for r in ranked] == [
        ("good", True),
        ("sata", False),
        ("elsewhere", False),
    ]
    assert "SATA" in ranked[1].reason
    assert "merchant scope" in ranked[2].reason


def test_another_currency_is_not_compared() -> None:
    """A landed price in another currency cannot be ranked against the budget's."""
    sgd = make_landed_quote(quote_id="sgd", final="10.00", currency="SGD")
    ranked = rank_quotes([sgd], need=NEED, merchants=("Northwind Parts",))
    assert not ranked[0].eligible
    assert "SGD" in ranked[0].reason
    assert best_quote([sgd], need=NEED, merchants=("Northwind Parts",)) is None


def test_ties_break_deterministically() -> None:
    """Equal landed prices order by merchant, then variant, so a rerun chooses the same."""
    quotes = [
        _quote("b", "Northwind Components", "77.00"),
        _quote("a", "Northwind Components", "77.00"),
        _quote("k", "Kestrel Parts", "77.00"),
    ]
    ranked = rank_quotes(quotes, need=NEED, merchants=MERCHANTS)
    assert [r.quote.id for r in ranked] == ["k", "a", "b"]


def test_nothing_to_choose_from() -> None:
    """No quotes, no choice."""
    assert rank_quotes([], need=NEED, merchants=MERCHANTS) == []
    assert best_quote([], need=NEED, merchants=MERCHANTS) is None


def test_scope_matches_as_the_gate_does() -> None:
    """Case and runs of white space do not matter; another merchant is out of scope."""
    assert in_scope("kestrel   PARTS", MERCHANTS)
    assert not in_scope("Contoso Hardware", MERCHANTS)

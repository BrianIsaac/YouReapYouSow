"""The need and how a candidate variant or landed quote is judged to fill it (rule 10)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.factories import make_landed_quote, make_variant
from youreapyousow.domain import CatalogueVariant, Price
from youreapyousow.procure.need import (
    AttributeMatch,
    CatalogueSearch,
    CheckoutUrl,
    Need,
    NeedSpec,
    Route,
    Shape,
    ValueMatch,
    ValueOption,
    judge_quote,
    judge_variant,
    need_judge,
)
from youreapyousow.reap.models import ShippingAddress

RAISED = datetime(2037, 10, 9, 8, 42, 3, tzinfo=UTC)
ADDRESS = ShippingAddress(
    first_name="Site",
    last_name="Engineer",
    phone="+6561234567",
    address_line1="1 Example Road",
    city="Singapore",
    postal_code="000001",
    country="SG",
)
DRIVE = AttributeMatch(
    attributes={"Capacity": ("1 TB", "2 TB"), "Interface": ("NVMe",), "Form factor": ("M.2 2280",)}
)
CREDITS = ValueMatch(value_from=ValueOption(option="Amount"), max_multiple=Decimal(4))


def _spec(**changes: object) -> NeedSpec:
    base: dict[str, object] = {
        "shape": Shape.PART,
        "route": Route.CATALOGUE,
        "search": CatalogueSearch(query="1TB NVMe M.2 2280 SSD"),
        "match": DRIVE,
        "quantity": 1,
        "email": "operator@example.com",
        "shipping_address": ADDRESS,
    }
    return NeedSpec.model_validate(base | changes)


def _need(spec: NeedSpec | None = None, *, estimate: str | None = None) -> Need:
    return Need(
        id="need_1",
        objective_id="obj_1",
        spec=spec or _spec(),
        reason="drive failing",
        estimate_usd=Decimal(estimate) if estimate is not None else None,
        raised_at=RAISED,
    )


def _drive(**options: str) -> CatalogueVariant:
    return make_variant(
        variant_id="var-1",
        options={"Capacity": "1 TB", "Interface": "NVMe", "Form factor": "M.2 2280"} | options,
    )


def _credit(amount: str, *, price: str | None = None) -> CatalogueVariant:
    return CatalogueVariant(
        id=f"var-credit-{amount}",
        product_id="prd-credits",
        merchant="Northwind Cloud",
        options={"Amount": f"USD {amount}"},
        price=Price(amount=Decimal(price or amount), currency="USD"),
    )


def test_a_drive_matching_every_attribute_fills_the_need() -> None:
    """Each attribute of the variant is one of the accepted values."""
    ok, detail = judge_variant(_need(), _drive(), quantity=1)
    assert ok, detail
    assert "Capacity 1 TB" in detail


def test_an_approved_equivalent_fills_the_need() -> None:
    """A 2 TB drive is an accepted value of the Capacity attribute."""
    assert judge_variant(_need(), _drive(Capacity="2 TB"), quantity=1)[0]


def test_values_compare_without_case_or_spacing() -> None:
    """``1TB`` and ``nvme`` are the same values as ``1 TB`` and ``NVMe``."""
    assert judge_variant(_need(), _drive(Capacity="1TB", Interface="nvme"), quantity=1)[0]


def test_a_wrong_attribute_is_refused_and_named() -> None:
    """A SATA drive does not fill an NVMe need."""
    ok, detail = judge_variant(_need(), _drive(Interface="SATA"), quantity=1)
    assert not ok
    assert "Interface" in detail
    assert "SATA" in detail


def test_a_missing_attribute_is_refused() -> None:
    """A variant that does not state its capacity cannot be judged, so it fails closed."""
    variant = make_variant(options={"Interface": "NVMe", "Form factor": "M.2 2280"})
    ok, detail = judge_variant(_need(), variant, quantity=1)
    assert not ok
    assert "Capacity" in detail


def test_value_covers_the_estimate_within_the_multiple() -> None:
    """A USD 100 credit covers a 40 USD estimate at 2.5 times, within 4."""
    need = _need(_spec(shape=Shape.COMPUTE, match=CREDITS), estimate="40")
    ok, detail = judge_variant(need, _credit("100"), quantity=1)
    assert ok, detail
    assert "100" in detail


def test_value_below_the_estimate_is_refused() -> None:
    """A USD 50 credit does not cover a 60 USD estimate."""
    need = _need(_spec(shape=Shape.COMPUTE, match=CREDITS), estimate="60")
    ok, detail = judge_variant(need, _credit("50"), quantity=1)
    assert not ok
    assert "does not cover" in detail


def test_value_beyond_the_multiple_is_refused() -> None:
    """No buying a year to fix an hour: a USD 250 credit is over 4 times a 40 USD estimate."""
    need = _need(_spec(shape=Shape.COMPUTE, match=CREDITS), estimate="40")
    ok, detail = judge_variant(need, _credit("250"), quantity=1)
    assert not ok
    assert "multiple" in detail


def test_value_from_price_counts_the_quantity() -> None:
    """Two plans at 29 are worth 58 against a 50 estimate."""
    match = ValueMatch(value_from="price", max_multiple=Decimal(2))
    need = _need(_spec(shape=Shape.COMPUTE, match=match), estimate="50")
    plan = make_variant(variant_id="var-plan", price="29", options={"Tier": "Starter"})
    assert judge_variant(need, plan, quantity=2)[0]
    assert not judge_variant(need, plan, quantity=1)[0]


def test_an_option_value_without_one_number_fails_closed() -> None:
    """``Pro`` names no value, and ``USD 50 or 100`` names two; neither is guessed."""
    need = _need(_spec(shape=Shape.COMPUTE, match=CREDITS), estimate="40")
    for label in ("Pro", "USD 50 or 100"):
        variant = _credit("50").model_copy(update={"options": {"Amount": label}})
        ok, detail = judge_variant(need, variant, quantity=1)
        assert not ok
        assert label in detail


def test_value_in_another_currency_fails_closed() -> None:
    """A price in SGD is not compared with a USD estimate."""
    match = ValueMatch(value_from="price")
    need = _need(_spec(shape=Shape.COMPUTE, match=match), estimate="40")
    variant = _credit("100").model_copy(
        update={"price": Price(amount=Decimal(100), currency="SGD")}
    )
    assert not judge_variant(need, variant, quantity=1)[0]


def test_a_value_match_needs_an_estimate_when_raised() -> None:
    """Without the estimate the coverage cannot be judged, so the need is refused."""
    with pytest.raises(ValidationError, match="estimate"):
        _need(_spec(shape=Shape.COMPUTE, match=CREDITS))


def test_the_checkout_url_route_is_judged_by_the_configured_cart() -> None:
    """Only the operator's configured cart from its merchant fills a checkout-URL need."""
    target = CheckoutUrl(
        merchant="Example Merchant",
        merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/var_123:1",
    )
    need = _need(_spec(route=Route.CHECKOUT_URL, search=None, match=None, external_checkout=target))
    cart = CatalogueVariant(
        id=target.checkout_url,
        product_id=target.merchant_domain,
        merchant="Example Merchant",
        price=Price(amount=Decimal(129), currency="USD"),
    )
    assert judge_variant(need, cart, quantity=1)[0]
    other = cart.model_copy(update={"id": "https://merchant.example/cart/var_124:1"})
    assert not judge_variant(need, other, quantity=1)[0]
    elsewhere = cart.model_copy(update={"merchant": "Kestrel Parts"})
    assert not judge_variant(need, elsewhere, quantity=1)[0]


def test_a_quote_for_another_need_is_refused() -> None:
    """The judgement is of this need; a quote raised for another one never fills it."""
    quote = make_landed_quote(need_id="need_2", variant=_drive())
    ok, detail = judge_quote(_need(), quote)
    assert not ok
    assert "need_2" in detail


def test_a_quote_is_judged_on_its_variant_and_quantity() -> None:
    """The landed quote's variant and quantity are what rule 10 reads."""
    good = make_landed_quote(need_id="need_1", variant=_drive())
    assert judge_quote(_need(), good)[0]
    bad = make_landed_quote(need_id="need_1", variant=_drive(Interface="SATA"))
    assert not judge_quote(_need(), bad)[0]


def test_the_gate_judge_reads_the_need_it_is_given() -> None:
    """The gate's judge looks the need up by id and fails closed on an unknown one."""
    needs = {"need_1": _need()}
    judge = need_judge(needs.get)
    assert judge(make_landed_quote(need_id="need_1", variant=_drive()))[0]
    ok, detail = judge(make_landed_quote(need_id="need_9", variant=_drive()))
    assert not ok
    assert "need_9" in detail


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"search": None}, "search"),
        ({"match": None}, "match"),
        ({"route": Route.CHECKOUT_URL}, "external_checkout"),
        ({"quantity": 0}, "quantity"),
        ({"email": "not-an-email"}, "email"),
    ],
)
def test_an_incomplete_spec_is_refused(changes: dict[str, object], message: str) -> None:
    """A catalogue need names a search and a match; a checkout-URL need names its cart."""
    with pytest.raises(ValidationError, match=message):
        _spec(**changes)


def test_a_checkout_url_need_requires_an_address() -> None:
    """Reap requires ``shippingAddress`` on every external checkout quote."""
    target = CheckoutUrl(
        merchant="Example Merchant",
        merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/var_123:1",
    )
    with pytest.raises(ValidationError, match="shipping_address"):
        _spec(route=Route.CHECKOUT_URL, external_checkout=target, shipping_address=None)


def test_the_search_becomes_reaps_request() -> None:
    """Snake case in the file, Reap's camel case on the wire, the price band as strings."""
    search = CatalogueSearch.model_validate(
        {
            "query": "GPU compute credits",
            "merchant_preference": {"mode": "PREFER", "merchant_name": "Northwind Cloud"},
            "context": {"country": "SG", "currency": "USD"},
            "price": {"min": "40", "max": "200"},
            "availability": "AVAILABLE_ONLY",
            "limit": 20,
        }
    )
    assert search.request().to_wire() == {
        "query": "GPU compute credits",
        "merchantPreference": {"mode": "PREFER", "merchantName": "Northwind Cloud"},
        "context": {"country": "SG", "currency": "USD"},
        "filters": {"price": {"min": "40", "max": "200"}, "availability": "AVAILABLE_ONLY"},
        "pagination": {"limit": 20},
    }


def test_the_search_refuses_unknown_keys() -> None:
    """A typo in the file is an error, not a silently ignored filter."""
    with pytest.raises(ValidationError, match="merchant_prefrence"):
        CatalogueSearch.model_validate({"query": "x", "merchant_prefrence": None})


def test_the_need_describes_what_it_buys() -> None:
    """The search query, or the configured cart, is the need in words."""
    assert _need().what == "1TB NVMe M.2 2280 SSD"


def test_an_unnamed_option_accepts_any_value() -> None:
    """Only the named options are constrained; ``Colour`` is not, ``Interface`` is."""
    assert DRIVE.accepts("Colour", "Red")
    assert DRIVE.accepts("interface", "nvme")
    assert not DRIVE.accepts("Interface", "SATA")

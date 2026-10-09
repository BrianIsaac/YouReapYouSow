"""Tests for the agentic mock engine and its catalogues, without HTTP."""

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from youreapyousow.clock import ManualClock
from youreapyousow.reap.mock.agentic import (
    BUNDLED_CATALOGUES,
    TEST_CARDS,
    TEST_OTP,
    AgenticMockConfig,
    AgenticMockEngine,
    AgenticMockError,
    Catalogue,
    Environment,
    HostedStepError,
    Operation,
    SimulatedCreate,
    bundled_catalogue,
    load_catalogue,
)
from youreapyousow.reap.models import (
    AgenticErrorCode,
    CheckoutStatus,
    ClientReferenceOwner,
    CreateBinSponsorEnrollmentRequest,
    CreateCheckoutRequest,
    CreateExternalCheckoutQuoteRequest,
    CreateExternalEnrollmentRequest,
    CreateItemsQuoteRequest,
    CreateReapCardEnrollmentRequest,
    EnrollmentStatus,
    ExternalCheckout,
    MerchantPreference,
    Presentation,
    PriceFilter,
    ProductDetailsRequest,
    ProductSearchRequest,
    Quote,
    QuoteItem,
    ResolveVariantRequest,
    SearchContext,
    SearchFilters,
    SearchPagination,
    SelectShippingOptionRequest,
    ShippingAddress,
)

CATALOGUE_DIR = Path(__file__).parents[3] / "src" / "youreapyousow" / "reap" / "mock" / "catalogue"


def _minimal() -> dict[str, Any]:
    """A valid one-merchant, one-product catalogue file to break in tests.

    Returns:
        The file's content.
    """
    return {
        "name": "tiny",
        "description": "A test catalogue.",
        "currency": "USD",
        "default_country": "SG",
        "countries": {"SG": {"tax_rate": "0.09", "tax_included": True, "calling_code": "65"}},
        "merchants": [
            {
                "name": "Tiny Shop",
                "ships_to": ["SG"],
                "shipping_options": [
                    {"id": "tiny-standard", "name": "Standard", "price": "4", "selected": True}
                ],
                "products": [
                    {
                        "id": "prd-tiny",
                        "name": "Tiny Widget",
                        "variants": [
                            {"id": "var-tiny-red", "options": {"Colour": "Red"}, "price": "10"},
                            {"id": "var-tiny-blue", "options": {"Colour": "Blue"}, "price": "12"},
                        ],
                    }
                ],
            }
        ],
    }


def _write(tmp_path: Path, content: dict[str, Any], name: str = "tiny.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(content), encoding="utf-8")
    return path


# Catalogues


def test_the_three_bundled_catalogues_load_and_merge() -> None:
    """compute, parts, headphones and prizes ship with the package and load together."""
    assert BUNDLED_CATALOGUES == ("compute", "parts", "headphones", "prizes")
    assert {path.stem for path in CATALOGUE_DIR.glob("*.yaml")} == set(BUNDLED_CATALOGUES)
    merged = bundled_catalogue()
    assert merged.names == BUNDLED_CATALOGUES
    assert [m.name for m in merged.merchants] == [
        "Northwind Cloud",
        "Kestrel Compute",
        "Northwind Components",
        "Kestrel Parts",
        "Example Merchant",
        "Keychron",
    ]
    assert bundled_catalogue("parts").names == ("parts",)


def test_an_unknown_bundled_name_is_refused() -> None:
    """Only the three bundled catalogues can be named."""
    with pytest.raises(ValueError, match="flights"):
        bundled_catalogue("flights")


def test_compute_sells_the_same_credit_from_two_merchants() -> None:
    """Two merchants sell USD 100 of credits; neither needs shipping."""
    catalogue = bundled_catalogue("compute")
    northwind = catalogue.variant("var-northwind-gpu-credits-100")
    kestrel = catalogue.variant("var-kestrel-gpu-credits-100")
    assert northwind.merchant.name != kestrel.merchant.name
    assert northwind.variant.options == kestrel.variant.options == {"Amount": "USD 100"}
    assert kestrel.variant.price < northwind.variant.price
    assert not northwind.variant.requires_shipping
    assert not kestrel.variant.requires_shipping


def test_parts_holds_every_part_the_purchase_files_name() -> None:
    """Two 1 TB drives, a 2 TB equivalent, a premium drive, a premium-only fan and a PSU."""
    catalogue = bundled_catalogue("parts")
    one_tb = [
        entry for entry in catalogue.variants() if entry.variant.options.get("Capacity") == "1 TB"
    ]
    assert {entry.merchant.name for entry in one_tb} == {"Northwind Components", "Kestrel Parts"}
    assert catalogue.variant("var-northwind-nvme-2tb").variant.options["Capacity"] == "2 TB"
    assert catalogue.variant("var-kestrel-pro-nvme-1tb").variant.price == Decimal("179")
    fan = catalogue.product("prd-northwind-fan-120")
    assert [v.available for v in fan.product.variants] == [False, True]
    assert fan.product.default.id == "var-northwind-fan-120-premium"
    assert catalogue.variant("var-northwind-psu-650").variant.options["Wattage"] == "650 W"


def test_headphones_follow_the_docs_example() -> None:
    """Black at 129 available, Silver unavailable, Express preselected at 13, Standard 5."""
    catalogue = bundled_catalogue("headphones")
    product = catalogue.product("prd-sony-wh-1000xm5")
    assert product.product.name == "Sony WH 1000XM5 Wireless Headphones"
    black, silver = product.product.variants
    assert (black.options, black.price, black.available) == ({"Color": "Black"}, 129, True)
    assert (silver.options, silver.available) == ({"Color": "Silver"}, False)
    options = {o.name: (o.price, o.selected) for o in product.merchant.shipping_options}
    assert options == {"Standard": (5, False), "Express": (13, True)}


def test_a_file_loads_from_any_path(tmp_path: Path) -> None:
    """A catalogue file outside the package loads the same way."""
    catalogue = load_catalogue(_write(tmp_path, _minimal()))
    assert catalogue.names == ("tiny",)
    assert catalogue.product("prd-tiny").product.option_groups == {"Colour": ("Red", "Blue")}


def test_ids_must_be_unique_across_files(tmp_path: Path) -> None:
    """Loading the same products twice is refused, so every id names one thing."""
    path = _write(tmp_path, _minimal())
    with pytest.raises(ValueError, match="duplicate"):
        load_catalogue(path, path)


_TINY_SHIPPING = {"id": "tiny-standard", "name": "Standard", "price": "4", "selected": True}
_PRODUCT = ("merchants", 0, "products", 0)


def _set(content: dict[str, Any], path: tuple[str | int, ...], value: object) -> None:
    node: Any = content
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ((*_PRODUCT, "default_variant"), "nope", "nope"),
        ((*_PRODUCT, "variants", 1, "options", "Colour"), "Red", "same options"),
        ((*_PRODUCT, "variants", 1, "options", "Size"), "Large", "Size"),
        (("merchants", 0, "ships_to"), ["SG", "MY"], "MY"),
        (("default_country",), "MY", "MY"),
        (
            ("merchants", 0, "shipping_options"),
            [_TINY_SHIPPING, {**_TINY_SHIPPING, "id": "tiny-express"}],
            "selected",
        ),
        (("merchants", 0, "shipping_options"), [], "shipping option"),
        (
            ("merchants", 0, "offer_codes"),
            [{"code": "X", "percent_off": "5", "amount_off": "2"}],
            "exactly one",
        ),
        (("merchants", 0, "colour"), "red", "colour"),
    ],
)
def test_a_malformed_file_is_refused(
    tmp_path: Path, path: tuple[str | int, ...], value: object, message: str
) -> None:
    """Typos and contradictions fail at load time, naming what is wrong."""
    content = _minimal()
    _set(content, path, value)
    with pytest.raises(ValueError, match=message):
        load_catalogue(_write(tmp_path, content))


# The engine

RETURN_URL = "https://example.com/return"
SG_ADDRESS = ShippingAddress(
    first_name="Site",
    last_name="Engineer",
    phone="+6561234567",
    address_line1="1 Example Road",
    city="Singapore",
    postal_code="000001",
    country="SG",
)
US_ADDRESS = ShippingAddress(
    first_name="Avery",
    last_name="Tan",
    phone="+14155550123",
    address_line1="123 Example Street",
    city="Example City",
    region="CA",
    postal_code="94105",
    country="US",
)


@pytest.fixture
def engine(clock: ManualClock) -> AgenticMockEngine:
    """An engine over the three bundled catalogues on the manual clock.

    Returns:
        The engine.
    """
    return AgenticMockEngine(bundled_catalogue(), clock=clock)


def _rejects(call: Callable[[], object], code: str) -> AgenticMockError:
    with pytest.raises(AgenticMockError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def _enrol(engine: AgenticMockEngine, owner: str = "objective-1") -> str:
    created = engine.create_enrollment(
        CreateExternalEnrollmentRequest(
            owner=ClientReferenceOwner(id=owner, email="operator@example.com"),
            presentation=Presentation(return_url=RETURN_URL),
        )
    )
    return created.id


def _active(engine: AgenticMockEngine, owner: str = "objective-1") -> str:
    enrollment_id = _enrol(engine, owner)
    engine.submit_card(
        enrollment_id, number="4622 9431 2313 7797", cvc="640", expiry="12/27", otp=TEST_OTP
    )
    return enrollment_id


def _quote(
    engine: AgenticMockEngine,
    variant_id: str = "var-northwind-nvme-1tb",
    *,
    quantity: int = 1,
    address: ShippingAddress | None = SG_ADDRESS,
    offer_code: str | None = None,
) -> Quote:
    return engine.create_quote(
        CreateItemsQuoteRequest(
            email="operator@example.com",
            items=[QuoteItem(variant_id=variant_id, quantity=quantity)],
            shipping_address=address,
            offer_code=offer_code,
        )
    )


def _checkout_request(quote_id: str, enrollment_id: str) -> CreateCheckoutRequest:
    return CreateCheckoutRequest(
        quote_id=quote_id,
        enrollment_id=enrollment_id,
        presentation=Presentation(return_url=RETURN_URL),
    )


def _search(
    engine: AgenticMockEngine,
    query: str,
    *,
    merchant_preference: MerchantPreference | None = None,
    filters: SearchFilters | None = None,
) -> list[str]:
    response = engine.search_products(
        ProductSearchRequest(query=query, merchant_preference=merchant_preference, filters=filters)
    )
    return [product.id for product in response.products]


# Enrollments


def test_an_external_enrollment_waits_for_the_hosted_card_entry(
    engine: AgenticMockEngine, clock: ManualClock
) -> None:
    """Create returns REQUIRES_ACTION with a redirect that expires; nothing is stored yet."""
    created = engine.create_enrollment(
        CreateExternalEnrollmentRequest(
            owner=ClientReferenceOwner(id="objective-1", email="operator@example.com"),
            presentation=Presentation(return_url=RETURN_URL),
        )
    )
    assert created.status == EnrollmentStatus.REQUIRES_ACTION
    assert created.source == "EXTERNAL"
    assert created.owner.id == "objective-1"
    assert created.next_action is not None
    assert created.next_action.url == f"http://reap-mock.local/hosted/enrollments/{created.id}"
    expires = clock() + timedelta(seconds=AgenticMockConfig().enrollment_ttl_s)
    assert created.next_action.expires_at == expires.isoformat().replace("+00:00", "Z")
    read = engine.get_enrollment(created.id)
    assert read.status == EnrollmentStatus.REQUIRES_ACTION
    assert read.payment_method is None
    assert read.next_action == created.next_action
    assert read.owner.type == "CLIENT_REFERENCE"


@pytest.mark.parametrize("number", list(TEST_CARDS))
def test_each_published_test_card_activates_the_enrollment(
    engine: AgenticMockEngine, number: str
) -> None:
    """The three test cards from the setup page, with their CVC, expiry and the OTP."""
    enrollment_id = _enrol(engine)
    cvc, expiry = TEST_CARDS[number]
    returned = engine.submit_card(
        enrollment_id, number=number, cvc=cvc, expiry=expiry, otp=TEST_OTP
    )
    assert returned == RETURN_URL
    read = engine.get_enrollment(enrollment_id)
    assert read.status == EnrollmentStatus.ACTIVE
    assert read.next_action is None
    assert read.payment_method is not None
    assert read.payment_method.last4 == number.replace(" ", "")[-4:]
    assert (read.payment_method.expiry_month, read.payment_method.expiry_year) == (12, 2027)


@pytest.mark.parametrize(
    ("number", "cvc", "otp", "message"),
    [
        ("4242 4242 4242 4242", "123", TEST_OTP, "test card"),
        ("4622 9431 2313 7797", "000", TEST_OTP, "test card"),
        ("4622 9431 2313 7797", "640", "000000", "one-time password"),
    ],
)
def test_a_wrong_card_or_password_leaves_the_enrollment_waiting(
    engine: AgenticMockEngine, number: str, cvc: str, otp: str, message: str
) -> None:
    """Only the published cards and the published OTP are accepted; the user can retry."""
    enrollment_id = _enrol(engine)
    with pytest.raises(HostedStepError, match=message):
        engine.submit_card(enrollment_id, number=number, cvc=cvc, expiry="12/27", otp=otp)
    assert engine.get_enrollment(enrollment_id).status == EnrollmentStatus.REQUIRES_ACTION


def test_an_unfinished_enrollment_expires(engine: AgenticMockEngine, clock: ManualClock) -> None:
    """Past nextAction.expiresAt the enrollment is EXPIRED and the page no longer accepts."""
    enrollment_id = _enrol(engine)
    clock.advance(seconds=AgenticMockConfig().enrollment_ttl_s)
    read = engine.get_enrollment(enrollment_id)
    assert read.status == EnrollmentStatus.EXPIRED
    assert read.next_action is None
    with pytest.raises(HostedStepError, match="expired"):
        engine.submit_card(
            enrollment_id, number="4622 9431 2313 7797", cvc="640", expiry="12/27", otp=TEST_OTP
        )


def test_card_sources_other_than_external_are_coming_soon(engine: AgenticMockEngine) -> None:
    """REAP_CARD and BIN_SPONSOR are "coming soon" in the OpenAPI, so they are rejected."""
    for request in (
        CreateReapCardEnrollmentRequest(card_id="0b8c3d1e-5f4a-4b6c-8d7e-9f0a1b2c3d4e"),
        CreateBinSponsorEnrollmentRequest(card_id="sponsor-1"),
    ):
        _rejects(lambda r=request: engine.create_enrollment(r), "AGENTIC_REQUEST_REJECTED")


def test_enrollments_list_by_owner_a_page_at_a_time(engine: AgenticMockEngine) -> None:
    """The list is scoped to one owner, pages with nextCursor, and refuses a bad cursor."""
    ids = [_enrol(engine, "objective-1") for _ in range(3)]
    _enrol(engine, "objective-2")
    first = engine.list_enrollments(owner_id="objective-1", limit=2)
    assert [e.id for e in first.items] == ids[:2]
    assert first.next_cursor is not None
    second = engine.list_enrollments(owner_id="objective-1", limit=2, cursor=first.next_cursor)
    assert [e.id for e in second.items] == ids[2:]
    assert second.next_cursor is None
    assert engine.list_enrollments(owner_id="objective-1", owner_type="REAP_USER").items == []
    error = _rejects(
        lambda: engine.list_enrollments(owner_id="objective-1", cursor="nonsense"),
        "VALIDATION_FAILED",
    )
    assert error.status == 422


def test_revoking_is_final_and_needs_an_active_enrollment(engine: AgenticMockEngine) -> None:
    """ACTIVE becomes REVOKED; anything else is ENROLLMENT_NOT_ACTIVE; unknown is not found."""
    waiting = _enrol(engine)
    error = _rejects(lambda: engine.revoke_enrollment(waiting), "ENROLLMENT_NOT_ACTIVE")
    assert (error.status, error.detail) == (409, {"reason": "CARD_NOT_CAPTURED"})
    active = _active(engine)
    revoked = engine.revoke_enrollment(active)
    assert revoked.status == EnrollmentStatus.REVOKED
    assert revoked.payment_method is not None
    again = _rejects(lambda: engine.revoke_enrollment(active), "ENROLLMENT_NOT_ACTIVE")
    assert again.detail is None
    _rejects(lambda: engine.revoke_enrollment("missing"), "ENROLLMENT_NOT_FOUND")
    _rejects(lambda: engine.get_enrollment("missing"), "ENROLLMENT_NOT_FOUND")


# Products


def test_the_docs_search_finds_the_docs_product(engine: AgenticMockEngine) -> None:
    """The guide's request returns the guide's shape: range 129 to 149, Black previewed."""
    response = engine.search_products(
        ProductSearchRequest(
            query="Sony WH 1000XM5 headphones",
            context=SearchContext(country="US", currency="USD"),
            filters=SearchFilters(
                price=PriceFilter(min=Decimal("100"), max=Decimal("200")),
                availability="AVAILABLE_ONLY",
            ),
            pagination=SearchPagination(limit=20),
        )
    )
    assert [p.id for p in response.products] == ["prd-sony-wh-1000xm5"]
    product = response.products[0]
    assert product.merchant.name == "Example Merchant"
    assert (product.price_range.min.amount, product.price_range.max.amount) == (129, 149)
    assert product.available is True
    assert product.preview_variant is not None
    assert (product.preview_variant.id, product.preview_variant.price.amount) == ("var_123", 129)
    assert response.pagination.returned_count == 1
    assert response.pagination.has_next_page is False
    assert response.warnings == []


def test_the_part_search_finds_every_drive_and_nothing_else(engine: AgenticMockEngine) -> None:
    """Both 1 TB drives, the 2 TB equivalent and the premium drive; no fan, no power supply."""
    found = _search(engine, "1TB NVMe M.2 2280 SSD")
    assert set(found) == {
        "prd-northwind-nvme-1tb",
        "prd-kestrel-nvme-1tb",
        "prd-northwind-nvme-2tb",
        "prd-kestrel-pro-nvme-1tb",
    }
    assert _search(engine, "120mm case fan") == ["prd-northwind-fan-120"]
    assert _search(engine, "GPU compute credits")[:2] == [
        "prd-kestrel-gpu-credits",
        "prd-northwind-gpu-credits",
    ]
    assert _search(engine, "flight to Tokyo") == []


def test_merchant_preference_only_and_prefer(engine: AgenticMockEngine) -> None:
    """ONLY keeps one merchant's products; PREFER ranks them first; unknown is not resolved."""
    query = "1TB NVMe M.2 2280 SSD"
    only = _search(
        engine,
        query,
        merchant_preference=MerchantPreference(mode="ONLY", merchant_name="kestrel parts"),
    )
    assert set(only) == {"prd-kestrel-nvme-1tb", "prd-kestrel-pro-nvme-1tb"}
    preferred = _search(
        engine,
        query,
        merchant_preference=MerchantPreference(mode="PREFER", merchant_name="Kestrel Parts"),
    )
    assert set(preferred[:2]) == set(only)
    assert len(preferred) == 4
    for mode in ("ONLY", "PREFER"):
        error = _rejects(
            lambda m=mode: _search(
                engine,
                query,
                merchant_preference=MerchantPreference(mode=m, merchant_name="Nobody"),
            ),
            "MERCHANT_NOT_RESOLVED",
        )
        assert (error.status, error.detail) == (400, None)


def test_price_and_availability_filters(engine: AgenticMockEngine) -> None:
    """A price band keeps products with a variant inside it; AVAILABLE_ONLY drops sold-out ones."""
    query = "1TB NVMe M.2 2280 SSD"
    cheap = _search(engine, query, filters=SearchFilters(price=PriceFilter(max=Decimal("70"))))
    assert set(cheap) == {"prd-northwind-nvme-1tb", "prd-kestrel-nvme-1tb"}
    engine.set_variant_available("var-northwind-fan-120-premium", available=False)
    fan = "120mm case fan"
    assert _search(engine, fan, filters=SearchFilters(availability="AVAILABLE_ONLY")) == []
    assert _search(engine, fan) == ["prd-northwind-fan-120"]


def test_search_pages_with_a_cursor(engine: AgenticMockEngine) -> None:
    """A page of one, then the next by cursor; a cursor that does not decode is refused."""
    query = "1TB NVMe M.2 2280 SSD"
    everything = _search(engine, query)
    first = engine.search_products(
        ProductSearchRequest(query=query, pagination=SearchPagination(limit=1))
    )
    assert first.pagination.has_next_page is True
    assert first.pagination.returned_count == 1
    second = engine.search_products(
        ProductSearchRequest(
            query=query, pagination=SearchPagination(limit=3, cursor=first.pagination.next_cursor)
        )
    )
    assert [p.id for p in first.products + second.products] == everything
    assert second.pagination.next_cursor is None
    _rejects(
        lambda: engine.search_products(
            ProductSearchRequest(query=query, pagination=SearchPagination(cursor="!!"))
        ),
        "VALIDATION_FAILED",
    )


def test_a_currency_the_mock_does_not_price_in_is_left_out_with_a_warning(
    engine: AgenticMockEngine,
) -> None:
    """The mock converts nothing: products in another currency are dropped and said so."""
    response = engine.search_products(
        ProductSearchRequest(query="GPU compute credits", context=SearchContext(currency="SGD"))
    )
    assert response.products == []
    assert len(response.warnings) == 1
    assert "SGD" in response.warnings[0]


def test_details_expand_options_and_report_unknown_ids(engine: AgenticMockEngine) -> None:
    """Option values carry availability; the default variant is purchasable; misses are errors."""
    response = engine.product_details(
        ProductDetailsRequest(product_ids=["prd-sony-wh-1000xm5", "prd-missing"])
    )
    (product,) = response.products
    assert product.merchant is not None
    assert product.merchant.name == "Example Merchant"
    (colour,) = product.options
    assert colour.name == "Color"
    assert [(v.label, v.available) for v in colour.values] == [("Black", True), ("Silver", False)]
    assert product.default_variant.id == "var_123"
    assert product.default_variant.requires_shipping is True
    assert product.media == []
    (error,) = response.errors
    assert (error.product_id, error.code) == ("prd-missing", "AGENTIC_RESOURCE_NOT_FOUND")


def _option_ids(engine: AgenticMockEngine, product_id: str) -> dict[str, str]:
    (product,) = engine.product_details(ProductDetailsRequest(product_ids=[product_id])).products
    return {
        f"{group.name}={value.label}": value.option_id
        for group in product.options
        for value in group.values
    }


def test_resolving_options_to_a_variant(engine: AgenticMockEngine) -> None:
    """One value per group resolves; a sold-out variant resolves with available false."""
    ids = _option_ids(engine, "prd-sony-wh-1000xm5")
    black = engine.resolve_variant(
        ResolveVariantRequest(product_id="prd-sony-wh-1000xm5", option_ids=[ids["Color=Black"]])
    )
    assert (black.id, black.price.amount, black.available) == ("var_123", 129, True)
    assert black.options is not None
    assert [(o.name, o.value) for o in black.options] == [("Color", "Black")]
    silver = engine.resolve_variant(
        ResolveVariantRequest(product_id="prd-sony-wh-1000xm5", option_ids=[ids["Color=Silver"]])
    )
    assert (silver.id, silver.available) == ("var_124", False)


def test_options_that_name_no_single_variant_fail_to_resolve(engine: AgenticMockEngine) -> None:
    """Unknown ids, two values of one group, or a group left out: VARIANT_RESOLUTION_FAILED."""
    product = "prd-northwind-nvme-1tb"
    ids = _option_ids(engine, product)
    for option_ids in (
        ["opt-unknown"],
        [ids["Capacity=1 TB"], ids["Interface=NVMe"]],
        list(_option_ids(engine, "prd-sony-wh-1000xm5").values())[:1],
    ):
        error = _rejects(
            lambda o=option_ids: engine.resolve_variant(
                ResolveVariantRequest(product_id=product, option_ids=o)
            ),
            "VARIANT_RESOLUTION_FAILED",
        )
        assert error.status == 400
    sony = _option_ids(engine, "prd-sony-wh-1000xm5")
    _rejects(
        lambda: engine.resolve_variant(
            ResolveVariantRequest(product_id="prd-sony-wh-1000xm5", option_ids=list(sony.values()))
        ),
        "VARIANT_RESOLUTION_FAILED",
    )
    _rejects(
        lambda: engine.resolve_variant(
            ResolveVariantRequest(product_id="prd-missing", option_ids=["x"])
        ),
        "AGENTIC_RESOURCE_NOT_FOUND",
    )


# Quotes


def test_a_part_quote_lands_items_shipping_and_included_tax(
    engine: AgenticMockEngine, clock: ManualClock
) -> None:
    """Singapore includes tax in prices: final is items plus the preselected Standard."""
    quote = _quote(engine)
    breakdown = quote.amount_breakdown
    assert breakdown.items_subtotal.amount == Decimal("69")
    assert breakdown.shipping is not None
    assert breakdown.shipping.amount == Decimal("8")
    assert breakdown.tax is not None
    assert breakdown.tax.included_in_prices is True
    assert breakdown.tax.amount.amount == Decimal("5.70")
    assert breakdown.discounts == []
    assert breakdown.additional_charges == []
    assert breakdown.final_amount.amount == Decimal("77")
    assert breakdown.final_amount.currency == "USD"
    assert [(o.id, o.selected) for o in quote.shipping_options] == [
        ("northwind-standard", True),
        ("northwind-express", False),
    ]
    assert quote.shipping_options[0].details is not None
    assert quote.expires_at == (clock() + timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    assert engine.get_quote(quote.id) == quote


@pytest.mark.parametrize(
    ("variant_id", "final"),
    [
        ("var-kestrel-nvme-1tb", "79"),
        ("var-northwind-nvme-2tb", "107"),
        ("var-kestrel-pro-nvme-1tb", "194"),
        ("var-northwind-fan-120-premium", "137"),
        ("var-northwind-psu-650", "97"),
    ],
)
def test_the_parts_land_where_the_grant_needs_them(
    engine: AgenticMockEngine, variant_id: str, final: str
) -> None:
    """The landed prices the catalogue's header promises for the gate's three outcomes."""
    assert _quote(engine, variant_id).amount_breakdown.final_amount.amount == Decimal(final)


def test_a_credit_quote_has_no_shipping_and_adds_the_merchants_fee(
    engine: AgenticMockEngine,
) -> None:
    """Credits need no address; the dearer list price lands cheaper after the fees."""
    northwind = _quote(engine, "var-northwind-gpu-credits-100", address=None)
    kestrel = _quote(engine, "var-kestrel-gpu-credits-100", address=None)
    assert northwind.shipping_options == []
    assert northwind.amount_breakdown.shipping is None
    assert northwind.amount_breakdown.additional_charges is not None
    assert [(c.name, c.amount.amount) for c in northwind.amount_breakdown.additional_charges] == [
        ("Service fee", Decimal("2.50"))
    ]
    assert northwind.amount_breakdown.final_amount.amount == Decimal("102.50")
    assert kestrel.amount_breakdown.final_amount.amount == Decimal("103.90")


def test_a_us_quote_adds_tax_and_takes_the_offer_off(engine: AgenticMockEngine) -> None:
    """US tax is not included in prices; SAVE10 takes ten percent off the items first."""
    quote = _quote(engine, "var_123", address=US_ADDRESS, offer_code="SAVE10")
    breakdown = quote.amount_breakdown
    assert breakdown.items_subtotal.amount == Decimal("129")
    assert breakdown.discounts is not None
    assert [(d.name, d.amount.amount) for d in breakdown.discounts] == [
        ("Offer code SAVE10", Decimal("12.90"))
    ]
    assert breakdown.tax is not None
    assert (breakdown.tax.amount.amount, breakdown.tax.included_in_prices) == (
        Decimal("9.29"),
        False,
    )
    assert breakdown.shipping is not None
    assert breakdown.shipping.amount == Decimal("13")
    assert breakdown.final_amount.amount == Decimal("138.39")


def test_quantity_multiplies_the_items(engine: AgenticMockEngine) -> None:
    """Two drives: twice the items, shipping once."""
    quote = _quote(engine, quantity=2)
    assert quote.amount_breakdown.items_subtotal.amount == Decimal("138")
    assert quote.amount_breakdown.final_amount.amount == Decimal("146")


def test_shipping_address_is_required_when_an_item_ships(engine: AgenticMockEngine) -> None:
    """A drive without an address fails validation; a credit without one does not."""
    error = _rejects(lambda: _quote(engine, address=None), "VALIDATION_FAILED")
    assert error.status == 422
    assert error.detail is not None
    assert error.detail["errors"][0]["path"] == "shippingAddress"


@pytest.mark.parametrize(
    ("address", "reason", "message"),
    [
        (
            SG_ADDRESS.model_copy(update={"country": "MY"}),
            "ITEMS_UNSHIPPABLE",
            "Your cart has been updated and the items you added can't be shipped to your "
            "address. Remove the items to complete your order.",
        ),
        (
            US_ADDRESS.model_copy(update={"region": None}),
            "STATE_OR_PROVINCE_REQUIRED",
            "Select a state / province",
        ),
        (
            US_ADDRESS.model_copy(update={"phone": "+6561234567"}),
            "INVALID_PHONE",
            "Phone is invalid",
        ),
    ],
)
def test_an_address_the_merchant_cannot_fulfil(
    engine: AgenticMockEngine, address: ShippingAddress, reason: str, message: str
) -> None:
    """QUOTE_UNFULFILLABLE carries the OpenAPI's reason and its fixed message."""
    error = _rejects(lambda: _quote(engine, address=address), "QUOTE_UNFULFILLABLE")
    assert (error.status, error.detail) == (400, {"reason": reason, "message": message})


def _tiny(tmp_path: Path, **merchant: object) -> Catalogue:
    content = _minimal()
    content["countries"]["SG"]["address_line2_required"] = True
    content["merchants"][0].update(merchant)
    return load_catalogue(_write(tmp_path, content))


def test_address_line_two_and_card_payment_follow_the_catalogue(
    tmp_path: Path, clock: ManualClock
) -> None:
    """A country can require line 2; a merchant can refuse cards."""
    engine = AgenticMockEngine(_tiny(tmp_path), clock=clock)
    error = _rejects(lambda: _quote(engine, "var-tiny-red"), "QUOTE_UNFULFILLABLE")
    assert error.detail == {
        "reason": "ADDRESS_LINE_2_REQUIRED",
        "message": "Address line 2 is required.",
    }
    with_line2 = SG_ADDRESS.model_copy(update={"address_line2": "#01-01"})
    assert (
        _quote(engine, "var-tiny-red", address=with_line2).amount_breakdown.final_amount.amount
        == 14
    )
    refusing = AgenticMockEngine(_tiny(tmp_path, card_payment=False), clock=clock)
    error = _rejects(
        lambda: _quote(refusing, "var-tiny-red", address=with_line2), "CARD_PAYMENT_UNAVAILABLE"
    )
    assert (error.status, error.detail) == (400, None)


def test_quotes_refuse_what_cannot_be_bought(engine: AgenticMockEngine) -> None:
    """Sold out, unknown, from two merchants, or under a bad offer code."""
    _rejects(lambda: _quote(engine, "var_124", address=US_ADDRESS), "VARIANT_UNAVAILABLE")
    engine.set_variant_available("var_124", available=True)
    assert (
        _quote(engine, "var_124", address=US_ADDRESS).amount_breakdown.items_subtotal.amount == 149
    )
    engine.set_variant_available("var-northwind-psu-650", available=False)
    error = _rejects(lambda: _quote(engine, "var-northwind-psu-650"), "VARIANT_UNAVAILABLE")
    assert error.status == 409
    _rejects(lambda: _quote(engine, "prd-northwind-nvme-1tb"), "AGENTIC_REQUEST_REJECTED")
    _rejects(
        lambda: engine.create_quote(
            CreateItemsQuoteRequest(
                email="operator@example.com",
                items=[
                    QuoteItem(variant_id="var-northwind-nvme-1tb", quantity=1),
                    QuoteItem(variant_id="var-kestrel-nvme-1tb", quantity=1),
                ],
                shipping_address=SG_ADDRESS,
            )
        ),
        "AGENTIC_REQUEST_REJECTED",
    )
    _rejects(
        lambda: _quote(engine, "var_123", address=US_ADDRESS, offer_code="NOPE"),
        "OFFER_CODE_INVALID",
    )
    _rejects(
        lambda: _quote(engine, "var_123", address=US_ADDRESS, offer_code="SUMMER25"),
        "OFFER_CODE_EXPIRED",
    )


def _external(url: str, domain: str = "merchant.example") -> CreateExternalCheckoutQuoteRequest:
    return CreateExternalCheckoutQuoteRequest(
        email="avery.tan@reap.hk",
        external_checkout=ExternalCheckout(merchant_domain=domain, checkout_url=url),
        shipping_address=US_ADDRESS,
    )


def test_a_checkout_url_quote_reads_the_cart_from_the_url(engine: AgenticMockEngine) -> None:
    """The docs' URL shape on an allowlisted domain prices the cart it names."""
    quote = engine.create_quote(
        _external(
            "https://merchant.example/cart/var_123:2?attributes[partner_click_id]=example-123"
        )
    )
    assert quote.amount_breakdown.items_subtotal.amount == Decimal("258")


@pytest.mark.parametrize(
    ("url", "domain", "reason"),
    [
        (
            "https://merchant.example/cart/var_123:1",
            "unknown.example",
            "MERCHANT_CONTEXT_UNVERIFIED",
        ),
        (
            "https://kestrel-parts.example/cart/var-kestrel-nvme-1tb:1",
            "kestrel-parts.example",
            "MERCHANT_CONTEXT_UNVERIFIED",
        ),
        ("https://elsewhere.example/cart/var_123:1", "merchant.example", "INVALID"),
        ("http://merchant.example/cart/var_123:1", "merchant.example", "INVALID"),
        ("https://merchant.example/products/var_123", "merchant.example", "INVALID"),
        ("https://merchant.example/cart/var_123:0", "merchant.example", "INVALID"),
        ("https://merchant.example/cart/var-northwind-nvme-1tb:1", "merchant.example", "NOT_FOUND"),
    ],
)
def test_a_checkout_url_that_cannot_be_used(
    engine: AgenticMockEngine, url: str, domain: str, reason: str
) -> None:
    """CHECKOUT_URL_INVALID with the OpenAPI's reason."""
    error = _rejects(lambda: engine.create_quote(_external(url, domain)), "CHECKOUT_URL_INVALID")
    assert (error.status, error.detail) == (400, {"reason": reason})


def test_a_quote_is_read_back_and_unknown_ids_are_not_found(engine: AgenticMockEngine) -> None:
    """GET returns the quote as last priced."""
    _rejects(lambda: engine.get_quote("missing"), "QUOTE_NOT_FOUND")


def test_choosing_shipping_reprices_the_quote(engine: AgenticMockEngine) -> None:
    """Express replaces Standard in the breakdown and in the selection."""
    quote = _quote(engine)
    express = engine.select_shipping_option(
        quote.id, SelectShippingOptionRequest(shipping_option_id="northwind-express")
    )
    assert express.amount_breakdown.final_amount.amount == Decimal("87")
    assert [(o.id, o.selected) for o in express.shipping_options] == [
        ("northwind-standard", False),
        ("northwind-express", True),
    ]
    assert express.expires_at == quote.expires_at
    assert engine.get_quote(quote.id) == express
    error = _rejects(
        lambda: engine.select_shipping_option(
            quote.id, SelectShippingOptionRequest(shipping_option_id="kestrel-express")
        ),
        "SHIPPING_OPTION_INVALID",
    )
    assert (error.status, error.detail) == (400, None)
    _rejects(
        lambda: engine.select_shipping_option(
            "missing", SelectShippingOptionRequest(shipping_option_id="northwind-express")
        ),
        "QUOTE_NOT_FOUND",
    )


def test_a_quote_cannot_be_repriced_once_expired_used_or_stale(
    engine: AgenticMockEngine, clock: ManualClock
) -> None:
    """Expired, checked out, or holding a sold-out item: each has its own code."""
    choose = SelectShippingOptionRequest(shipping_option_id="northwind-express")
    expired = _quote(engine)
    clock.advance(seconds=120)
    _rejects(lambda: engine.select_shipping_option(expired.id, choose), "QUOTE_EXPIRED")
    used = _quote(engine)
    engine.create_checkout(_checkout_request(used.id, _active(engine)))
    _rejects(lambda: engine.select_shipping_option(used.id, choose), "QUOTE_NOT_MUTABLE")
    stale = _quote(engine)
    engine.set_variant_available("var-northwind-nvme-1tb", available=False)
    error = _rejects(
        lambda: engine.select_shipping_option(stale.id, choose), "QUOTE_REPLACEMENT_REQUIRED"
    )
    assert error.status == 409


def test_an_offer_that_lapses_fails_the_reprice(tmp_path: Path, clock: ManualClock) -> None:
    """Re-pricing re-checks the offer code against the clock."""
    lapsing = [
        {
            "code": "SOON",
            "amount_off": "1",
            "expires_at": (clock() + timedelta(seconds=60)).isoformat(),
        }
    ]
    content = _minimal()
    content["merchants"][0]["offer_codes"] = lapsing
    content["merchants"][0]["shipping_options"].append(
        {"id": "tiny-express", "name": "Express", "price": "9"}
    )
    engine = AgenticMockEngine(load_catalogue(_write(tmp_path, content)), clock=clock)
    quote = _quote(engine, "var-tiny-red", offer_code="SOON")
    assert quote.amount_breakdown.final_amount.amount == Decimal("13")
    clock.advance(seconds=60)
    _rejects(
        lambda: engine.select_shipping_option(
            quote.id, SelectShippingOptionRequest(shipping_option_id="tiny-express")
        ),
        "OFFER_CODE_EXPIRED",
    )


# Checkouts


def test_a_checkout_without_the_header_waits_for_approval_then_completes(
    engine: AgenticMockEngine, clock: ManualClock
) -> None:
    """REQUIRES_ACTION, approved on the hosted page, PROCESSING, then COMPLETED."""
    quote = _quote(engine)
    enrollment_id = _active(engine)
    created = engine.create_checkout(_checkout_request(quote.id, enrollment_id))
    assert created.status == CheckoutStatus.REQUIRES_ACTION
    assert (created.quote_id, created.enrollment_id) == (quote.id, enrollment_id)
    assert created.amount == quote.amount_breakdown.final_amount
    assert created.next_action is not None
    assert created.next_action.url == f"http://reap-mock.local/hosted/checkouts/{created.id}"
    waiting = engine.get_checkout(created.id)
    assert (waiting.status, waiting.order_id, waiting.final_amount) == (
        CheckoutStatus.REQUIRES_ACTION,
        None,
        None,
    )
    assert waiting.next_action == created.next_action
    assert engine.approve_checkout(created.id) == RETURN_URL
    processing = engine.get_checkout(created.id)
    assert (processing.status, processing.next_action) == (CheckoutStatus.PROCESSING, None)
    clock.advance(seconds=AgenticMockConfig().processing_s)
    done = engine.get_checkout(created.id)
    assert done.status == CheckoutStatus.COMPLETED
    assert done.order_id is not None
    assert done.order_id.startswith("MOCK-ORDER-")
    assert done.final_amount == quote.amount_breakdown.final_amount
    assert done.next_action is None
    clock.advance(days=1)
    assert engine.get_checkout(created.id) == done


def test_an_unapproved_checkout_expires(engine: AgenticMockEngine, clock: ManualClock) -> None:
    """Past nextAction.expiresAt the checkout is EXPIRED, final, and cannot be approved."""
    created = engine.create_checkout(_checkout_request(_quote(engine).id, _active(engine)))
    clock.advance(seconds=AgenticMockConfig().approval_ttl_s)
    assert engine.get_checkout(created.id).status == CheckoutStatus.EXPIRED
    with pytest.raises(HostedStepError, match="EXPIRED"):
        engine.approve_checkout(created.id)


def test_the_sandbox_header_completes_at_once(engine: AgenticMockEngine) -> None:
    """X-Simulate-Checkout: COMPLETED returns the checkout COMPLETED (changelog, 24 Sept)."""
    quote = _quote(engine)
    created = engine.create_checkout(
        _checkout_request(quote.id, _active(engine)), simulate="COMPLETED"
    )
    assert (created.status, created.next_action) == (CheckoutStatus.COMPLETED, None)
    assert created.amount == quote.amount_breakdown.final_amount
    read = engine.get_checkout(created.id)
    assert read.status == CheckoutStatus.COMPLETED
    assert read.order_id is not None
    assert read.final_amount == quote.amount_breakdown.final_amount


def test_the_header_can_answer_as_the_guides_example_does(
    engine: AgenticMockEngine,
) -> None:
    """Configured the other way, create says REQUIRES_ACTION and the first read COMPLETED."""
    engine.config = replace(engine.config, simulated_create=SimulatedCreate.REQUIRES_ACTION)
    created = engine.create_checkout(
        _checkout_request(_quote(engine).id, _active(engine)), simulate="COMPLETED"
    )
    assert created.status == CheckoutStatus.REQUIRES_ACTION
    assert created.next_action is not None
    assert engine.get_checkout(created.id).status == CheckoutStatus.COMPLETED


def test_production_rejects_the_header(engine: AgenticMockEngine) -> None:
    """X-Simulate-Checkout: "This header is rejected in production"."""
    engine.config = replace(engine.config, environment=Environment.PRODUCTION)
    error = _rejects(
        lambda: engine.create_checkout(
            _checkout_request(_quote(engine).id, _active(engine)), simulate="COMPLETED"
        ),
        "AGENTIC_REQUEST_REJECTED",
    )
    assert error.status == 400


def test_a_failed_order_and_a_changed_final_amount(
    engine: AgenticMockEngine, clock: ManualClock
) -> None:
    """Test hooks: the merchant order fails, or the charge differs from the quote."""
    failing = _quote(engine)
    engine.fail_order(failing.id)
    created = engine.create_checkout(_checkout_request(failing.id, _active(engine)))
    engine.approve_checkout(created.id)
    clock.advance(seconds=AgenticMockConfig().processing_s)
    failed = engine.get_checkout(created.id)
    assert (failed.status, failed.order_id, failed.final_amount) == (
        CheckoutStatus.FAILED,
        None,
        None,
    )

    differing = _quote(engine)
    engine.override_final_amount(differing.id, Decimal("80.15"))
    done = engine.create_checkout(
        _checkout_request(differing.id, _active(engine)), simulate="COMPLETED"
    )
    read = engine.get_checkout(done.id)
    assert read.final_amount is not None
    assert read.final_amount.amount == Decimal("80.15")
    assert done.amount == differing.amount_breakdown.final_amount


def test_an_item_that_sells_out_before_the_order_fails_it(
    engine: AgenticMockEngine, clock: ManualClock
) -> None:
    """The order is placed after approval; a sold-out item makes it FAILED."""
    created = engine.create_checkout(_checkout_request(_quote(engine).id, _active(engine)))
    engine.approve_checkout(created.id)
    engine.set_variant_available("var-northwind-nvme-1tb", available=False)
    clock.advance(seconds=AgenticMockConfig().processing_s)
    assert engine.get_checkout(created.id).status == CheckoutStatus.FAILED


def test_checkout_refusals(engine: AgenticMockEngine, clock: ManualClock) -> None:
    """Unknown quote or enrollment, an enrollment not ACTIVE, an expired or used quote."""
    active = _active(engine)
    quote = _quote(engine)
    other = "0b8c3d1e-5f4a-4b6c-8d7e-9f0a1b2c3d4e"
    _rejects(lambda: engine.create_checkout(_checkout_request(other, active)), "QUOTE_NOT_FOUND")
    _rejects(
        lambda: engine.create_checkout(_checkout_request(quote.id, other)), "ENROLLMENT_NOT_FOUND"
    )
    waiting = _enrol(engine)
    error = _rejects(
        lambda: engine.create_checkout(_checkout_request(quote.id, waiting)),
        "ENROLLMENT_NOT_ACTIVE",
    )
    assert (error.status, error.detail) == (409, {"reason": "CARD_NOT_CAPTURED"})
    engine.create_checkout(_checkout_request(quote.id, active))
    error = _rejects(
        lambda: engine.create_checkout(_checkout_request(quote.id, active)),
        "AGENTIC_REQUEST_REJECTED",
    )
    assert error.detail == {"field": "quoteId"}
    stale = _quote(engine)
    clock.advance(seconds=120)
    error = _rejects(
        lambda: engine.create_checkout(_checkout_request(stale.id, active)), "QUOTE_EXPIRED"
    )
    assert error.status == 409
    _rejects(lambda: engine.get_checkout("missing"), "CHECKOUT_NOT_FOUND")


def test_only_a_checkout_awaiting_approval_can_be_approved(engine: AgenticMockEngine) -> None:
    """A completed checkout's page cannot approve it again; an unknown one is not found."""
    created = engine.create_checkout(
        _checkout_request(_quote(engine).id, _active(engine)), simulate="COMPLETED"
    )
    with pytest.raises(HostedStepError, match="COMPLETED"):
        engine.approve_checkout(created.id)
    with pytest.raises(HostedStepError, match="no checkout"):
        engine.approve_checkout("missing")


# Test hooks


def test_an_injected_error_answers_the_next_calls_then_stops(engine: AgenticMockEngine) -> None:
    """The code's documented status, a Retry-After for the temporary 503s, times honoured."""
    engine.inject_error(
        Operation.CREATE_QUOTE, AgenticErrorCode.QUOTE_TEMPORARILY_UNAVAILABLE, times=2
    )
    for _ in range(2):
        error = _rejects(lambda: _quote(engine), "QUOTE_TEMPORARILY_UNAVAILABLE")
        assert (error.status, error.retry_after_s) == (503, 1.0)
    assert _quote(engine).amount_breakdown.final_amount.amount == 77
    engine.inject_error(
        Operation.SEARCH,
        AgenticErrorCode.AGENTIC_SERVICE_UNAVAILABLE,
        detail={"note": "injected"},
    )
    error = _rejects(lambda: _search(engine, "fan"), "AGENTIC_SERVICE_UNAVAILABLE")
    assert (error.status, error.detail, error.retry_after_s) == (503, {"note": "injected"}, None)


def test_only_a_documented_code_can_be_injected(engine: AgenticMockEngine) -> None:
    """Each operation accepts only the codes its OpenAPI entry and the errors page list."""
    with pytest.raises(ValueError, match="QUOTE_EXPIRED"):
        engine.inject_error(Operation.SEARCH, AgenticErrorCode.QUOTE_EXPIRED)
    with pytest.raises(ValueError, match="times"):
        engine.inject_error(Operation.SEARCH, AgenticErrorCode.RATE_LIMIT_EXCEEDED, times=0)


def test_agentic_payments_can_be_switched_off(engine: AgenticMockEngine) -> None:
    """403 AGENTIC_PAYMENTS_NOT_ENABLED with the OpenAPI's message, on every operation."""
    engine.enabled = False
    error = _rejects(lambda: _search(engine, "fan"), "AGENTIC_PAYMENTS_NOT_ENABLED")
    assert (error.status, error.message) == (
        403,
        "Agentic Payments is not enabled for this project",
    )
    _rejects(lambda: engine.get_checkout("x"), "AGENTIC_PAYMENTS_NOT_ENABLED")


def test_drops_and_holds_are_taken_once(engine: AgenticMockEngine) -> None:
    """The server asks once per request; a hook fires for exactly the calls it was set for."""
    assert engine.take_drop(Operation.CREATE_CHECKOUT) is False
    engine.drop_next_response(Operation.CREATE_CHECKOUT)
    assert engine.take_drop(Operation.CREATE_CHECKOUT) is True
    assert engine.take_drop(Operation.CREATE_CHECKOUT) is False
    assert engine.take_hold(Operation.CREATE_QUOTE) is None
    gate = engine.hold_next(Operation.CREATE_QUOTE)
    assert engine.take_hold(Operation.CREATE_QUOTE) is gate
    assert engine.take_hold(Operation.CREATE_QUOTE) is None


def test_a_hook_names_something_that_exists(engine: AgenticMockEngine) -> None:
    """Hooks refuse unknown variants and quotes rather than silently doing nothing."""
    with pytest.raises(KeyError):
        engine.set_variant_available("var-missing", available=False)
    with pytest.raises(KeyError):
        engine.fail_order("missing")
    with pytest.raises(KeyError):
        engine.override_final_amount("missing", Decimal(1))


def test_expiry_is_never_later_than_the_expires_at_shown(clock: ManualClock) -> None:
    """With a sub-second clock, a quote is refused from the second its expiresAt names."""
    clock.advance(seconds=0.5)
    engine = AgenticMockEngine(bundled_catalogue(), clock=clock)
    quote = _quote(engine)
    clock.advance(seconds=119.5)
    assert clock().isoformat().replace("+00:00", "Z") == quote.expires_at
    _rejects(
        lambda: engine.create_checkout(_checkout_request(quote.id, _active(engine))),
        "QUOTE_EXPIRED",
    )


@pytest.mark.parametrize(
    ("query", "product"),
    [
        ("120 mm fan", "prd-northwind-fan-120"),
        ("1 TB NVMe", "prd-northwind-nvme-1tb"),
        ("wh-1000xm5", "prd-sony-wh-1000xm5"),
        ("650W power supply", "prd-northwind-psu-650"),
    ],
)
def test_search_bridges_spacing_and_hyphens(
    engine: AgenticMockEngine, query: str, product: str
) -> None:
    """A query that splits or joins a name's words differently still finds it."""
    assert product in _search(engine, query)


def test_a_completed_checkout_is_updated_when_its_order_settled(
    engine: AgenticMockEngine, clock: ManualClock
) -> None:
    """The settled checkout's updatedAt is approval plus processing, not its next read."""
    created = engine.create_checkout(_checkout_request(_quote(engine).id, _active(engine)))
    engine.approve_checkout(created.id)
    settled = clock() + timedelta(seconds=AgenticMockConfig().processing_s)
    clock.advance(minutes=5)
    done = engine.get_checkout(created.id)
    assert done.status == CheckoutStatus.COMPLETED
    assert done.updated_at == settled.isoformat().replace("+00:00", "Z")

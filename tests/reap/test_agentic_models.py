"""Contract tests: Reap's own agentic example JSON through the wire models.

Every example in ``tests/reap/agentic_examples.py`` is pasted verbatim from Reap's docs with
its URL. Each one parses and serialises back to the same keys and values, so the models carry
exactly the names Reap uses on the wire, no more and no fewer.
"""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from tests.reap import agentic_examples as ex
from youreapyousow.reap.models import (
    BinSponsorEnrollmentCreated,
    Checkout,
    CheckoutCreated,
    CheckoutStatus,
    CreateCheckoutRequest,
    CreateEnrollmentRequest,
    CreateExternalCheckoutQuoteRequest,
    CreateItemsQuoteRequest,
    CreateQuoteRequest,
    Enrollment,
    EnrollmentCreated,
    EnrollmentStatus,
    ErrorResponse,
    ExternalEnrollmentCreated,
    Page,
    Presentation,
    ProductDetailsRequest,
    ProductDetailsResponse,
    ProductSearchRequest,
    ProductSearchResponse,
    Quote,
    QuoteItem,
    ReapCardEnrollmentCreated,
    ResolveVariantRequest,
    SelectShippingOptionRequest,
    ShippingAddress,
    Variant,
    Wire,
)

QUOTE_ID = "3f1c2b9a-7d4e-4c1a-9b2f-0e6d5a4c3b21"
ENROLLMENT_ID = "8a6e0f3d-2c1b-4e9a-8f7d-6c5b4a3e2d10"
CARD_ID = "5b4a3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d"

_ENROLLMENT_CREATED = TypeAdapter[EnrollmentCreated](EnrollmentCreated)
_CREATE_ENROLLMENT = TypeAdapter[CreateEnrollmentRequest](CreateEnrollmentRequest)
_CREATE_QUOTE = TypeAdapter[CreateQuoteRequest](CreateQuoteRequest)


def _substitute(example: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Replace Reap's placeholders where the OpenAPI demands a UUID.

    Args:
        example: A decoded example.
        values: Placeholder to replacement, for top-level string fields.

    Returns:
        A copy with those values replaced; keys are untouched.
    """
    return {
        key: values.get(value, value) if isinstance(value, str) else value
        for key, value in example.items()
    }


def _round_trip(model: type[BaseModel], text: str) -> None:
    raw = ex.load(text)
    parsed = model.model_validate(raw)
    assert isinstance(parsed, Wire)
    assert parsed.to_wire() == raw


@pytest.mark.parametrize(
    ("model", "text"),
    [
        (Enrollment, ex.ENROLLMENT_RESPONSE),
        (Enrollment, ex.ENROLLMENT_REVOKED_RESPONSE),
        (Page[Enrollment], ex.ENROLLMENT_LIST_RESPONSE),
        (ProductSearchRequest, ex.SEARCH_REQUEST),
        (ProductSearchResponse, ex.SEARCH_RESPONSE),
        (ProductDetailsRequest, ex.DETAILS_REQUEST),
        (ProductDetailsResponse, ex.DETAILS_RESPONSE),
        (ResolveVariantRequest, ex.VARIANT_REQUEST),
        (Variant, ex.VARIANT_RESPONSE),
        (Quote, ex.QUOTE_RESPONSE),
        (SelectShippingOptionRequest, ex.SHIPPING_OPTION_REQUEST),
        (Quote, ex.SHIPPING_OPTION_RESPONSE),
        (CheckoutCreated, ex.CHECKOUT_CREATED_RESPONSE),
        (Checkout, ex.CHECKOUT_RESPONSE),
        (ErrorResponse, ex.ERROR_RESPONSE),
        (ErrorResponse, ex.VALIDATION_ERROR_RESPONSE),
        (ErrorResponse, ex.RATE_LIMIT_RESPONSE),
    ],
)
def test_docs_examples_round_trip_key_for_key(model: type[BaseModel], text: str) -> None:
    """Each example parses and serialises back to the same keys and values."""
    _round_trip(model, text)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (ex.ENROLLMENT_REAP_CARD_RESPONSE, ReapCardEnrollmentCreated),
        (ex.ENROLLMENT_BIN_SPONSOR_RESPONSE, BinSponsorEnrollmentCreated),
        (ex.ENROLLMENT_EXTERNAL_RESPONSE, ExternalEnrollmentCreated),
    ],
)
def test_enrollment_create_response_is_chosen_by_source(text: str, kind: type[Wire]) -> None:
    """The create response's shape depends on ``source``, as the OpenAPI's oneOf says."""
    raw = ex.load(text)
    created = _ENROLLMENT_CREATED.validate_python(raw)
    assert isinstance(created, kind)
    assert created.status is EnrollmentStatus.REQUIRES_ACTION
    assert created.to_wire() == raw


def test_external_enrollment_request_round_trips() -> None:
    """The external-card request needs no substitution: nothing in it is a UUID."""
    raw = ex.load(ex.ENROLLMENT_EXTERNAL_REQUEST)
    assert _CREATE_ENROLLMENT.validate_python(raw).to_wire() == raw


@pytest.mark.parametrize(
    "text", [ex.ENROLLMENT_REAP_CARD_REQUEST, ex.ENROLLMENT_BIN_SPONSOR_REQUEST]
)
def test_card_enrollment_requests_round_trip(text: str) -> None:
    """Reap-card and BIN-sponsor requests round-trip once the card id is a real one."""
    raw = _substitute(ex.load(text), {"<card-id>": CARD_ID})
    assert _CREATE_ENROLLMENT.validate_python(raw).to_wire() == raw


def test_reap_card_enrollment_needs_a_uuid_card_id() -> None:
    """``cardId`` is ``format: uuid`` for a Reap card, so the docs' placeholder is refused."""
    with pytest.raises(ValidationError):
        _CREATE_ENROLLMENT.validate_python(ex.load(ex.ENROLLMENT_REAP_CARD_REQUEST))


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (ex.QUOTE_ITEMS_REQUEST, CreateItemsQuoteRequest),
        (ex.QUOTE_EXTERNAL_REQUEST, CreateExternalCheckoutQuoteRequest),
    ],
)
def test_quote_requests_round_trip_as_their_own_form(text: str, kind: type[Wire]) -> None:
    """A quote is either items or an external checkout URL, told apart by which is present."""
    raw = ex.load(text)
    request = _CREATE_QUOTE.validate_python(raw)
    assert isinstance(request, kind)
    assert request.to_wire() == raw


def test_quote_request_refuses_both_forms_at_once() -> None:
    """The OpenAPI forbids sending ``items`` and ``externalCheckout`` together."""
    both = ex.load(ex.QUOTE_EXTERNAL_REQUEST) | {"items": [{"variantId": "v", "quantity": 1}]}
    with pytest.raises(ValidationError):
        _CREATE_QUOTE.validate_python(both)


def test_checkout_request_round_trips_with_uuid_ids() -> None:
    """The checkout request round-trips once its ids are the UUIDs the schema demands."""
    raw = _substitute(
        ex.load(ex.CHECKOUT_REQUEST), {"<quote-id>": QUOTE_ID, "<enrollment-id>": ENROLLMENT_ID}
    )
    assert CreateCheckoutRequest.model_validate(raw).to_wire() == raw


def test_checkout_request_refuses_a_quote_id_that_is_not_a_uuid() -> None:
    """Reap answers 422 for a non-UUID ``quoteId`` (its changelog); refuse it first."""
    with pytest.raises(ValidationError):
        CreateCheckoutRequest.model_validate(ex.load(ex.CHECKOUT_REQUEST))


def test_money_is_decimal_in_python() -> None:
    """Amounts parse to ``Decimal``; the gate decides on ``finalAmount`` as sent."""
    quote = Quote.model_validate(ex.load(ex.QUOTE_RESPONSE))
    final = quote.amount_breakdown.final_amount
    assert (final.amount, final.currency) == (Decimal(142), "USD")
    assert [option.selected for option in quote.shipping_options] == [False, True]


def test_parts_the_guide_leaves_out_stay_out() -> None:
    """The shipping-option example has no discounts; none are invented on the way back."""
    quote = Quote.model_validate(ex.load(ex.SHIPPING_OPTION_RESPONSE))
    assert quote.amount_breakdown.discounts is None
    assert "discounts" not in quote.to_wire()["amountBreakdown"]


def test_checkout_statuses_are_reaps_five() -> None:
    """The lifecycle page names five checkout statuses; three are final."""
    assert {s.value for s in CheckoutStatus} == {
        "REQUIRES_ACTION",
        "PROCESSING",
        "COMPLETED",
        "FAILED",
        "EXPIRED",
    }
    assert {s for s in CheckoutStatus if s.is_final} == {
        CheckoutStatus.COMPLETED,
        CheckoutStatus.FAILED,
        CheckoutStatus.EXPIRED,
    }


def test_error_detail_is_optional_on_the_wire() -> None:
    """The rate-limit page's 429 body carries no ``detail``; it parses as None."""
    error = ErrorResponse.model_validate(ex.load(ex.RATE_LIMIT_RESPONSE)).error
    assert (error.code, error.detail) == ("RATE_LIMIT_EXCEEDED", None)


def _address(**overrides: str) -> dict[str, str]:
    return {
        "firstName": "Avery",
        "lastName": "Tan",
        "phone": "+85200000000",
        "addressLine1": "123 Example Street",
        "city": "Example City",
        "country": "HK",
    } | overrides


@pytest.mark.parametrize("phone", ["85200000000", "+0123456789", "+12345", "+1234567890123456"])
def test_phone_must_match_reaps_pattern(phone: str) -> None:
    r"""``shippingAddress.phone`` follows ``^\+[1-9]\d{6,14}$`` from the OpenAPI."""
    with pytest.raises(ValidationError):
        ShippingAddress.model_validate(_address(phone=phone))


@pytest.mark.parametrize(
    "body",
    [
        {"email": "a@example.com", "items": []},
        {"email": "a@example.com", "items": [{"variantId": "v", "quantity": 1}] * 21},
        {"email": "a@example.com", "items": [{"variantId": "v", "quantity": 0}]},
        {"email": "not-an-email", "items": [{"variantId": "v", "quantity": 1}]},
        {"email": "a@example.com", "items": [{"variantId": "v", "quantity": 1}], "offerCode": " "},
    ],
)
def test_items_quote_constraints(body: dict[str, Any]) -> None:
    """Items 1 to 20, quantity above 0, an email, a non-blank offer code."""
    with pytest.raises(ValidationError):
        CreateItemsQuoteRequest.model_validate(body)


def test_external_quote_needs_a_shipping_address() -> None:
    """External checkout quotes always need ``shippingAddress``."""
    raw = ex.load(ex.QUOTE_EXTERNAL_REQUEST)
    del raw["shippingAddress"]
    with pytest.raises(ValidationError):
        CreateExternalCheckoutQuoteRequest.model_validate(raw)


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (ProductSearchRequest, {"query": ""}),
        (ProductSearchRequest, {"query": "gpu", "pagination": {"limit": 51}}),
        (ProductSearchRequest, {"query": "gpu", "pagination": {"limit": 0}}),
        (ProductDetailsRequest, {"productIds": []}),
        (ProductDetailsRequest, {"productIds": [str(n) for n in range(11)]}),
        (ResolveVariantRequest, {"productId": "p", "optionIds": []}),
    ],
)
def test_product_request_constraints(model: type[BaseModel], body: dict[str, Any]) -> None:
    """Search query non-empty, limit 1 to 50, 1 to 10 product ids, at least one option id."""
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_return_url_must_be_https() -> None:
    """The OpenAPI describes ``returnUrl`` as an HTTPS URL."""
    raw = _substitute(
        ex.load(ex.CHECKOUT_REQUEST), {"<quote-id>": QUOTE_ID, "<enrollment-id>": ENROLLMENT_ID}
    )
    raw["presentation"] = {"type": "REDIRECT", "returnUrl": "http://example.com/orders/done"}
    with pytest.raises(ValidationError):
        CreateCheckoutRequest.model_validate(raw)


def test_requests_build_in_snake_case_with_constants_filled() -> None:
    """Callers build requests in snake_case; constants such as ``REDIRECT`` fill themselves."""
    quote = CreateItemsQuoteRequest(
        email="ops@example.com", items=[QuoteItem(variant_id="v", quantity=2)]
    )
    assert quote.to_wire() == {
        "email": "ops@example.com",
        "items": [{"variantId": "v", "quantity": 2}],
    }
    checkout = CreateCheckoutRequest(
        quote_id=QUOTE_ID,
        enrollment_id=ENROLLMENT_ID,
        presentation=Presentation(return_url="https://example.com/orders/done"),
    )
    assert checkout.to_wire()["presentation"] == {
        "type": "REDIRECT",
        "returnUrl": "https://example.com/orders/done",
    }

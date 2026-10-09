"""The Kwal participant contract's wire models, read as the skill's own helpers read it.

Every body here has the shape of a fixture in the Kwal skill at ``be5f52c``
(``skills/agent-payment/tests/``), so a model that parses them parses what the
participant gateway sends.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from youreapyousow.kwal.models import (
    KwalAmount,
    KwalErrorBody,
    KwalFunding,
    KwalPayment,
    KwalProductDetail,
    KwalProducts,
    KwalQuote,
    KwalSetup,
    KwalVariant,
    PaymentState,
    QuoteLine,
    QuoteRequest,
    SetupState,
)


def amount(minor_units: str, currency: str = "USDC", decimals: int = 6) -> dict[str, object]:
    """State an amount as the gateway does."""
    return {"minorUnits": minor_units, "currency": currency, "decimals": decimals}


QUOTE = {
    "quoteId": "quote_1",
    "lines": [{"variantId": "var_1", "quantity": 2}],
    "shippingOptions": [
        {"shippingOptionId": "ship_standard", "label": "Standard", "price": amount("500000")}
    ],
    "selectedShippingOptionId": "ship_standard",
    "subtotal": amount("3000000"),
    "shipping": amount("500000"),
    "tax": amount("100000"),
    "total": amount("3600000"),
    "expiresAtUnixSeconds": "1791535323",
}


def test_an_amount_is_exact_in_whole_units() -> None:
    """Minor units are text on the wire and become an exact decimal, never a float."""
    assert KwalAmount.model_validate(amount("12340000")).value == Decimal("12.34")
    assert KwalAmount.model_validate(amount("1234", "USD", 2)).value == Decimal("12.34")


def test_an_absent_amount_field_is_zero_as_proto3_omits_it() -> None:
    """proto3 JSON omits a zero, so no minor units and no decimals read as zero."""
    bare = KwalAmount.model_validate({"currency": "USD"})
    assert (bare.value, bare.decimals) == (Decimal(0), 0)
    assert KwalAmount.model_validate({}).currency is None


def test_an_amount_is_written_back_exactly() -> None:
    """The mock states an amount the way the gateway does: minor units as text."""
    written = KwalAmount.of(Decimal("19.02"), "USD", 2).to_wire()
    assert written == {"minorUnits": "1902", "currency": "USD", "decimals": 2}


@pytest.mark.parametrize("minor_units", ["-1", "1.5", "\uff11\uff12", "", "0x10"])
def test_an_amount_that_is_not_whole_ascii_units_is_refused(minor_units: str) -> None:
    """Another script's digits or a sign would be read as a number nobody can compare."""
    with pytest.raises(ValidationError):
        KwalAmount.model_validate(amount(minor_units))


def test_a_search_reads_each_product_and_an_absent_list_as_empty() -> None:
    """``merchant`` and ``price`` are optional; a search without a match is empty."""
    found = KwalProducts.model_validate(
        {
            "products": [
                {
                    "productId": "p1",
                    "title": "Fan",
                    "merchant": "Parts Co",
                    "price": amount("1500", "USD", 2),
                },
                {"productId": "p2", "title": "Drive"},
            ]
        }
    )
    assert [p.product_id for p in found.products] == ["p1", "p2"]
    assert found.products[1].merchant is None
    assert found.products[1].price is None
    assert KwalProducts.model_validate({}).products == []


def test_a_product_reads_its_options_and_refuses_one_without_values() -> None:
    """An option decides the variant, so an option without values cannot be offered."""
    body = {
        "product": {"productId": "p1", "title": "Fan"},
        "options": [{"name": "Size", "values": [{"optionId": "o1", "label": "120 mm"}]}],
    }
    detail = KwalProductDetail.model_validate(body)
    assert detail.options[0].values[0].option_id == "o1"
    with pytest.raises(ValidationError):
        KwalProductDetail.model_validate(body | {"options": [{"name": "Size", "values": []}]})


def test_a_variant_is_not_purchasable_unless_it_says_so() -> None:
    """proto3 JSON omits a false flag; a purchasable variant carries the price it quotes at."""
    assert not KwalVariant.model_validate({"variantId": "v1"}).purchasable
    priced = {"variantId": "v1", "purchasable": True, "price": amount("1500", "USD", 2)}
    assert KwalVariant.model_validate(priced).purchasable
    with pytest.raises(ValidationError):
        KwalVariant.model_validate({"variantId": "v1", "purchasable": True})


def test_a_quote_reads_every_figure_and_its_deadline() -> None:
    """The total is what funding covers and what the checkout charges."""
    quote = KwalQuote.model_validate(QUOTE)
    assert quote.total.value == Decimal("3.6")
    assert quote.expires_at_unix_seconds == 1791535323
    assert quote.selected_shipping_option_id == "ship_standard"
    assert quote.payment_id is None


@pytest.mark.parametrize(
    "broken",
    [
        {"total": amount("0")},
        {"lines": []},
        {"expiresAtUnixSeconds": "0"},
        {"selectedShippingOptionId": "ship_unknown"},
    ],
)
def test_a_quote_that_cannot_be_reviewed_is_refused(broken: dict[str, object]) -> None:
    """No total, no line, no deadline, or a selection it does not offer: a contract mismatch."""
    with pytest.raises(ValidationError):
        KwalQuote.model_validate(QUOTE | broken)


def test_a_quote_request_is_written_with_the_wire_names() -> None:
    """The request carries the email, the lines and, only when given, the address."""
    request = QuoteRequest(
        email="operator@example.com", lines=[QuoteLine(variant_id="v1", quantity=1)]
    )
    assert request.to_wire() == {
        "email": "operator@example.com",
        "lines": [{"variantId": "v1", "quantity": 1}],
    }


def test_a_simulated_card_spend_reads_its_hold_and_its_card_transaction() -> None:
    """A sandbox payment names the card transaction and what the vault holds for it."""
    payment = KwalPayment.model_validate(
        {
            "paymentId": "pay_1",
            "state": "PARTICIPANT_PAYMENT_STATE_PENDING",
            "step": "card_authorization",
            "cardTransactionId": "600ddd6f-f258-48d0-adf5-d081dd10df04",
            "held": amount("12340000"),
            "amount": amount("1234", "USD", 2),
            "quoteId": "quote_1",
        }
    )
    assert payment.state == PaymentState.PENDING
    assert payment.held is not None
    assert payment.held.value == Decimal("12.34")


def test_a_payment_waiting_for_approval_needs_an_https_link() -> None:
    """The human approves on the hosted page, so a waiting state without a link is refused."""
    waiting = {"paymentId": "pay_1", "state": "PARTICIPANT_PAYMENT_STATE_REQUIRES_ACTION"}
    with pytest.raises(ValidationError):
        KwalPayment.model_validate(waiting)
    with pytest.raises(ValidationError):
        KwalPayment.model_validate(waiting | {"approvalUrl": "javascript:alert(1)"})
    linked = KwalPayment.model_validate(waiting | {"approvalUrl": "https://pay.example/1"})
    assert linked.approval_url == "https://pay.example/1"


def test_an_unknown_payment_state_is_refused() -> None:
    """A state the build cannot act on is a contract mismatch."""
    with pytest.raises(ValidationError):
        KwalPayment.model_validate({"paymentId": "pay_1", "state": "PENDING"})


def test_a_ready_setup_reads_its_card_and_deposit() -> None:
    """Ready means an active card and an observed deposit."""
    setup = KwalSetup.model_validate(
        {
            "state": "PARTICIPANT_SETUP_STATE_READY",
            "vaultAddress": "0x" + "ab" * 20,
            "chain": "ink-sepolia",
            "cardStatus": "ACTIVE",
            "enrollmentId": "11111111-1111-4111-8111-111111111111",
            "enrollmentStatus": "PARTICIPANT_ENROLLMENT_STATUS_ACTIVE",
            "depositObserved": True,
        }
    )
    assert setup.state == SetupState.READY
    assert setup.enrollment_status == "ACTIVE"


def test_a_ready_setup_without_its_evidence_is_refused() -> None:
    """Ready without an active card or a deposit is not ready (the skill's ``parse_setup``)."""
    with pytest.raises(ValidationError):
        KwalSetup.model_validate(
            {"state": "PARTICIPANT_SETUP_STATE_READY", "vaultAddress": "0x" + "ab" * 20}
        )


def test_funding_amounts_are_usdc_with_six_decimals() -> None:
    """The service states every funding amount in six-decimal USDC."""
    funding = KwalFunding.model_validate(
        {"state": "PARTICIPANT_FUNDING_STATE_READY", "available": amount("4000000")}
    )
    assert funding.available is not None
    assert funding.available.value == Decimal(4)
    with pytest.raises(ValidationError):
        KwalFunding.model_validate(
            {"state": "PARTICIPANT_FUNDING_STATE_READY", "available": amount("400", "USD", 2)}
        )


def test_an_error_body_names_its_tag() -> None:
    """Only the stable tag after ``tag:kraken.com,2025:`` is read; titles may be private."""
    error = KwalErrorBody.model_validate(
        {"type": "tag:kraken.com,2025:ParticipantCardBusy", "title": "private text"}
    )
    assert error.tag == "ParticipantCardBusy"
    assert KwalErrorBody.model_validate({"type": "about:blank"}).tag is None
    assert KwalErrorBody.model_validate({}).tag is None


def test_a_setup_states_its_enrolment_status_in_full_on_the_wire() -> None:
    """Read by its short name, written back by the full enum name the gateway uses."""
    body = {
        "state": "PARTICIPANT_SETUP_STATE_PENDING",
        "enrollmentId": "11111111-1111-4111-8111-111111111111",
        "enrollmentStatus": "PARTICIPANT_ENROLLMENT_STATUS_REQUIRES_ACTION",
    }
    assert KwalSetup.model_validate(body).to_wire()["enrollmentStatus"] == (
        "PARTICIPANT_ENROLLMENT_STATUS_REQUIRES_ACTION"
    )

"""Tests for webhook signing and verification."""

import json
from datetime import timedelta

import pytest

from tests.conftest import START
from youreapyousow.reap.webhooks import WebhookSignatureError, sign, verify

SECRET = "whsec_test"
BODY = json.dumps({"id": "e1", "type": "CARD_STATUS_UPDATED", "data": {"id": "c"}}).encode()
NOW_TS = int(START.timestamp())


def test_valid_signature_verifies_and_parses() -> None:
    """A correctly signed, fresh delivery parses into its envelope."""
    event = verify(SECRET, BODY, sign(SECRET, BODY, NOW_TS), START)
    assert (event.id, event.type, event.data) == ("e1", "CARD_STATUS_UPDATED", {"id": "c"})


def test_signature_format_matches_reap() -> None:
    """The header carries ``t=`` and a hex ``v1=`` digest."""
    header = sign(SECRET, BODY, NOW_TS)
    t_part, v1_part = header.split(",")
    assert t_part == f"t={NOW_TS}"
    assert len(v1_part.removeprefix("v1=")) == 64


@pytest.mark.parametrize(
    ("header", "body", "message"),
    [
        (None, BODY, "missing"),
        ("garbage", BODY, "malformed"),
        ("t=abc,v1=00", BODY, "malformed"),
        (sign("other", BODY, NOW_TS), BODY, "does not match"),
        (sign(SECRET, BODY, NOW_TS), BODY + b" ", "does not match"),
        (sign(SECRET, BODY, NOW_TS - 301), BODY, "five-minute"),
        (sign(SECRET, b"[]", NOW_TS), b"[]", "not a webhook envelope"),
    ],
)
def test_bad_deliveries_are_rejected(header: str | None, body: bytes, message: str) -> None:
    """Missing, malformed, forged, tampered, stale and non-envelope deliveries fail."""
    with pytest.raises(WebhookSignatureError, match=message):
        verify(SECRET, body, header, START)


def test_future_skew_within_window_is_accepted() -> None:
    """A small clock skew in either direction is tolerated."""
    verify(SECRET, BODY, sign(SECRET, BODY, NOW_TS + 60), START)
    verify(SECRET, BODY, sign(SECRET, BODY, NOW_TS), START + timedelta(seconds=299))

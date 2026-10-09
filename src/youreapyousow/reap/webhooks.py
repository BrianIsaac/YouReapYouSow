"""Reap webhook signing and verification.

Reap signs every delivery with HMAC-SHA256 over ``{timestamp}.{raw_body}`` and sends
``X-Reap-Webhook-Signature: t=<unix seconds>,v1=<hex digest>``; receivers reject
anything older than five minutes (https://docs.reap.global/webhooks/signature-verification.md).
The mock signs with the same scheme, so one receiver serves both.
"""

import hashlib
import hmac
from datetime import datetime

from pydantic import ValidationError

from youreapyousow.reap.models import WebhookEvent

SIGNATURE_HEADER = "X-Reap-Webhook-Signature"
TOLERANCE_SECONDS = 300


class WebhookSignatureError(ValueError):
    """Raised when a delivery's signature is missing, malformed, wrong or stale."""


def sign(secret: str, body: bytes, timestamp: int) -> str:
    """Produce the signature header value for a body.

    Args:
        secret: The endpoint's signing secret.
        body: The exact raw request body.
        timestamp: Unix seconds at signing.

    Returns:
        The header value ``t=<timestamp>,v1=<hex digest>``.
    """
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def verify(secret: str, body: bytes, header: str | None, now: datetime) -> WebhookEvent:
    """Verify a delivery and parse its envelope.

    Args:
        secret: The endpoint's signing secret.
        body: The exact raw request body.
        header: The ``X-Reap-Webhook-Signature`` value.
        now: Current time, for the replay window.

    Returns:
        The parsed ``{id, type, data}`` envelope.

    Raises:
        WebhookSignatureError: If the signature is absent, malformed, wrong or older
            than five minutes, or the body is not a webhook envelope.
    """
    if not header:
        raise WebhookSignatureError("missing signature header")
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    try:
        timestamp = int(parts["t"])
        provided = parts["v1"]
    except (KeyError, ValueError) as error:
        raise WebhookSignatureError("malformed signature header") from error
    if abs(now.timestamp() - timestamp) > TOLERANCE_SECONDS:
        raise WebhookSignatureError("signature timestamp outside the five-minute window")
    expected = sign(secret, body, timestamp).split("v1=", 1)[1]
    if not hmac.compare_digest(expected, provided):
        raise WebhookSignatureError("signature does not match")
    try:
        return WebhookEvent.model_validate_json(body)
    except ValidationError as error:
        raise WebhookSignatureError("body is not a webhook envelope") from error

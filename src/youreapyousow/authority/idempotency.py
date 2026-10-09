"""Idempotency keys for money-moving calls.

The key is built from identifiers the caller cannot choose, the intent and the ledger
event that claimed it for execution. It is therefore stable for the life of one claim,
so a replay of the same claim is recognised by Reap (and returns the recorded outcome
locally), and it can never be minted twice for one intent because the claim is a
compare-and-set that only one caller wins.
"""


def purchase_idempotency_key(intent_id: str, claim_event_id: str) -> str:
    """Derive the key sent to Reap as ``Idempotency-Key`` for one purchase.

    Args:
        intent_id: The purchase intent.
        claim_event_id: The ledger event that claimed the intent.

    Returns:
        A high-entropy key well under Reap's 255-character limit.
    """
    return f"{intent_id}:{claim_event_id}"

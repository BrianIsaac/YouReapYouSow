"""Tests for idempotency key derivation."""

from youreapyousow.authority.idempotency import purchase_idempotency_key


def test_key_is_stable_and_distinct_per_claim() -> None:
    """The same claim yields the same key; a different claim a different one."""
    key = purchase_idempotency_key("int_a", "evt_1")
    assert key == purchase_idempotency_key("int_a", "evt_1")
    assert key != purchase_idempotency_key("int_a", "evt_2")
    assert len(key) <= 255

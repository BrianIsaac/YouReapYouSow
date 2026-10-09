"""Identifier generation."""

import uuid


def new_id(prefix: str) -> str:
    """Return a new unique identifier with a readable prefix.

    Args:
        prefix: A short record-kind prefix such as ``obj`` or ``int``.

    Returns:
        An identifier of the form ``<prefix>_<32 hex characters>``.
    """
    return f"{prefix}_{uuid.uuid4().hex}"

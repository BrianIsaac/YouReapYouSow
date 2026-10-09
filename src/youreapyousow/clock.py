"""Time source for the control plane.

Every rule that depends on time takes ``now`` explicitly, and every service takes a
``Clock`` so tests can drive time deterministically.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

type Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime.

    Returns:
        The current UTC time.
    """
    return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to, for tests and replayable simulations."""

    def __init__(self, start: datetime) -> None:
        """Create a clock fixed at ``start``.

        Args:
            start: The initial time; must be timezone-aware.

        Raises:
            ValueError: If ``start`` is naive.
        """
        if start.tzinfo is None:
            raise ValueError("ManualClock needs a timezone-aware start time")
        self._now = start

    def __call__(self) -> datetime:
        """Return the clock's current time.

        Returns:
            The current simulated time.
        """
        return self._now

    def advance(self, **delta: float) -> datetime:
        """Move the clock forward.

        Args:
            **delta: Keyword arguments accepted by ``datetime.timedelta``.

        Returns:
            The new current time.
        """
        self._now = self._now + timedelta(**delta)
        return self._now

"""The one console error the room is allowed: its probe for the drops list on an older server.

The room asks ``GET /api/drops`` and, on a 404, falls back to the single drop at
``/api/state``; the browser logs that 404 itself, which no page code can silence.
"""

from __future__ import annotations

from playwright.sync_api import ConsoleMessage


def probe_404(msg: ConsoleMessage) -> bool:
    """Tells whether a console error is the drops probe's expected 404.

    Args:
        msg: The console message.

    Returns:
        True for the probe's 404 and nothing else.
    """
    url = str(msg.location.get("url", ""))
    return "404" in msg.text and url.endswith("/api/drops")

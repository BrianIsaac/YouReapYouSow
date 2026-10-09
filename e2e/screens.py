"""Screenshots of the room's screens at laptop and phone widths, failing on console errors.

Run against any server that serves the room (the fixture server or the real app):
``uv run --no-project --with playwright python e2e/screens.py http://127.0.0.1:8000 out/``
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import ConsoleMessage, sync_playwright
from probe import probe_404

WIDTHS = {"laptop": (1440, 900), "phone": (390, 844)}
ROUTES = ("drops", "drop", "join", "coach", "agreement", "challenge", "result")


def shoot(  # noqa: PLR0917 - each argument is one plain option
    base: str, out: Path, player: str | None, routes: list[str], tag: str, drop: str
) -> list[str]:
    """Opens each route at each width, saves a full-page screenshot and collects errors.

    Args:
        base: The server's base URL.
        out: The directory for the screenshots.
        player: A player id to view as, or None for a viewer without a seat.
        routes: The routes to open.
        tag: A prefix for the file names.
        drop: The drop to open (``main`` for a server with a single drop).

    Returns:
        Every console error and page error seen, one line each.
    """
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
        )
        for label, (width, height) in WIDTHS.items():
            context = browser.new_context(viewport={"width": width, "height": height})
            context.grant_permissions(["camera"])
            page = context.new_page()

            def on_console(msg: ConsoleMessage, where: str = label) -> None:
                if msg.type == "error" and not probe_404(msg):
                    errors.append(f"{where}: {msg.text}")

            page.on("console", on_console)
            page.on("pageerror", lambda exc, where=label: errors.append(f"{where}: {exc}"))
            query = f"?player={player}" if player else ""
            for route in routes:
                where = "#/drops" if route == "drops" else f"#/d/{drop}/{route}"
                page.goto(f"{base}/{query}{where}")
                page.wait_for_selector("main .stack, main .card, main .stack-lg", timeout=8000)
                page.wait_for_timeout(900)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                if overflow > 0:
                    errors.append(f"{label} {route}: page scrolls sideways by {overflow}px")
                page.screenshot(path=str(out / f"{tag}-{route}-{label}.png"), full_page=True)
            context.close()
        browser.close()
    return errors


def main() -> None:
    """Parses the arguments, shoots the screens and exits non-zero on any error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("out", type=Path)
    parser.add_argument("--player", default=None)
    parser.add_argument("--routes", default=",".join(ROUTES))
    parser.add_argument("--tag", default="screen")
    parser.add_argument("--drop", default="main")
    args = parser.parse_args()
    errors = shoot(
        args.base.rstrip("/"), args.out, args.player, args.routes.split(","), args.tag, args.drop
    )
    for line in errors:
        print(line)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()

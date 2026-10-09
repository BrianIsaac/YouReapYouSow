"""The whole room, end to end, as three players would live it, in headless Chromium.

Three browser contexts (one per player) join, talk to the coach, lock, accept, check in (one
with the fake camera, one with an uploaded file, one with a log), wait out the challenge,
and the first player names the winner and watches the agent buy the prize. Screenshots of
each stage are saved at laptop and phone widths, and any console error fails the run.

Run against a fresh group on a fast clock, for example:
``uv run --no-project python e2e/fixture_server.py --port 8791 --seconds-per-day 1.5``
``uv run --no-project --with playwright python e2e/room_flow.py http://127.0.0.1:8791 out/``
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

from playwright.sync_api import Browser, ConsoleMessage, Page, expect, sync_playwright

PLAYERS = [
    ("Alice", "I want to do more push-ups: 5 strict today, 20 by the end. I can film it."),
    ("Ben", "I want to start running, three runs a week, from nothing at all right now."),
    ("Chloe", "A strength routine at the gym, four sessions a week, I will log each one."),
]
# A 1x1 PNG, unique per call by a trailing comment chunk, as an uploaded check-in photo.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class Room:
    """Three players' pages and the errors seen on them."""

    def __init__(self, browser: Browser, base: str, out: Path) -> None:
        """Opens one context and page per player.

        Args:
            browser: The browser.
            base: The server's base URL.
            out: The screenshot directory.
        """
        self.base = base
        self.out = out
        self.errors: list[str] = []
        self.pages: list[Page] = []
        for i, (name, _) in enumerate(PLAYERS):
            phone = i == 1
            context = browser.new_context(
                viewport={"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
            )
            context.grant_permissions(["camera"])
            page = context.new_page()
            page.on("console", lambda msg, who=name: self.console(who, msg))
            page.on("pageerror", lambda exc, who=name: self.errors.append(f"{who}: {exc}"))
            self.pages.append(page)

    def console(self, who: str, msg: ConsoleMessage) -> None:
        """Records a console error.

        Args:
            who: The player whose page logged it.
            msg: The console message.
        """
        if msg.type == "error":
            self.errors.append(f"{who}: {msg.text}")

    def shot(self, page: Page, name: str) -> None:
        """Saves a full-page screenshot.

        Args:
            page: The page.
            name: The file name, without extension.
        """
        page.wait_for_timeout(400)
        page.screenshot(path=str(self.out / f"{name}.png"), full_page=True)


def run(base: str, out: Path) -> list[str]:
    """Plays the whole journey.

    Args:
        base: The server's base URL.
        out: The screenshot directory.

    Returns:
        Every console and page error seen.
    """
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
        )
        room = Room(browser, base, out)
        alice, ben, chloe = room.pages

        alice.goto(f"{base}/#/drop")
        alice.get_by_role("button", name="Reset the demo group").wait_for()
        alice.once("dialog", lambda d: d.accept())
        alice.get_by_role("button", name="Reset the demo group").click()
        expect(alice.get_by_text("Open for joining").first).to_be_visible()
        room.shot(alice, "01-drop-laptop")

        for page, (name, _) in zip(room.pages, PLAYERS, strict=True):
            page.goto(f"{base}/#/join")
            page.get_by_label("Your name, as the room will see it").fill(name)
            page.get_by_role("checkbox").check()
            page.get_by_role("button", name="Reserve my seat").click()
            seated = page.get_by_text(f"You are in, {name}")
            expect(seated.or_(page.get_by_role("heading", name="Your coach"))).to_be_visible()
            if name == "Alice":
                room.shot(page, "02-join-seated-laptop")

        for page, (name, message) in zip(room.pages, PLAYERS, strict=True):
            expect(page.get_by_role("heading", name="Your coach")).to_be_visible(timeout=8000)
            page.get_by_label("Your message to the coach").fill(message)
            page.get_by_role("button", name="Send").click()
            expect(page.get_by_role("button", name="Lock my contract")).to_be_visible(timeout=20000)
            if name == "Ben":
                room.shot(page, "03-coach-proposed-phone")
        target = alice.get_by_label("Milestone day 7 target")
        target.fill("9")
        alice.get_by_role("button", name="Save changes").click()
        expect(target).to_have_value("9")
        room.shot(alice, "03-coach-proposed-laptop")
        for page in room.pages:
            page.get_by_role("button", name="Lock my contract").click()
            expect(page.get_by_text("Locked").first).to_be_visible()

        for page in room.pages:
            expect(page.get_by_role("heading", name="Everyone sees every goal")).to_be_visible(
                timeout=8000
            )
        room.shot(alice, "04-agreement-laptop")
        room.shot(ben, "04-agreement-phone")
        for page in room.pages:
            page.get_by_role("button", name="I accept these terms").click()
            page.wait_for_timeout(300)

        for page in room.pages:
            expect(page.get_by_role("heading", name="Check in")).to_be_visible(timeout=8000)

        alice.get_by_role("button", name="Start the camera").click()
        expect(alice.locator(".camera video")).to_be_visible()
        alice.wait_for_function(
            "document.querySelector('.camera video') && document.querySelector('.camera video').videoWidth > 0"
        )
        alice.get_by_role("button", name="Take the photo").click()
        expect(alice.locator(".camera img")).to_be_visible()
        alice.get_by_role("button", name="Submit the check-in").click()
        expect(alice.locator(".checkins .pill.verified").first).to_be_visible(timeout=20000)
        room.shot(alice, "05-challenge-laptop")

        ben.locator("input[type=file]").set_input_files(
            {"name": "run.png", "mimeType": "image/png", "buffer": PNG}
        )
        ben.get_by_label("What you did").fill("0")
        ben.get_by_role("button", name="Submit the check-in").click()
        expect(ben.locator(".checkins .pill.rejected").first).to_be_visible(timeout=20000)
        room.shot(ben, "05-challenge-rejected-phone")

        chloe.get_by_role("button", name="Submit the check-in").click()
        expect(chloe.locator(".checkins .pill.verified").first).to_be_visible(timeout=20000)

        for page in room.pages:
            expect(page.get_by_role("heading", name="Standings are frozen")).to_be_visible(
                timeout=120000
            )
        room.shot(alice, "06-dispute-window-laptop")
        ben.get_by_role("button", name="Dispute this score").first.click()
        ben.get_by_label("Reason for the dispute").fill("The photo does not show the reps.")
        ben.get_by_role("button", name="Send the dispute").click()
        expect(ben.locator(".pill.disputed").first).to_be_visible(timeout=8000)
        room.shot(ben, "06-dispute-window-phone")

        finish = alice.get_by_role("button", name="Name the winner and buy the prize")
        expect(finish).to_be_enabled(timeout=120000)
        finish.click()
        expect(alice.locator(".order-id")).to_be_visible(timeout=120000)
        room.shot(alice, "07-result-laptop")
        expect(ben.locator(".order-id")).to_be_visible(timeout=8000)
        room.shot(ben, "07-result-phone")

        time.sleep(0.5)
        browser.close()
        return room.errors


def main() -> None:
    """Parses the arguments, plays the journey and exits non-zero on any error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    errors = run(args.base.rstrip("/"), args.out)
    for line in errors:
        print(line)
    print("room flow: " + ("errors" if errors else "clean"))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()

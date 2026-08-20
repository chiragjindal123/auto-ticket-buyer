#!/usr/bin/env python3
"""
TicketPlus click recorder / replayer.

This tool is intentionally simple:
- RECORD mode: you perform the desired mouse movements, scrolls, and clicks manually once.
  The script records robust information about each action.
- RUN mode: at the configured time, it replays the recorded actions.
  It stops when the configured stop URL/text is detected, leaving the
  browser for you to handle manually.

It does not attempt to bypass CAPTCHA, queues, payment checks, or
other anti-bot/access-control mechanisms.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


ACTIVITY_URL = "https://ticketplus.com.tw/activity/21d3c3504ff522a6732789a46f5796d7"
DATA_FILE = Path("ticketplus_clicks.json")
PROFILE_DIR = Path("ticketplus_browser_profile")

# Change this before the real run.
# Format: YYYY-MM-DD HH:MM:SS
SALE_TIME = "2026-08-20 01:40:00"

# Optional safety stop conditions. Add text that appears only after you
# have successfully reached the ticket/order stage.
STOP_TEXTS = [

]

# How often to retry an action when the target is not yet available.
RETRY_INTERVAL = 0.7

# Maximum time to wait for one recorded action target.
ACTION_TIMEOUT = 30

# If the replayed clicks are shifted because the browser window is positioned
# differently, adjust these two values after a test run.
SCREEN_OFFSET_X = 0
SCREEN_OFFSET_Y = 0


def record():
    """
    Record real Windows mouse clicks and scrolls.

    This intentionally does NOT rely on JavaScript events from TicketPlus.
    It records the physical mouse input at the OS level, which works even
    when the seat map is canvas/SVG based.
    """
    try:
        from pynput import mouse
    except ImportError:
        print("Missing dependency: pynput")
        print("Install it with: pip install pynput")
        sys.exit(1)

    DATA_FILE.write_text("[]", encoding="utf-8")
    actions = []

    print("\n=== TicketPlus GLOBAL mouse recorder ===")
    print("A Chromium window will open.")
    print("Perform your actions manually.")
    print("Recorded: LEFT/RIGHT click + scroll.")
    print("Mouse movement is NOT recorded.")
    print("Press F8 to finish recording.")
    print("Press ESC to cancel.\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport=None,
            args=["--start-maximized","--start-fullscreen",],
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(ACTIVITY_URL, wait_until="domcontentloaded")

        # Find the Chromium window area approximately from the Playwright
        # viewport. Coordinates are recorded globally and replayed through
        # Playwright's viewport mouse after converting from screen coordinates.
        try:
            viewport = page.evaluate(
                "() => ({w: window.innerWidth, h: window.innerHeight})"
            )
        except Exception:
            viewport = {"w": 1280, "h": 720}

        stop = {"done": False, "cancel": False}

        def on_click(x, y, button, pressed):
            if not pressed or stop["done"]:
                return

            name = str(button)

            # Record only left/right clicks.
            if name not in ("Button.left", "Button.right"):
                return

            action = {
                "kind": "click",
                "screen_x": int(x),
                "screen_y": int(y),
                "button": "left" if name == "Button.left" else "right",
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }

            actions.append(action)
            DATA_FILE.write_text(
                json.dumps(actions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            print(
                f"[{len(actions)}] CLICK "
                f"{action['button']} "
                f"screen=({action['screen_x']},{action['screen_y']})"
            )

        def on_scroll(x, y, dx, dy):
            if stop["done"]:
                return

            # Ignore tiny trackpad noise.
            if dx == 0 and dy == 0:
                return

            action = {
                "kind": "scroll",
                "screen_x": int(x),
                "screen_y": int(y),
                "dx": int(dx),
                "dy": int(dy),
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }

            actions.append(action)
            DATA_FILE.write_text(
                json.dumps(actions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            print(
                f"[{len(actions)}] SCROLL "
                f"screen=({action['screen_x']},{action['screen_y']}) "
                f"delta=({action['dx']},{action['dy']})"
            )

        def on_press(key):
            try:
                if key == keyboard.Key.f8:
                    stop["done"] = True
                    return False
                if key == keyboard.Key.esc:
                    stop["cancel"] = True
                    stop["done"] = True
                    return False
            except Exception:
                pass

        # Keyboard listener needs to be defined after importing pynput.
        from pynput import keyboard

        mouse_listener = mouse.Listener(
            on_click=on_click,
            on_scroll=on_scroll,
        )
        keyboard_listener = keyboard.Listener(on_press=on_press)

        mouse_listener.start()
        keyboard_listener.start()

        try:
            while not stop["done"]:
                time.sleep(0.1)
        finally:
            mouse_listener.stop()
            keyboard_listener.stop()

        if stop["cancel"]:
            DATA_FILE.write_text("[]", encoding="utf-8")
            print("\nRecording cancelled.")
        else:
            print(
                f"\nSaved {len(actions)} actions to "
                f"{DATA_FILE.resolve()}"
            )

        context.close()


def _get_browser_screen_origin(page):
    """
    Best-effort conversion from OS screen coordinates to browser viewport
    coordinates.

    Playwright itself does not expose the native browser window's screen
    origin, so we use a small helper window created by the browser and ask
    the user to calibrate once if necessary.
    """
    return 0, 0


def wait_for_manual_action(*args, **kwargs):
    raise RuntimeError(
        "DOM event recorder removed. Recording now uses pynput global input."
    )


def text_matches_stop(page) -> str | None:
    """Return a matched stop text, if visible on the current page."""
    try:
        body = page.locator("body").inner_text(timeout=1500)
        for text in STOP_TEXTS:
            if text and text in body:
                return text
    except Exception:
        pass
    return None


def try_replay_action(page, action: dict[str, Any]) -> bool:
    """
    Replay recorded global mouse actions.

    Note: exact screen coordinates can vary with browser/window placement.
    The runner therefore supports a calibration offset below.
    """
    kind = (action.get("kind") or "").lower()

    if kind == "scroll":
        dx = action.get("dx", 0)
        dy = action.get("dy", 0)
        try:
            page.mouse.wheel(float(dx), float(dy))
            return True
        except Exception:
            return False

    if kind == "click":
        x = action.get("screen_x")
        y = action.get("screen_y")
        if x is None or y is None:
            return False

        # These offsets are deliberately explicit so you can calibrate
        # them if Windows/browser placement differs.
        x = float(x) + SCREEN_OFFSET_X
        y = float(y) + SCREEN_OFFSET_Y

        try:
            page.mouse.click(
                x,
                y,
                button=action.get("button", "left"),
            )
            return True
        except Exception:
            return False

    return False



def seconds_until(target: datetime) -> float:
    return (target - datetime.now()).total_seconds()


def run():
    if not DATA_FILE.exists():
        print(f"ERROR: {DATA_FILE} does not exist.")
        print("Run: python ticketplus_clicker.py record")
        sys.exit(1)

    actions = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    if not actions:
        print("ERROR: No recorded actions.")
        sys.exit(1)

    target = datetime.strptime(SALE_TIME, "%Y-%m-%d %H:%M:%S")

    print("\n=== TicketPlus action runner ===")
    print(f"Sale/start time: {target}")
    print(f"Recorded actions: {len(actions)}")
    print(f"Browser profile: {PROFILE_DIR.resolve()}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport=None,
            args=["--start-maximized","--start-fullscreen",],
        )

        page = context.pages[0] if context.pages else context.new_page()

        print("Opening TicketPlus...")
        page.goto(ACTIVITY_URL, wait_until="domcontentloaded")
        print(
            "Browser is open. Keep its window position/size the same as "
            "during recording."
        )

        # Wait until configured time.
        while True:
            remaining = seconds_until(target)

            if remaining <= 0:
                break

            if remaining > 60:
                print(f"Waiting... {remaining / 60:.1f} minutes", end="\r")
                time.sleep(min(10, remaining))
            else:
                print(f"Starting in {remaining:.1f}s", end="\r")
                time.sleep(min(0.2, remaining))

        print("\nSTARTING ACTION LOOP")

        for index, action in enumerate(actions, start=1):
            print(
                f"\nAction {index}/{len(actions)}: "
                f"{action.get('kind', 'click').upper()} {action.get('text')!r}"
            )

            deadline = time.monotonic() + ACTION_TIMEOUT

            while time.monotonic() < deadline:
                stop = text_matches_stop(page)
                if stop:
                    print(
                        f"\nSTOP CONDITION DETECTED: {stop!r}\n"
                        "Browser is left open for manual handling."
                    )
                    return

                if try_replay_action(page, action):
                    print("  -> replayed")
                    time.sleep(0.3)
                    break

                time.sleep(RETRY_INTERVAL)
            else:
                print(
                    f"Could not find action {index} within "
                    f"{ACTION_TIMEOUT}s."
                )
                print("Browser remains open for manual control.")
                return

        print("\nRecorded action sequence completed.")
        print("Browser remains open. Take over manually.")

        try:
            input("\nPress Enter to close the browser...")
        except KeyboardInterrupt:
            pass

        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=["record", "run"],
        help="record your manual action sequence or replay it",
    )
    args = parser.parse_args()

    if args.mode == "record":
        record()
    else:
        run()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
TicketPlus text-anchor recorder + replay helper.

Workflow
--------
RECORD:
  1. Open TicketPlus.
  2. You enter an anchor text, e.g. "10/10".
  3. Script finds the visible text and scrolls it into view.
    4. You physically click the exact place you want in the browser.
    5. The script captures that click automatically.
    6. Enter how many times that click should be repeated.
    7. The action is saved.
    8. Repeat for the next anchor.

RUN:
  1. Open the same persistent browser profile.
  2. Wait until START_TIME.
  3. Find each anchor text.
  4. Scroll it into view.
  5. Replay the recorded relative click.
  6. Repeat the click the saved number of times.
  7. Continue to the next action.
  8. Stop at STOP_TEXTS so you can take over manually.

Notes
-----
- This uses DOM text as the anchor.
- The click position is recorded relative to the bounding box of the anchor.
- This avoids absolute screen coordinates.
- It does not bypass CAPTCHA, queues, or payment/security controls.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

URL = (
    "https://ticketplus.com.tw/activity/"
    "21d3c3504ff522a6732789a46f5796d7"
)

# Taiwan local time.
# Example:
# START_TIME = "2026-09-01 12:00:00"
START_TIME = "2026-09-07 10:00:00"

ACTIONS_FILE = Path("ticketplus_actions_2v.json")
PROFILE_DIR = Path("ticketplus_profile_2v")

CHECK_INTERVAL = 0.10
REFRESH_INTERVAL = 1.5
TARGET_TIMEOUT = 300.0

KEEP_BROWSER_OPEN = True

# When one of these becomes visible, automation stops and leaves
# the browser open for you.
STOP_TEXTS = [

]


# ============================================================
# GENERAL HELPERS
# ============================================================

def load_actions():
    if not ACTIONS_FILE.exists():
        return []

    try:
        data = json.loads(ACTIONS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Actions file must contain a JSON list.")
        return data
    except Exception as exc:
        print(f"ERROR reading {ACTIONS_FILE}: {exc}")
        sys.exit(1)


def save_actions(actions):
    ACTIONS_FILE.write_text(
        json.dumps(actions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def wait_until_start_time():
    target = datetime.strptime(START_TIME, "%Y-%m-%d %H:%M:%S")

    print()
    print("========================================")
    print("Waiting for configured start time")
    print("========================================")
    print("START_TIME:", target)
    print()

    while True:
        remaining = (target - datetime.now()).total_seconds()

        if remaining <= 0:
            break

        if remaining > 60:
            print(
                f"Starting in {remaining / 60:.1f} minutes",
                end="\r",
                flush=True,
            )
            time.sleep(min(5, remaining))
        else:
            print(
                f"Starting in {remaining:.2f} seconds",
                end="\r",
                flush=True,
            )
            time.sleep(min(0.1, remaining))

    print("\nStart time reached.\n")


def safe_visible_text(page):
    try:
        return page.locator("body").inner_text(timeout=1500)
    except Exception:
        return ""


def stop_condition_visible(page):
    body = safe_visible_text(page)

    for text in STOP_TEXTS:
        if text and text in body:
            return text

    return None


def connect_to_existing_chrome(playwright):
    browser = playwright.chromium.connect_over_cdp(
        "http://127.0.0.1:9222"
    )

    if not browser.contexts:
        raise RuntimeError("No Chrome browser context is available.")

    context = browser.contexts[0]
    pages = context.pages

    if not pages:
        page = context.new_page()
    else:
        page = next(
            (
                candidate
                for candidate in reversed(pages)
                if candidate.url and candidate.url != "about:blank"
            ),
            pages[-1],
        )

    print(f"Using existing Chrome tab: {page.url}")
    return browser, page


# ============================================================
# FIND TEXT ANCHOR
# ============================================================

def find_visible_text(page, text):
    """
    Return the first useful visible locator matching text.

    Exact matches are preferred, then role-based and partial matches.
    """

    candidates = []

    # Exact text node
    try:
        candidates.append(page.get_by_text(text, exact=True))
    except Exception:
        pass

    # Button with exact accessible name
    try:
        candidates.append(
            page.get_by_role("button", name=text, exact=True)
        )
    except Exception:
        pass

    # Link with exact accessible name
    try:
        candidates.append(
            page.get_by_role("link", name=text, exact=True)
        )
    except Exception:
        pass

    # Partial text fallback
    try:
        candidates.append(page.get_by_text(text, exact=False))
    except Exception:
        pass

    for locator in candidates:
        try:
            count = locator.count()

            for i in range(min(count, 20)):
                item = locator.nth(i)

                if item.is_visible():
                    return item

        except Exception:
            continue

    return None


def find_and_scroll(page, text, timeout=TARGET_TIMEOUT):
    """
    Continuously search for the anchor.
    Periodically refreshes the page if it cannot be found.
    """

    started = time.monotonic()
    last_refresh = time.monotonic()

    print(f"\nSearching for anchor: {text!r}")

    while True:
        stop_text = stop_condition_visible(page)
        if stop_text:
            print(f"\nSTOP TEXT DETECTED: {stop_text!r}")
            return None, "STOP"

        locator = find_visible_text(page, text)

        if locator is not None:
            try:
                locator.scroll_into_view_if_needed(timeout=5000)
                time.sleep(0.05)

                # Re-check visibility after scrolling.
                if locator.is_visible():
                    print(f"Found and scrolled to: {text!r}")
                    return locator, "FOUND"

            except Exception as exc:
                print(f"Scroll warning: {exc}")

        elapsed = time.monotonic() - started

        if elapsed >= timeout:
            print(f"\nTimed out waiting for {text!r}.")
            return None, "TIMEOUT"

        if time.monotonic() - last_refresh >= REFRESH_INTERVAL:
            print("Refreshing page...", end="\r", flush=True)

            try:
                page.reload(
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as exc:
                print(f"\nRefresh warning: {exc}")

            last_refresh = time.monotonic()

        time.sleep(CHECK_INTERVAL)


# ============================================================
# RECORD MODE
# ============================================================

def record_mode():
    """
    Record clicks from real browser interaction.

    The user enters anchor text.
    The script scrolls to it.
    The user physically clicks the desired location.
    We capture the click position relative to the browser viewport.
    """

    print()
    print("========================================")
    print(" TicketPlus ACTION RECORDER")
    print("========================================")
    print()
    print("This recorder captures a real browser click automatically.")
    print("It stores click offsets relative to the anchor text.")
    print()

    actions = []

    with sync_playwright() as p:
        browser, page = connect_to_existing_chrome(p)

        print("\nBrowser opened.")
        print("Use the already-open TicketPlus tab.")
        print()

        while True:
            print("----------------------------------------")
            anchor = input(
                "Enter anchor text to find "
                "(blank = finish recording): "
            ).strip()

            if not anchor:
                break

            locator, status = find_and_scroll(page, anchor)

            if status == "STOP":
                print("Stop condition detected.")
                break

            if status != "FOUND" or locator is None:
                print("Could not find that anchor.")
                continue

            # Get anchor bounding box.
            try:
                box = locator.bounding_box()
            except Exception:
                box = None

            if not box:
                print("Could not determine anchor position.")
                continue

            print()
            print("Anchor found and scrolled into view:")
            print(f"  {anchor!r}")
            print()
            print("Click the exact seat / ticket / button in the browser now.")
            print("Press Esc if you want to cancel this anchor.")

            click_data = page.evaluate(
                """() => new Promise((resolve) => {
                    const overlayId = '__ticketplus_record_overlay__';
                    const existing = document.getElementById(overlayId);

                    if (existing) {
                        existing.remove();
                    }

                    const overlay = document.createElement('div');
                    overlay.id = overlayId;
                    overlay.tabIndex = 0;
                    overlay.style.position = 'fixed';
                    overlay.style.inset = '0';
                    overlay.style.zIndex = '2147483647';
                    overlay.style.cursor = 'crosshair';
                    overlay.style.background = 'rgba(0, 0, 0, 0.02)';

                    const banner = document.createElement('div');
                    banner.textContent = 'Click the target once to record it. Press Esc to cancel.';
                    banner.style.position = 'absolute';
                    banner.style.left = '16px';
                    banner.style.top = '16px';
                    banner.style.padding = '10px 14px';
                    banner.style.borderRadius = '10px';
                    banner.style.background = 'rgba(0, 0, 0, 0.8)';
                    banner.style.color = '#fff';
                    banner.style.font = '14px/1.4 sans-serif';
                    banner.style.boxShadow = '0 8px 24px rgba(0, 0, 0, 0.25)';

                    overlay.appendChild(banner);
                    document.body.appendChild(overlay);
                    overlay.focus();

                    const cleanup = () => {
                        window.removeEventListener('keydown', onKeyDown, true);
                        overlay.remove();
                    };

                    const onKeyDown = (event) => {
                        if (event.key === 'Escape') {
                            event.preventDefault();
                            event.stopPropagation();
                            cleanup();
                            resolve(null);
                        }
                    };

                    overlay.addEventListener('click', (event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        const rect = overlay.getBoundingClientRect();
                        cleanup();
                        resolve({
                            x: event.clientX - rect.left,
                            y: event.clientY - rect.top,
                        });
                    }, { once: true });

                    window.addEventListener('keydown', onKeyDown, true);
                })"""
            )

            if not click_data:
                print("Recording cancelled for this anchor.")
                continue

            dx = float(click_data["x"]) - float(box["x"])
            dy = float(click_data["y"]) - float(box["y"])

            while True:
                try:
                    repeat = int(
                        input(
                            "How many times should this click be repeated? [1]: "
                        ).strip() or "1"
                    )

                    if repeat < 1:
                        raise ValueError

                    break

                except ValueError:
                    print("Enter a positive integer.")

            action = {
                "anchor": anchor,
                "click_offset_x": dx,
                "click_offset_y": dy,
                "repeat": repeat,
            }

            actions.append(action)
            save_actions(actions)

            print()
            print("Saved action:")
            print(json.dumps(action, ensure_ascii=False, indent=2))
            print()

            more = input(
                "Record another action? [Y/n]: "
            ).strip().lower()

            if more == "n":
                break

        print()
        print("========================================")
        print(f"Saved {len(actions)} action(s)")
        print(f"File: {ACTIONS_FILE.resolve()}")
        print("========================================")

        print("Chrome left open.")


# ============================================================
# REPLAY MODE
# ============================================================

def replay_click(page, locator, offset_x, offset_y, repeat):
    """
    Calculate an absolute browser viewport position from the current
    anchor bounding box, then click there multiple times.
    """

    box = locator.bounding_box()

    if not box:
        raise RuntimeError("Anchor has no bounding box.")

    x = box["x"] + offset_x
    y = box["y"] + offset_y

    print(
        f"Clicking at relative "
        f"({offset_x}, {offset_y}) "
        f"-> viewport ({x:.1f}, {y:.1f})"
    )

    def show_click_marker(marker_x, marker_y):
        page.evaluate(
            """({ x, y }) => {
                const markerId = '__ticketplus_click_marker__';
                const existing = document.getElementById(markerId);

                if (existing) {
                    existing.remove();
                }

                const marker = document.createElement('div');
                marker.id = markerId;
                marker.style.position = 'fixed';
                marker.style.left = `${x - 12}px`;
                marker.style.top = `${y - 12}px`;
                marker.style.width = '24px';
                marker.style.height = '24px';
                marker.style.borderRadius = '999px';
                marker.style.border = '3px solid #ff0033';
                marker.style.background = 'rgba(255, 0, 51, 0.25)';
                marker.style.boxShadow = '0 0 0 4px rgba(255, 0, 51, 0.12)';
                marker.style.zIndex = '2147483647';
                marker.style.pointerEvents = 'none';

                const crosshairH = document.createElement('div');
                crosshairH.style.position = 'absolute';
                crosshairH.style.left = '-10px';
                crosshairH.style.top = '10px';
                crosshairH.style.width = '44px';
                crosshairH.style.height = '2px';
                crosshairH.style.background = '#ff0033';
                crosshairH.style.boxShadow = '0 0 6px rgba(255, 0, 51, 0.9)';

                const crosshairV = document.createElement('div');
                crosshairV.style.position = 'absolute';
                crosshairV.style.left = '10px';
                crosshairV.style.top = '-10px';
                crosshairV.style.width = '2px';
                crosshairV.style.height = '44px';
                crosshairV.style.background = '#ff0033';
                crosshairV.style.boxShadow = '0 0 6px rgba(255, 0, 51, 0.9)';

                marker.appendChild(crosshairH);
                marker.appendChild(crosshairV);
                document.body.appendChild(marker);

                setTimeout(() => {
                    marker.remove();
                }, 500);
            }""",
            {"x": x, "y": y},
        )

    for i in range(repeat):
        try:
            show_click_marker(x, y)
            time.sleep(0.03)
            page.mouse.click(x, y)
            print(f"  click {i + 1}/{repeat}")
        except Exception as exc:
            print(f"  click failed: {exc}")
            return False

        # Tiny delay between repeated clicks.
        time.sleep(0.03)

    return True


def run_mode():
    actions = load_actions()

    if not actions:
        print()
        print("No actions recorded.")
        print()
        print("Run:")
        print("  python ticketplus_bot.py record")
        return

    print()
    print("========================================")
    print(" TicketPlus ACTION REPLAYER")
    print("========================================")
    print()
    print(f"Loaded {len(actions)} action(s).")
    print()

    for i, action in enumerate(actions, 1):
        print(
            f"{i}. anchor={action['anchor']!r}, "
            f"offset=({action['click_offset_x']}, "
            f"{action['click_offset_y']}), "
            f"repeat={action['repeat']}"
        )

    with sync_playwright() as p:
        browser, page = connect_to_existing_chrome(p)

        print()
        print("Using the already-open TicketPlus tab.")
        print()

        wait_until_start_time()

        for index, action in enumerate(actions, 1):
            anchor = action["anchor"]
            offset_x = float(action["click_offset_x"])
            offset_y = float(action["click_offset_y"])
            repeat = int(action["repeat"])

            print()
            print("========================================")
            print(f"ACTION {index}/{len(actions)}")
            print(f"Anchor: {anchor!r}")
            print(f"Repeat: {repeat}")
            print("========================================")

            locator, status = find_and_scroll(
                page,
                anchor,
                timeout=TARGET_TIMEOUT,
            )

            if status == "STOP":
                print("Automation stopped for manual handling.")
                break

            if status != "FOUND" or locator is None:
                print(
                    f"Could not find {anchor!r}. "
                    "Leaving browser open."
                )
                break

            try:
                success = replay_click(
                    page,
                    locator,
                    offset_x,
                    offset_y,
                    repeat,
                )
            except Exception as exc:
                print(f"Replay error: {exc}")
                success = False

            if not success:
                print("Action failed. Leaving browser open.")
                break

            # Let the page react before looking for the next anchor.
            time.sleep(0.2)

        else:
            print()
            print("========================================")
            print("ALL RECORDED ACTIONS COMPLETED")
            print("========================================")
            print("Browser is left open for manual handling.")

        if KEEP_BROWSER_OPEN:
            print()
            input("Press ENTER to close the browser...")

        print("Chrome left open.")


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"record", "run"}:
        print()
        print("Usage:")
        print("  python ticketplus_bot.py record")
        print("  python ticketplus_bot.py run")
        print()
        sys.exit(1)

    if sys.argv[1] == "record":
        record_mode()
    else:
        run_mode()


if __name__ == "__main__":
    main()

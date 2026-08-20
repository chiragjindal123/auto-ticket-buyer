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
SALE_TIME = "2026-09-01 12:00:00"

# Optional safety stop conditions. Add text that appears only after you
# have successfully reached the ticket/order stage.
STOP_TEXTS = [
    "訂單",
    "購票資料",
    "選擇票種",
    "驗證碼",
]

# How often to retry an action when the target is not yet available.
RETRY_INTERVAL = 0.7

# Maximum time to wait for one recorded action target.
ACTION_TIMEOUT = 30


def install_recorder(page):
    """
    Install capture-phase listeners for mouse movement, scroll, and clicks.

    We store every action in window.__ticketplus_actions so Python can
    retrieve it after the event. This survives normal page interactions
    but is re-installed automatically after navigation.
    """
    recorder_js = """
        (() => {
          window.__ticketplus_actions = window.__ticketplus_actions || [];

          const state = window.__ticketplus_state = window.__ticketplus_state || {
            lastMoveX: null,
            lastMoveY: null,
          };

          function cssPath(node) {
            if (!node || node.nodeType !== 1) return null;

            if (node.id) return '#' + CSS.escape(node.id);

            for (const attr of ['data-testid','data-test','data-cy','data-id','name','aria-label']) {
              const value = node.getAttribute(attr);
              if (value) {
                return node.tagName.toLowerCase() + '[' + attr + '="' +
                  value.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]';
              }
            }

            const parts = [];
            let cur = node;

            while (cur && cur.nodeType === 1 && parts.length < 7) {
              let p = cur.tagName.toLowerCase();
              const classes = Array.from(cur.classList || [])
                .filter(c => c.length < 60)
                .filter(c => !/^(active|selected|hover|focus|disabled|loading)$/i.test(c))
                .slice(0, 3);

              if (classes.length) {
                p += classes.map(c => '.' + CSS.escape(c)).join('');
              }

              const parent = cur.parentElement;
              if (parent) {
                const same = Array.from(parent.children)
                  .filter(x => x.tagName === cur.tagName);
                if (same.length > 1) {
                  p += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
                }
              }

              parts.unshift(p);
              if (cur.id) {
                parts[0] = '#' + CSS.escape(cur.id);
                break;
              }
              cur = cur.parentElement;
            }

            return parts.join(' > ');
          }

          function pushAction(action) {
            action.url = location.href;
            action.timestamp = Date.now();
            window.__ticketplus_actions.push(action);
          }

          document.addEventListener('mousemove', (event) => {
            if (event.clientX === state.lastMoveX && event.clientY === state.lastMoveY) {
              return;
            }

            state.lastMoveX = event.clientX;
            state.lastMoveY = event.clientY;

            pushAction({
              kind: 'move',
              x: event.clientX,
              y: event.clientY,
            });
          }, true);

          document.addEventListener('scroll', (event) => {
            const target = event.target && event.target.nodeType === 1
              ? event.target
              : document.scrollingElement || document.documentElement;

            pushAction({
              kind: 'scroll',
              selector: target === (document.scrollingElement || document.documentElement)
                ? null
                : cssPath(target),
              scroll_x: target.scrollLeft || 0,
              scroll_y: target.scrollTop || 0,
            });
          }, true);

          document.addEventListener('click', (event) => {
            const el = event.target && event.target.closest
              ? event.target.closest('button, a, input, select, [role="button"], [onclick]')
              : event.target;

                        if (!el) return;

                        const text = (el.innerText || el.value || el.getAttribute('aria-label') || '')
                            .replace(/\\s+/g, ' ')
              .trim()
              .slice(0, 200);

            pushAction({
              kind: 'click',
              selector: cssPath(el),
              text: text,
              tag: el.tagName.toLowerCase(),
              button: event.button,
              x: event.clientX,
              y: event.clientY,
            });
          }, true);
        })();
        """
    page.add_init_script(recorder_js)
    try:
        page.evaluate(recorder_js)
    except Exception:
        # The page may still be navigating; the init script will apply to the
        # next document load.
        pass


def wait_for_manual_action(page, already_seen: int):
    """Wait until the browser reports one new recorded action."""
    while True:
        try:
            actions = page.evaluate("window.__ticketplus_actions || []")
            if len(actions) > already_seen:
                return actions[already_seen]
        except Exception:
            pass
        time.sleep(0.1)


def record():
    DATA_FILE.write_text("[]", encoding="utf-8")

    print("\n=== TicketPlus action recorder ===")
    print("A browser will open.")
    print("Log in manually if necessary.")
    print("Then perform the desired mouse movement / scroll / click sequence ONCE.")
    print("Press Ctrl+C when you are finished.\n")

    actions = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport=None,
            args=["--start-maximized"],
        )

        page = context.pages[0] if context.pages else context.new_page()
        install_recorder(page)
        page.goto(ACTIVITY_URL, wait_until="domcontentloaded")

        # Re-install recorder on every new page created by navigation/popups.
        for pg in context.pages:
            install_recorder(pg)

        seen = 0

        try:
            while True:
                # If the active page changed, install the recorder there.
                current = context.pages[-1]
                try:
                    install_recorder(current)
                except Exception:
                    pass

                action = wait_for_manual_action(current, seen)
                seen += 1

                recorded_action = {
                    "kind": action.get("kind", "click"),
                    "selector": action.get("selector"),
                    "text": action.get("text"),
                    "tag": action.get("tag"),
                    "x": action.get("x"),
                    "y": action.get("y"),
                    "button": action.get("button"),
                    "scroll_x": action.get("scroll_x"),
                    "scroll_y": action.get("scroll_y"),
                    "url": action.get("url"),
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                }

                actions.append(recorded_action)
                DATA_FILE.write_text(
                    json.dumps(actions, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                kind = recorded_action["kind"].upper()
                detail = []
                if recorded_action.get("text"):
                    detail.append(f"text={recorded_action['text']!r}")
                if recorded_action.get("selector"):
                    detail.append(f"selector={recorded_action['selector']}")
                if recorded_action.get("x") is not None and recorded_action.get("y") is not None:
                    detail.append(f"x={recorded_action['x']} y={recorded_action['y']}")
                if recorded_action.get("scroll_x") is not None or recorded_action.get("scroll_y") is not None:
                    detail.append(
                        f"scroll_x={recorded_action.get('scroll_x', 0)} scroll_y={recorded_action.get('scroll_y', 0)}"
                    )

                print(f"[{len(actions)}] {kind} " + " ".join(detail))

        except KeyboardInterrupt:
            print(f"\nSaved {len(actions)} actions to {DATA_FILE.resolve()}")
        finally:
            context.close()


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
    Try to replay a recorded action.

    We prefer the original interaction shape when available.
    """
    kind = (action.get("kind") or "click").lower()
    selector = action.get("selector") or ""
    text = (action.get("text") or "").strip()
    x = action.get("x")
    y = action.get("y")

    if kind == "move":
        if x is None or y is None:
            return False
        page.mouse.move(float(x), float(y))
        return True

    if kind == "scroll":
        scroll_x = action.get("scroll_x")
        scroll_y = action.get("scroll_y")
        if selector:
            try:
                loc = page.locator(selector).first
                loc.evaluate(
                    """(el, values) => {
                        el.scrollLeft = values.scroll_x || 0;
                        el.scrollTop = values.scroll_y || 0;
                    }""",
                    {"scroll_x": scroll_x or 0, "scroll_y": scroll_y or 0},
                )
                return True
            except Exception:
                pass

        try:
            page.evaluate(
                """values => {
                    window.scrollTo(values.scroll_x || 0, values.scroll_y || 0);
                }""",
                {"scroll_x": scroll_x or 0, "scroll_y": scroll_y or 0},
            )
            return True
        except Exception:
            return False

    if kind != "click":
        return False

    if x is not None and y is not None:
        try:
            page.mouse.move(float(x), float(y))
            button_value = action.get("button")
            if button_value == 1:
                button = "middle"
            elif button_value == 2:
                button = "right"
            else:
                button = "left"
            page.mouse.click(float(x), float(y), button=button)
            return True
        except Exception:
            pass

    if selector:
        try:
            loc = page.locator(selector).first
            loc.wait_for(state="visible", timeout=2500)
            loc.click(timeout=2500)
            return True
        except Exception:
            pass

    if text:
        candidates = [
            page.get_by_role("button", name=text, exact=True).first,
            page.get_by_text(text, exact=True).first,
            page.locator(f"button:has-text({json.dumps(text)})").first,
            page.locator(f"a:has-text({json.dumps(text)})").first,
        ]

        for loc in candidates:
            try:
                loc.wait_for(state="visible", timeout=2000)
                loc.click(timeout=2500)
                return True
            except Exception:
                pass

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
            args=["--start-maximized"],
        )

        page = context.pages[0] if context.pages else context.new_page()

        print("Opening TicketPlus...")
        page.goto(ACTIVITY_URL, wait_until="domcontentloaded")

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

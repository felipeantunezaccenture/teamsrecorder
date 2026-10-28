"""Proof of concept: read recent Teams chats for ONE account via Playwright.

Standalone dev helper (not part of the Noted daemon). Read-only: it never sends,
edits or deletes anything.

Usage:
    pip install playwright && python -m playwright install chromium

    # 1) one-time login (visible window, MFA included); session is saved in the profile dir
    python tools/teams_chats_poc.py --account accenture --login

    # 2) read chats with activity in the last N minutes
    python tools/teams_chats_poc.py --account accenture --since-minutes 1440

    # if selectors stop matching, dump page + network traffic to tune them
    python tools/teams_chats_poc.py --account accenture --debug

The profile lives in %LOCALAPPDATA%\\noted_teams_poc\\<account> (outside the repo).
CSS selectors below are best guesses for the new Teams web client and are
UNVERIFIED; adjust the SELECTORS dict after a --debug run if needed.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

TEAMS_URL = "https://teams.microsoft.com/v2/"

SELECTORS = {
    "chat_tab": '[data-tid="app-bar-chat"], button[aria-label^="Chat"]',
    "chat_item": '[role="treeitem"][data-item-type="chat"]',
    "meeting_icon": '[data-testid="meeting-chat-icon"]',
    "message": '[data-tid="chat-pane-message"]',
    "author": '[data-tid="message-author-name"]',
    "body": '[data-tid="chat-pane-message"] [id^="content-"], [data-tid="message-body"]',
    "time": "time[datetime]",
}

# Chats whose only content is trivial acknowledgements are skipped (as in the spec).
TRIVIAL = re.compile(r"^(ok|okay|vale|gracias|thanks|thx|👍|🙏|[\W_]+)$", re.IGNORECASE)


def profile_dir(account: str) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "noted_teams_poc"
    path = base / account
    path.mkdir(parents=True, exist_ok=True)
    return path


def do_login(account: str) -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir(account)), channel="msedge", headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(TEAMS_URL)
        print("Log in to Teams in the window (MFA included). Waiting up to 5 min for chats to load...")
        try:
            page.wait_for_selector(SELECTORS["chat_tab"], timeout=300_000)
            page.wait_for_timeout(5_000)  # let the session cookies settle
        except PWTimeout:
            ctx.close()
            sys.exit("Login not detected within 5 minutes (or the chat_tab selector needs adjusting).")
        ctx.close()
    print(f"Session saved for '{account}'.")


def parse_dt(value: str):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def read_chats(account: str, since_minutes: int, max_chats: int, debug: bool) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    results = []
    captured = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir(account)), channel="msedge", headless=not debug)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if debug:
            page.on("response", lambda r: captured.append(r.url) if "/messages" in r.url or "chatsvc" in r.url else None)
        page.goto(TEAMS_URL)
        try:
            page.wait_for_selector(SELECTORS["chat_tab"], timeout=60_000)
        except PWTimeout:
            ctx.close()
            sys.exit("Teams did not load (not logged in?). Run again with --login.")
        page.click(SELECTORS["chat_tab"])
        try:
            page.wait_for_selector(SELECTORS["chat_item"], timeout=30_000)
        except PWTimeout:
            out = Path(os.environ.get("TEMP", ".")) / f"teams_poc_{account}"
            out.mkdir(exist_ok=True)
            (out / "page.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(out / "screen.png"))
            ctx.close()
            sys.exit(f"chat_item selector not found; page dumped in {out}")

        items = page.query_selector_all(SELECTORS["chat_item"])[:max_chats]
        print(f"{len(items)} chats in list", file=sys.stderr)
        prev_mids = None
        for idx in range(len(items)):
            # re-query each time: the list re-renders after every click
            item = page.query_selector_all(SELECTORS["chat_item"])[idx]
            if item.query_selector(SELECTORS["meeting_icon"]):
                continue  # meeting chats are out of scope, as in the spec
            title = (item.inner_text() or "").split("\n")[0].strip()
            item.click()
            # wait until the message pane actually switches to this chat (a fixed sleep
            # sometimes read the previous chat's messages)
            for _ in range(20):
                page.wait_for_timeout(250)
                mids = page.eval_on_selector_all(
                    SELECTORS["message"], "els => els.map(e => e.dataset.mid).join(',')"
                )
                if mids != prev_mids:
                    break
            page.wait_for_timeout(500)  # let the rest of the history render
            prev_mids = mids
            messages = []
            author = ""
            for m in page.query_selector_all(SELECTORS["message"]):
                # data-mid is the message timestamp in epoch ms; author/time/content
                # elements are siblings of the message node and carry the same id suffix
                mid = m.get_attribute("data-mid") or ""
                dt = datetime.fromtimestamp(int(mid) / 1000, tz=timezone.utc) if mid.isdigit() else None
                # Teams only prints the author on the first message of a consecutive run
                a = page.query_selector(f'[id="author-{mid}"]') if mid else None
                if a:
                    author = a.inner_text().strip()
                if dt and dt < cutoff:
                    continue
                content = m.query_selector(f'[id="content-{mid}"]')
                if not content:
                    continue  # no text body (image, card, system message)
                # text of the content only (no reactions), with quoted replies split out
                text, quoted = content.evaluate(
                    """el => {
                        const c = el.cloneNode(true);
                        const q = c.querySelector('[data-tid="quoted-reply-card"]');
                        const quoted = q ? q.textContent.trim().replace(/\\s+/g, ' ') : '';
                        c.querySelectorAll('[data-tid^="quoted-reply"]').forEach(n => n.remove());
                        return [c.textContent.trim().replace(/[ \\t]*\\n[ \\t\\n]*/g, '\\n'), quoted];
                    }"""
                )
                if not text:
                    continue
                msg = {"author": author, "time": dt.isoformat() if dt else "", "text": text}
                if quoted:
                    msg["reply_to"] = quoted
                messages.append(msg)
            if not messages:
                continue
            if all(TRIVIAL.match(m["text"]) for m in messages):
                continue
            results.append({"chat": title, "messages": messages})

        if debug:
            out = Path(os.environ.get("TEMP", ".")) / f"teams_poc_{account}"
            out.mkdir(exist_ok=True)
            (out / "page.html").write_text(page.content(), encoding="utf-8")
            (out / "network.json").write_text(json.dumps(captured, indent=2), encoding="utf-8")
            print(f"Debug dump in {out}", file=sys.stderr)
        ctx.close()
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", required=True, help="label for the profile, e.g. accenture or inditex")
    ap.add_argument("--login", action="store_true", help="one-time interactive login")
    ap.add_argument("--since-minutes", type=int, default=1440)
    ap.add_argument("--max-chats", type=int, default=20)
    ap.add_argument("--debug", action="store_true", help="visible browser + dump HTML/network")
    args = ap.parse_args()

    if args.login:
        do_login(args.account)
        return
    data = read_chats(args.account, args.since_minutes, args.max_chats, args.debug)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

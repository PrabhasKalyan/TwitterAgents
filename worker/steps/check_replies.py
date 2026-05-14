"""Scan x.com/messages for inbound replies from handles we've DM'd.

Approach:
  - For each handle with a sent DM whose `replied=0`, open the conversation at
    `x.com/messages/<handle>` (which redirects to the canonical thread if it exists).
  - Inspect the last message in the thread: if it was authored by THEM (not us)
    and timestamp > our most recent sent_at, mark replied=1 on the matching DM(s).
  - Always update reply_checked_at to throttle re-checks.
"""
import asyncio
import json
import os
import random
from datetime import datetime, timezone

from config import SEARCH_DELAY_MAX, SEARCH_DELAY_MIN
from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CREDS_PATH = os.environ.get("TW_CREDS", "/inputs/twitter_credentials.json")


def _load_cookies():
    if not os.path.exists(CREDS_PATH):
        raise RuntimeError(f"twitter_credentials.json not found at {CREDS_PATH}")
    with open(CREDS_PATH) as f:
        c = json.load(f)
    return [
        {"name": "auth_token", "value": c["auth_token"], "domain": ".x.com", "path": "/",
         "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": c["ct0"], "domain": ".x.com", "path": "/",
         "httpOnly": False, "secure": True, "sameSite": "Lax"},
    ]


async def _last_inbound_after(page, handle: str, after_iso: str) -> bool:
    """Open conversation; return True if there's an inbound message dated > after_iso."""
    url = f"https://x.com/messages/compose?recipient_id={handle}"
    # Fallback to direct thread URL via x.com/messages search:
    # Easier: just open x.com/messages and click the handle in the list.
    try:
        await page.goto("https://x.com/messages", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)
        # Find a conversation cell whose visible text contains the handle.
        clicked = await page.evaluate(
            """(h) => {
                const cells = Array.from(document.querySelectorAll('[data-testid="conversation"]'));
                for (const c of cells) {
                    const t = (c.innerText || '').toLowerCase();
                    if (t.includes('@' + h.toLowerCase())) {
                        c.click();
                        return true;
                    }
                }
                return false;
            }""",
            handle,
        )
        if not clicked:
            return False
        await page.wait_for_timeout(2500)

        info = await page.evaluate(
            """() => {
                // Each message row has time elements with datetime; outgoing rows live in
                // [data-testid='messageEntry'] containers. Detect direction via aria-label
                // or by checking if the message is on the right (our messages) vs left.
                const rows = Array.from(document.querySelectorAll('[data-testid="messageEntry"]'));
                if (rows.length === 0) return null;
                const last = rows[rows.length - 1];
                const time = last.querySelector('time[datetime]');
                const dt = time ? time.getAttribute('datetime') : null;
                // Heuristic: outgoing messages have a "Sent" or our-side bubble class.
                const aria = (last.getAttribute('aria-label') || '').toLowerCase();
                const outgoing = aria.includes('you sent') || aria.includes('sent at');
                return {dt, outgoing};
            }"""
        )
        if not info or not info.get("dt"):
            return False
        last_dt = datetime.fromisoformat(info["dt"].replace("Z", "+00:00"))
        after_dt = datetime.fromisoformat(after_iso.replace("Z", "+00:00")) if after_iso else None
        if info.get("outgoing"):
            return False
        if after_dt and last_dt <= after_dt:
            return False
        return True
    except Exception as e:
        log.warning(f"reply check failed for @{handle}: {e}")
        return False


async def _async_run():
    from playwright.async_api import async_playwright

    init_db()

    with connect() as conn:
        # Get distinct handles that have any sent DM not yet marked replied.
        rows = conn.execute(
            """SELECT twitter_handle, MAX(sent_at) AS last_sent_at
               FROM dms
               WHERE send_status='sent' AND replied=0 AND twitter_handle IS NOT NULL
               GROUP BY twitter_handle"""
        ).fetchall()

    total = len(rows)
    run_id = start_run("check_replies", total=total)
    log.info(f"check_replies: {total} threads to scan")
    if total == 0:
        update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
        return

    cookies = _load_cookies()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        await context.add_cookies(cookies)
        page = await context.new_page()

        for i, r in enumerate(rows, 1):
            handle = r["twitter_handle"]
            replied = await _last_inbound_after(page, handle, r["last_sent_at"])
            now_iso = datetime.now(timezone.utc).isoformat()
            with connect() as conn:
                if replied:
                    conn.execute(
                        "UPDATE dms SET replied=1, reply_checked_at=? "
                        "WHERE twitter_handle=? AND send_status='sent'",
                        (now_iso, handle),
                    )
                else:
                    conn.execute(
                        "UPDATE dms SET reply_checked_at=? "
                        "WHERE twitter_handle=? AND send_status='sent'",
                        (now_iso, handle),
                    )
                conn.commit()
            log.info(f"[{i}/{total}] @{handle} -> {'REPLIED' if replied else 'no reply'}")
            update_run(run_id, processed=i, log_tail=get_tail())
            await asyncio.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

        await browser.close()

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info("check_replies done")


def run():
    asyncio.run(_async_run())


if __name__ == "__main__":
    run()

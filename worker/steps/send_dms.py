"""Send approved DMs via Playwright. Respects daily 20-message cap."""
import asyncio
import json
import os
import random
import time
from datetime import datetime

from db import (connect, increment_send_count, init_db, start_run,
                todays_send_count, update_run)
from logger import get_logger, get_tail

log = get_logger()

CREDS_PATH = os.environ.get("TW_CREDS", "/inputs/twitter_credentials.json")
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/data/screenshots")
DAILY_LIMIT = 20


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


async def _screenshot(page, handle: str, tag: str = "fail") -> str:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SCREENSHOT_DIR, f"{handle}_{tag}_{ts}.png")
    try:
        await page.screenshot(path=path, full_page=True)
    except Exception:
        return ""
    return path


async def _send_one(page, handle: str, text: str) -> tuple[bool, str, str]:
    """Returns (success, error_msg, screenshot_path)."""
    try:
        await page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)

        clicked = False
        try:
            btn = page.locator('[data-testid="sendDMFromProfile"]')
            await btn.wait_for(state="visible", timeout=8000)
            await btn.click()
            clicked = True
        except Exception:
            pass

        if not clicked:
            try:
                msg_btn = page.get_by_role("button", name="Message")
                await msg_btn.first.click(timeout=5000)
                clicked = True
            except Exception:
                pass

        if not clicked:
            shot = await _screenshot(page, handle, "no_dm_button")
            return False, "DM button not found (likely DMs closed)", shot

        compose = page.locator('[data-testid="dmComposerTextInput"]')
        await compose.wait_for(state="visible", timeout=10000)
        await compose.click()
        # Type as a human would
        for chunk in [text[i:i+8] for i in range(0, len(text), 8)]:
            await page.keyboard.type(chunk)
            await page.wait_for_timeout(random.randint(40, 110))

        await page.wait_for_timeout(700)
        send_btn = page.locator('[data-testid="dmComposerSendButton"]')
        await send_btn.wait_for(state="visible", timeout=5000)
        await send_btn.click()
        await page.wait_for_timeout(2500)

        # Confirm send by checking compose box cleared OR an error toast appeared
        try:
            val = await compose.input_value()
        except Exception:
            val = ""
        if val:
            shot = await _screenshot(page, handle, "post_send")
            return False, "Compose box still has text after clicking send", shot

        return True, "", ""
    except Exception as e:
        shot = await _screenshot(page, handle, "exception")
        return False, f"{type(e).__name__}: {e}", shot


async def _async_run(dry_run: bool = False):
    from playwright.async_api import async_playwright

    init_db()

    sent_today = todays_send_count()
    if sent_today >= DAILY_LIMIT:
        log.warning(f"Daily limit reached ({sent_today}/{DAILY_LIMIT}). Nothing to do.")
        return

    remaining = DAILY_LIMIT - sent_today

    with connect() as conn:
        # Exclude handles already sent
        rows = conn.execute(
            """SELECT id, twitter_handle, dm_text, company_name, founder_name
               FROM dms
               WHERE review_status = 'approved'
                 AND send_status != 'sent'
                 AND twitter_handle IS NOT NULL
                 AND twitter_handle NOT IN (SELECT twitter_handle FROM dms WHERE send_status = 'sent')
               ORDER BY id ASC
               LIMIT ?""",
            (remaining,),
        ).fetchall()

    total = len(rows)
    run_id = start_run("send", total=total)
    log.info(f"Sending up to {total} DMs (already sent today: {sent_today}/{DAILY_LIMIT}). dry_run={dry_run}")

    if dry_run:
        for r in rows:
            log.info(f"  [DRY] @{r['twitter_handle']} ({r['founder_name']} / {r['company_name']}):")
            log.info(f"        {r['dm_text']}")
        update_run(run_id, processed=total, status="completed", log_tail=get_tail(), finished=True)
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
            # Re-check daily cap each iteration in case other instances ran
            if todays_send_count() >= DAILY_LIMIT:
                log.warning("Daily cap hit mid-run, stopping cleanly")
                update_run(run_id, processed=i - 1, status="rate_limited", log_tail=get_tail(), finished=True)
                await browser.close()
                return

            log.info(f"[{i}/{total}] sending to @{r['twitter_handle']}")
            ok, err, shot = await _send_one(page, r["twitter_handle"], r["dm_text"])

            with connect() as conn:
                if ok:
                    conn.execute(
                        "UPDATE dms SET send_status='sent', sent_at=CURRENT_TIMESTAMP WHERE id = ?",
                        (r["id"],),
                    )
                    conn.commit()
                    increment_send_count()
                    log.info(f"  ✓ sent to @{r['twitter_handle']}")
                else:
                    conn.execute(
                        "UPDATE dms SET send_status='failed', error_msg=?, screenshot_path=? WHERE id = ?",
                        (err, shot, r["id"]),
                    )
                    conn.commit()
                    log.error(f"  ✗ failed @{r['twitter_handle']}: {err}")

            update_run(run_id, processed=i, log_tail=get_tail())

            if i < total:
                delay = random.uniform(50, 100)
                log.info(f"  sleeping {delay:.1f}s")
                await asyncio.sleep(delay)

        await browser.close()

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info("send_dms done")


def run(dry_run: bool = False):
    asyncio.run(_async_run(dry_run=dry_run))


if __name__ == "__main__":
    run()

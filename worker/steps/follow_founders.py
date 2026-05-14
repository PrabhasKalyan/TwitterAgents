"""Send Follow requests on x.com to every verified founder we haven't followed yet.

Runs independently of find_handles. Picks every row from `founders` where
  handle_status='found' AND twitter_handle IS NOT NULL
  AND (followed_at IS NULL OR follow_status != 'followed')
and clicks Follow on the founder's profile.

Idempotent: once `followed_at` is set, the row is skipped on subsequent runs.
Re-runs will retry rows that previously failed.
"""
import asyncio
import json
import os
import random

from playwright.async_api import async_playwright

from config import SEARCH_DELAY_MAX, SEARCH_DELAY_MIN
from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CREDS_PATH = os.environ.get("TW_CREDS", "/inputs/twitter_credentials.json")


def _load_cookies():
    if not os.path.exists(CREDS_PATH):
        log.warning(f"No twitter_credentials.json at {CREDS_PATH}")
        return None
    with open(CREDS_PATH) as f:
        c = json.load(f)
    return [
        {"name": "auth_token", "value": c["auth_token"], "domain": ".x.com", "path": "/",
         "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": c["ct0"], "domain": ".x.com", "path": "/",
         "httpOnly": False, "secure": True, "sameSite": "Lax"},
    ]


async def _safe_eval(page, js: str, default):
    try:
        return await page.evaluate(js)
    except Exception as e:
        msg = str(e)
        if "Execution context was destroyed" in msg or "navigation" in msg.lower():
            log.warning("  eval lost context (page navigated); skipping")
            return default
        raise


async def _follow_handle(page, handle: str) -> tuple[bool, str]:
    """Returns (ok, status) where status is 'followed' | 'failed' | 'missing'."""
    try:
        await page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=25000)
        # Wait for the React-rendered profile header to show either a follow or unfollow button.
        scope = page.locator('[data-testid="primaryColumn"]')
        follow_btn = scope.locator('[data-testid$="-follow"]').first
        unfollow_btn = scope.locator('[data-testid$="-unfollow"]').first

        try:
            await page.wait_for_selector(
                '[data-testid="primaryColumn"] [data-testid$="-follow"], '
                '[data-testid="primaryColumn"] [data-testid$="-unfollow"]',
                timeout=12000,
            )
        except Exception:
            # Account might be suspended / deactivated / blocked.
            body_txt = ""
            try:
                body_txt = (await page.locator("body").inner_text(timeout=2000))[:400].lower()
            except Exception:
                pass
            if any(k in body_txt for k in ("account suspended", "doesn't exist", "this account doesn")):
                return False, "missing"
            return False, "failed"

        if await unfollow_btn.count() > 0:
            return True, "followed"  # already following — record as done

        if await follow_btn.count() > 0:
            await follow_btn.click(timeout=5000)
            # Confirm the toggle flipped to unfollow.
            try:
                await scope.locator('[data-testid$="-unfollow"]').first.wait_for(timeout=5000)
                return True, "followed"
            except Exception:
                return False, "failed"

        return False, "failed"
    except Exception as e:
        log.warning(f"follow failed for @{handle}: {type(e).__name__}: {e}")
        return False, "failed"


def _pending_targets(limit: int | None) -> list[dict]:
    q = (
        "SELECT id, twitter_handle, founder_name FROM founders "
        "WHERE handle_status='found' AND twitter_handle IS NOT NULL "
        "AND (followed_at IS NULL OR follow_status != 'followed') "
        "ORDER BY id ASC"
    )
    args: tuple = ()
    if limit:
        q += " LIMIT ?"
        args = (limit,)
    with connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def _mark(founder_id: int, ok: bool, status: str):
    with connect() as conn:
        if ok:
            conn.execute(
                "UPDATE founders SET follow_status=?, followed_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, founder_id),
            )
        else:
            conn.execute(
                "UPDATE founders SET follow_status=? WHERE id=?",
                (status, founder_id),
            )
        conn.commit()


async def _async_run(limit: int | None = None):
    init_db()
    targets = _pending_targets(limit)
    total = len(targets)
    run_id = start_run("follow_founders", total=total)
    log.info(f"follow_founders: {total} founders pending")
    if total == 0:
        update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
        return

    cookies = _load_cookies()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        if cookies:
            await context.add_cookies(cookies)
        page = await context.new_page()

        followed = 0
        failed = 0
        for i, t in enumerate(targets, 1):
            handle = t["twitter_handle"]
            ok, status = await _follow_handle(page, handle)
            _mark(t["id"], ok, status)
            if ok:
                followed += 1
                log.info(f"  [{i}/{total}] @{handle} -> {status}")
            else:
                failed += 1
                log.info(f"  [{i}/{total}] @{handle} -> {status}")
            update_run(run_id, processed=i, log_tail=get_tail())
            await asyncio.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

        await browser.close()

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info(f"follow_founders done: {followed} followed, {failed} failed")


def run(limit: int | None = None):
    asyncio.run(_async_run(limit=limit))


if __name__ == "__main__":
    run()

"""Visit each found handle's profile, record last-tweet timestamp + activity bucket."""
import asyncio
import json
import os
import random
from datetime import datetime, timezone

from config import (ACTIVITY_ACTIVE_DAYS, ACTIVITY_DORMANT_DAYS,
                    ACTIVITY_SEMI_DAYS, SEARCH_DELAY_MAX, SEARCH_DELAY_MIN)
from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CREDS_PATH = os.environ.get("TW_CREDS", "/inputs/twitter_credentials.json")


def _load_cookies():
    if not os.path.exists(CREDS_PATH):
        return None
    with open(CREDS_PATH) as f:
        c = json.load(f)
    return [
        {"name": "auth_token", "value": c["auth_token"], "domain": ".x.com", "path": "/",
         "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": c["ct0"], "domain": ".x.com", "path": "/",
         "httpOnly": False, "secure": True, "sameSite": "Lax"},
    ]


def _bucket(days_since: float | None) -> str:
    if days_since is None:
        return "unknown"
    if days_since <= ACTIVITY_ACTIVE_DAYS:
        return "active"
    if days_since <= ACTIVITY_SEMI_DAYS:
        return "semi_active"
    if days_since <= ACTIVITY_DORMANT_DAYS:
        return "dormant"
    return "dead"


async def _profile_signals(page, handle: str) -> dict:
    """Return {last_tweet_at, tweet_count_recent, followers, following}."""
    await page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=25000)
    await page.wait_for_timeout(2500)

    data = await page.evaluate(
        """() => {
            const out = {last_tweet_iso: null, tweet_count_recent: 0,
                        followers: null, following: null};
            // Grab tweet datetime values (skip pinned by ignoring first if it's flagged).
            const times = Array.from(document.querySelectorAll('article time[datetime]'));
            const dates = times.map(t => t.getAttribute('datetime')).filter(Boolean);
            // Most-recent (max) datetime.
            if (dates.length) {
                out.last_tweet_iso = dates.sort().slice(-1)[0];
                // Count visible tweet articles as a rough recent-activity signal.
                out.tweet_count_recent = times.length;
            }
            // Followers / Following links use href patterns.
            const links = Array.from(document.querySelectorAll('a[href$="/verified_followers"], a[href$="/followers"], a[href$="/following"]'));
            for (const a of links) {
                const txt = (a.innerText || '').trim();
                const num = txt.replace(/[^0-9KMkm.]/g, '');
                const parse = (s) => {
                    if (!s) return null;
                    const lower = s.toLowerCase();
                    let n = parseFloat(lower);
                    if (isNaN(n)) return null;
                    if (lower.endsWith('k')) n *= 1000;
                    if (lower.endsWith('m')) n *= 1000000;
                    return Math.round(n);
                };
                if (a.href.includes('/followers') || a.href.includes('/verified_followers')) {
                    out.followers = parse(num);
                } else if (a.href.endsWith('/following')) {
                    out.following = parse(num);
                }
            }
            return out;
        }"""
    )
    return data


async def _async_run(founder_ids: list[int] | None = None):
    from playwright.async_api import async_playwright

    init_db()

    with connect() as conn:
        if founder_ids:
            placeholders = ",".join("?" * len(founder_ids))
            rows = conn.execute(
                f"SELECT id, twitter_handle FROM founders "
                f"WHERE id IN ({placeholders}) AND handle_status='found' AND twitter_handle IS NOT NULL",
                founder_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT f.id, f.twitter_handle FROM founders f "
                "LEFT JOIN founder_activity a ON a.founder_id = f.id "
                "WHERE f.handle_status='found' AND f.twitter_handle IS NOT NULL "
                "AND a.founder_id IS NULL"
            ).fetchall()

    total = len(rows)
    run_id = start_run("check_activity", total=total)
    log.info(f"check_activity: {total} profiles")

    if total == 0:
        update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
        return

    cookies = _load_cookies()
    now = datetime.now(timezone.utc)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        if cookies:
            await context.add_cookies(cookies)
        page = await context.new_page()

        for i, r in enumerate(rows, 1):
            handle = r["twitter_handle"]
            try:
                sig = await _profile_signals(page, handle)
            except Exception as e:
                log.warning(f"  @{handle}: signal extract failed: {e}")
                sig = {"last_tweet_iso": None, "tweet_count_recent": 0,
                       "followers": None, "following": None}

            last_iso = sig.get("last_tweet_iso")
            days_since = None
            last_dt = None
            if last_iso:
                try:
                    last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
                    days_since = (now - last_dt).total_seconds() / 86400.0
                except Exception:
                    pass
            score = _bucket(days_since)

            with connect() as conn:
                conn.execute(
                    """INSERT INTO founder_activity
                         (founder_id, last_tweet_at, tweet_count_recent, followers, following,
                          activity_score, checked_at)
                       VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(founder_id) DO UPDATE SET
                         last_tweet_at=excluded.last_tweet_at,
                         tweet_count_recent=excluded.tweet_count_recent,
                         followers=excluded.followers,
                         following=excluded.following,
                         activity_score=excluded.activity_score,
                         checked_at=CURRENT_TIMESTAMP""",
                    (r["id"], last_dt.isoformat() if last_dt else None,
                     sig.get("tweet_count_recent") or 0,
                     sig.get("followers"), sig.get("following"), score),
                )
                conn.commit()

            log.info(f"[{i}/{total}] @{handle} -> {score} "
                     f"(last_tweet: {last_iso or 'n/a'}, followers: {sig.get('followers')})")
            update_run(run_id, processed=i, log_tail=get_tail())
            await asyncio.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

        await browser.close()

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info("check_activity done")


def run(founder_ids: list[int] | None = None):
    asyncio.run(_async_run(founder_ids=founder_ids))


if __name__ == "__main__":
    run()

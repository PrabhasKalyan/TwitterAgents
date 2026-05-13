"""Scrape YC page for founder names, then search x.com for handles."""
import asyncio
import json
import os
import random
import re
import time

from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CREDS_PATH = os.environ.get("TW_CREDS", "/inputs/twitter_credentials.json")


def _load_cookies():
    if not os.path.exists(CREDS_PATH):
        log.warning(f"No twitter_credentials.json at {CREDS_PATH} — search may return fewer results")
        return None
    with open(CREDS_PATH) as f:
        c = json.load(f)
    return [
        {"name": "auth_token", "value": c["auth_token"], "domain": ".x.com", "path": "/",
         "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": c["ct0"], "domain": ".x.com", "path": "/",
         "httpOnly": False, "secure": True, "sameSite": "Lax"},
    ]


async def _extract_founders_from_yc(page, yc_url: str):
    """Return list of founder names from a YC company page."""
    try:
        await page.goto(yc_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        # YC founder cards typically appear as "<Name>, Founder" or under "Founders" section
        text = await page.content()
        # Heuristic: look for <h3> in the founder section
        names = await page.evaluate(
            """() => {
                const out = [];
                // Look for headings that look like founder names
                document.querySelectorAll('h3, .font-bold, [class*="founder"]').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t && t.split(' ').length >= 2 && t.split(' ').length <= 5 && !/[0-9]/.test(t)) {
                        out.push(t);
                    }
                });
                return out;
            }"""
        )
        # Dedupe, light filtering
        seen = set()
        out = []
        for n in names:
            n = n.strip()
            if n in seen or len(n) > 60 or len(n) < 4:
                continue
            # Filter obvious non-name strings
            low = n.lower()
            if any(w in low for w in ["founder", "team", "company", "about", "active", "hiring", "jobs"]):
                continue
            seen.add(n)
            out.append(n)
        return out[:5]
    except Exception as e:
        log.warning(f"YC scrape failed for {yc_url}: {e}")
        return []


async def _search_handle(page, founder_name: str, company_name: str) -> str | None:
    """Search x.com for a user, return @handle or None."""
    query = f"{founder_name} {company_name}".replace(" ", "+")
    url = f"https://x.com/search?q={query}&f=user"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(random.uniform(2000, 3500))
        # The first user cell has a screen_name link
        handle = await page.evaluate(
            """() => {
                const cells = document.querySelectorAll('[data-testid="UserCell"] a[href^="/"]');
                for (const a of cells) {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/^\\/([A-Za-z0-9_]{2,15})$/);
                    if (m) return m[1];
                }
                return null;
            }"""
        )
        return handle
    except Exception as e:
        log.warning(f"search failed for {founder_name} ({company_name}): {e}")
        return None


async def _async_run():
    from playwright.async_api import async_playwright

    init_db()

    limit = int(os.environ.get("FIND_HANDLES_LIMIT", "0"))
    limit_sql = f" LIMIT {limit}" if limit > 0 else ""

    with connect() as conn:
        companies = conn.execute(
            "SELECT id, name, yc_url FROM companies WHERE filtered_in = 1 "
            "AND id NOT IN (SELECT DISTINCT company_id FROM founders WHERE company_id IS NOT NULL)"
            f"{limit_sql}"
        ).fetchall()

    total = len(companies)
    run_id = start_run("find_handles", total=total)
    log.info(f"Looking up founders for {total} companies")

    cookies = _load_cookies()

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

        for i, comp in enumerate(companies, 1):
            log.info(f"[{i}/{total}] {comp['name']}")
            founders = []
            if comp["yc_url"]:
                founders = await _extract_founders_from_yc(page, comp["yc_url"])
                log.info(f"  found {len(founders)} candidate founder name(s)")

            if not founders:
                # Insert a placeholder so we don't re-process
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO founders (company_id, company_name, founder_name, handle_status) "
                        "VALUES (?, ?, ?, 'not_found')",
                        (comp["id"], comp["name"], ""),
                    )
                    conn.commit()
            else:
                for fname in founders:
                    handle = await _search_handle(page, fname, comp["name"])
                    status = "found" if handle else "not_found"
                    with connect() as conn:
                        conn.execute(
                            "INSERT INTO founders (company_id, company_name, founder_name, twitter_handle, handle_status) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (comp["id"], comp["name"], fname, handle, status),
                        )
                        conn.commit()
                    log.info(f"  {fname} -> @{handle if handle else '(not found)'}")
                    await asyncio.sleep(random.uniform(2.0, 4.0))

            update_run(run_id, processed=i, log_tail=get_tail())

        await browser.close()

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info("find_handles done")


def run():
    asyncio.run(_async_run())


if __name__ == "__main__":
    run()

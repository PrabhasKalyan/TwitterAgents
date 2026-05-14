"""Find Twitter handles via DuckDuckGo first, then x.com search as fallback.

Per founder:
  1. Scrape YC page for founder names (Playwright).
  2. For each name, query DDG HTML for `"<name>" "<company>" site:x.com OR site:twitter.com`.
  3. Parse top hits, extract candidate handles. Pick the best.
  4. Verify the handle's profile loads on x.com.
  5. If DDG finds nothing, fall back to x.com user search.
"""
import asyncio
import html
import json
import os
import random
import re
import urllib.parse

import httpx

from config import SEARCH_DELAY_MAX, SEARCH_DELAY_MIN
from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CREDS_PATH = os.environ.get("TW_CREDS", "/inputs/twitter_credentials.json")

DDG_URL = "https://html.duckduckgo.com/html/"
HANDLE_RE = re.compile(r"(?:x\.com|twitter\.com)/(?!i/|search|home|messages|notifications|explore|hashtag|intent|share)([A-Za-z0-9_]{2,15})(?:/|$|\?)")
RESERVED_HANDLES = {"home", "explore", "search", "messages", "notifications", "i",
                    "intent", "share", "hashtag", "compose", "settings", "login",
                    "signup", "tos", "privacy", "about", "jobs", "press"}


def _load_cookies():
    if not os.path.exists(CREDS_PATH):
        log.warning(f"No twitter_credentials.json at {CREDS_PATH} — fallback search may fail")
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
    try:
        await page.goto(yc_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        names = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('h3, .font-bold, [class*="founder"]').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t && t.split(' ').length >= 2 && t.split(' ').length <= 5 && !/[0-9]/.test(t)) {
                        out.push(t);
                    }
                });
                return out;
            }"""
        )
        seen, out = set(), []
        for n in names:
            n = n.strip()
            if n in seen or len(n) > 60 or len(n) < 4:
                continue
            low = n.lower()
            if any(w in low for w in ["founder", "team", "company", "about", "active", "hiring", "jobs"]):
                continue
            seen.add(n)
            out.append(n)
        return out[:5]
    except Exception as e:
        log.warning(f"YC scrape failed for {yc_url}: {e}")
        return []


def _ddg_search(query: str) -> list[str]:
    """POST to DDG HTML, return candidate handles in order of appearance."""
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0"}) as c:
            r = c.post(DDG_URL, data={"q": query})
            r.raise_for_status()
            body = r.text
    except Exception as e:
        log.warning(f"DDG query failed: {e}")
        return []

    # DDG wraps result links in /l/?uddg=<encoded>. Pull and decode.
    handles, seen = [], set()
    for m in re.finditer(r'href="(/l/\?[^"]*uddg=([^&"]+)[^"]*)"', body):
        raw = urllib.parse.unquote(m.group(2))
        # Also handle plaintext links (some DDG variants)
        for hm in HANDLE_RE.finditer(raw):
            h = hm.group(1)
            if h.lower() in RESERVED_HANDLES or h in seen:
                continue
            seen.add(h)
            handles.append(h)
    # Also scan body directly in case redirect wasn't matched
    for hm in HANDLE_RE.finditer(html.unescape(body)):
        h = hm.group(1)
        if h.lower() in RESERVED_HANDLES or h in seen:
            continue
        seen.add(h)
        handles.append(h)
    return handles[:10]


async def _verify_handle(page, handle: str, founder_name: str, company_name: str) -> bool:
    """Load x.com/<handle>; check it exists and bio/name plausibly matches."""
    try:
        await page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(1800)
        text = (await page.evaluate("() => document.body.innerText || ''"))[:8000].lower()
        if "this account doesn’t exist" in text or "this account doesn't exist" in text:
            return False
        if "account suspended" in text:
            return False
        # Plausibility: name parts OR company name appears
        fname_parts = [p for p in founder_name.lower().split() if len(p) >= 3]
        if any(p in text for p in fname_parts):
            return True
        if company_name and company_name.lower() in text:
            return True
        # Profile loaded but no match → still accept if it has a UserName cell (might be obscure)
        has_user = await page.locator('[data-testid="UserName"]').count()
        return has_user > 0
    except Exception as e:
        log.warning(f"verify failed for @{handle}: {e}")
        return False


async def _x_fallback_search(page, founder_name: str, company_name: str) -> str | None:
    query = f"{founder_name} {company_name}".replace(" ", "+")
    url = f"https://x.com/search?q={query}&f=user"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(random.uniform(2000, 3500))
        return await page.evaluate(
            """() => {
                const cells = document.querySelectorAll('[data-testid="UserCell"] a[href^="/"]');
                for (const a of cells) {
                    const m = (a.getAttribute('href') || '').match(/^\\/([A-Za-z0-9_]{2,15})$/);
                    if (m) return m[1];
                }
                return null;
            }"""
        )
    except Exception as e:
        log.warning(f"x fallback search failed for {founder_name}: {e}")
        return None


async def _find_one(page, founder_name: str, company_name: str) -> tuple[str | None, str]:
    """Return (handle, source) where source in {'ddg','x_fallback',''}."""
    query = f'"{founder_name}" "{company_name}" (site:x.com OR site:twitter.com)'
    candidates = _ddg_search(query)
    await asyncio.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    if not candidates:
        # Try looser query
        candidates = _ddg_search(f'"{founder_name}" {company_name} site:x.com')
        await asyncio.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    for h in candidates[:5]:
        if await _verify_handle(page, h, founder_name, company_name):
            return h, "ddg"
        await asyncio.sleep(random.uniform(1.0, 2.0))

    handle = await _x_fallback_search(page, founder_name, company_name)
    if handle and await _verify_handle(page, handle, founder_name, company_name):
        return handle, "x_fallback"
    return None, ""


async def _async_run(company_ids: list[int] | None = None):
    """If company_ids is provided, process only those. Otherwise process all unhandled."""
    from playwright.async_api import async_playwright

    init_db()

    with connect() as conn:
        if company_ids:
            placeholders = ",".join("?" * len(company_ids))
            companies = conn.execute(
                f"SELECT id, name, yc_url FROM companies WHERE id IN ({placeholders})",
                company_ids,
            ).fetchall()
        else:
            companies = conn.execute(
                "SELECT id, name, yc_url FROM companies WHERE filtered_in = 1 "
                "AND id NOT IN (SELECT DISTINCT company_id FROM founders WHERE company_id IS NOT NULL)"
            ).fetchall()

    total = len(companies)
    run_id = start_run("find_handles", total=total)
    log.info(f"find_handles: {total} companies (DDG-first)")

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
                log.info(f"  YC: {len(founders)} candidate founder name(s)")

            if not founders:
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO founders (company_id, company_name, founder_name, handle_status) "
                        "VALUES (?, ?, ?, 'not_found')",
                        (comp["id"], comp["name"], ""),
                    )
                    conn.commit()
            else:
                for fname in founders:
                    handle, source = await _find_one(page, fname, comp["name"])
                    status = "found" if handle else "not_found"
                    with connect() as conn:
                        conn.execute(
                            "INSERT INTO founders (company_id, company_name, founder_name, "
                            "twitter_handle, handle_status, search_source) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (comp["id"], comp["name"], fname, handle, status, source or None),
                        )
                        conn.commit()
                    badge = f"@{handle} ({source})" if handle else "(not found)"
                    log.info(f"  {fname} -> {badge}")
                    await asyncio.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

            update_run(run_id, processed=i, log_tail=get_tail())

        await browser.close()

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info("find_handles done")


def run(company_ids: list[int] | None = None):
    asyncio.run(_async_run(company_ids=company_ids))


if __name__ == "__main__":
    run()

"""Find Twitter handles. Strategy in priority order:

  1. **YC page direct links** — most YC company pages embed founder X/Twitter links
     right on the founder card. Cheapest, highest-confidence signal.
  2. **Bing search via Playwright** — render bing.com/search in the real browser
     and pull twitter.com/x.com result URLs. Far more permissive than DDG-HTML
     (which 403s from cloud IPs).
  3. **x.com user search** — last resort if both above fail.

After a handle is verified, optionally click Follow on x.com.
"""
import asyncio
import json
import os
import random
import re
import urllib.parse

from config import FOLLOW_AFTER_VERIFY, SEARCH_DELAY_MAX, SEARCH_DELAY_MIN
from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CREDS_PATH = os.environ.get("TW_CREDS", "/inputs/twitter_credentials.json")

HANDLE_RE = re.compile(
    r"(?:x\.com|twitter\.com)/(?!i/|search|home|messages|notifications|explore|hashtag|"
    r"intent|share|compose|settings|login|signup|tos|privacy|about|jobs|press)"
    r"([A-Za-z0-9_]{2,15})(?:/|$|\?|#)"
)
RESERVED_HANDLES = {"home", "explore", "search", "messages", "notifications", "i",
                    "intent", "share", "hashtag", "compose", "settings", "login",
                    "signup", "tos", "privacy", "about", "jobs", "press",
                    "status", "ycombinator"}

# Words that signal a non-name string (pitch-deck headers, navigation, etc.)
NAME_BLACKLIST_SUBSTR = [
    "founder", "team", "company", "about", "active", "hiring", "jobs",
    "solution", "problem", "news", "latest", "make something", "the team",
    "our ", "the problem", "the solution", "contact", "demo", "pitch",
    "subscribe", "newsletter", "investor", "press", "what we",
]


def _load_cookies():
    if not os.path.exists(CREDS_PATH):
        log.warning(f"No twitter_credentials.json at {CREDS_PATH} — cannot use x.com")
        return None
    with open(CREDS_PATH) as f:
        c = json.load(f)
    return [
        {"name": "auth_token", "value": c["auth_token"], "domain": ".x.com", "path": "/",
         "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": c["ct0"], "domain": ".x.com", "path": "/",
         "httpOnly": False, "secure": True, "sameSite": "Lax"},
    ]


def _looks_like_name(s: str) -> bool:
    s = s.strip()
    if len(s) < 4 or len(s) > 60:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    low = s.lower()
    if any(w in low for w in NAME_BLACKLIST_SUBSTR):
        return False
    # Must contain at least one ASCII letter group
    if not re.search(r"[A-Za-z]{2,}", s):
        return False
    # Strip leading emojis / punctuation, require ≥2 alpha tokens
    clean = re.sub(r"[^\w\s\-']", " ", s).strip()
    parts = [p for p in clean.split() if any(c.isalpha() for c in p)]
    if not (2 <= len(parts) <= 5):
        return False
    # Each token should start with a letter
    if not all(p[0].isalpha() for p in parts):
        return False
    return True


async def _scrape_yc_founders(page, yc_url: str) -> list[dict]:
    """Return [{name, twitter_handle | None}] extracted from YC founder cards."""
    try:
        await page.goto(yc_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1800)
        data = await page.evaluate(
            """() => {
                // Find sections/blocks that look like founder cards.
                const out = [];
                const containers = Array.from(document.querySelectorAll(
                    '[class*="founder"], [class*="Founder"], section, div'
                ));
                const seen = new Set();
                for (const el of containers) {
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 600) continue;
                    // Look inside for a Twitter/X link
                    const a = el.querySelector('a[href*="twitter.com/"], a[href*="x.com/"]');
                    if (!a) continue;
                    const href = a.getAttribute('href') || '';
                    // Try to find a name above the link: the first short text line in this block.
                    const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
                    let candidate = '';
                    for (const line of lines) {
                        if (line.length >= 4 && line.length <= 60 && /[A-Za-z]/.test(line)) {
                            candidate = line;
                            break;
                        }
                    }
                    const key = candidate + '|' + href;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({ name: candidate, href });
                }
                // Fallback: collect generic h3 names if no founder cards were found.
                if (out.length === 0) {
                    document.querySelectorAll('h3, .font-bold').forEach(el => {
                        const t = (el.innerText || '').trim();
                        if (t) out.push({ name: t, href: null });
                    });
                }
                return out;
            }"""
        )

        results = []
        seen_names, seen_handles = set(), set()
        for item in data:
            name = (item.get("name") or "").strip()
            href = item.get("href") or ""
            handle = None
            if href:
                m = HANDLE_RE.search(href)
                if m:
                    h = m.group(1)
                    if h.lower() not in RESERVED_HANDLES:
                        handle = h
            if name and not _looks_like_name(name):
                # If name is junk but we have a handle, keep the handle with an empty name.
                if not handle:
                    continue
                name = ""
            if not name and not handle:
                continue
            key_n = name.lower()
            if name and key_n in seen_names:
                continue
            if handle and handle in seen_handles:
                continue
            if name:
                seen_names.add(key_n)
            if handle:
                seen_handles.add(handle)
            results.append({"name": name, "twitter_handle": handle})

        return results[:6]
    except Exception as e:
        log.warning(f"YC scrape failed for {yc_url}: {e}")
        return []


async def _bing_search(page, query: str) -> list[str]:
    """Render bing.com/search in Playwright; extract twitter/x handles from results."""
    url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(random.uniform(1200, 2000))
        hrefs = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.href).filter(h => /(?:x|twitter)\\.com\\//i.test(h))"""
        )
    except Exception as e:
        log.warning(f"Bing query failed: {e}")
        return []

    out, seen = [], set()
    for href in hrefs:
        m = HANDLE_RE.search(href)
        if not m:
            continue
        h = m.group(1)
        if h.lower() in RESERVED_HANDLES or h in seen:
            continue
        seen.add(h)
        out.append(h)
    return out[:8]


async def _verify_handle(page, handle: str, founder_name: str, company_name: str) -> bool:
    try:
        await page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(1800)
        text = (await page.evaluate("() => document.body.innerText || ''"))[:8000].lower()
        if "this account doesn’t exist" in text or "this account doesn't exist" in text:
            return False
        if "account suspended" in text:
            return False
        name_parts = [p for p in (founder_name or "").lower().split() if len(p) >= 3]
        if name_parts and any(p in text for p in name_parts):
            return True
        if company_name and company_name.lower() in text:
            return True
        has_user = await page.locator('[data-testid="UserName"]').count()
        return has_user > 0
    except Exception as e:
        log.warning(f"verify failed for @{handle}: {e}")
        return False


async def _follow_handle(page, handle: str) -> bool:
    """Click the Follow button if not already following. Assumes profile is loaded."""
    try:
        # If we just verified, we're already on the profile. Otherwise navigate.
        if f"/{handle}" not in page.url:
            await page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
        # Try the Follow button (NOT 'Following' which appears post-follow).
        clicked = await page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('[data-testid$="-follow"], [role="button"]'));
                for (const b of btns) {
                    const txt = (b.innerText || '').trim();
                    if (txt === 'Follow') { b.click(); return true; }
                }
                return false;
            }"""
        )
        if clicked:
            await page.wait_for_timeout(1200)
        return bool(clicked)
    except Exception as e:
        log.warning(f"follow failed for @{handle}: {e}")
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
    """Return (handle, source). Source: 'yc_direct'|'bing'|'x_fallback'|''."""
    # Bing first: query name + company
    if founder_name:
        for query in [
            f'"{founder_name}" "{company_name}" (site:x.com OR site:twitter.com)',
            f'"{founder_name}" {company_name} site:x.com',
            f'{founder_name} {company_name} twitter',
        ]:
            cands = await _bing_search(page, query)
            await asyncio.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
            for h in cands[:5]:
                if await _verify_handle(page, h, founder_name, company_name):
                    return h, "bing"
                await asyncio.sleep(random.uniform(1.0, 2.0))
            if cands:
                break  # Found candidates but none verified — don't keep hammering bing

    handle = await _x_fallback_search(page, founder_name, company_name)
    if handle and await _verify_handle(page, handle, founder_name, company_name):
        return handle, "x_fallback"
    return None, ""


async def _async_run(company_ids: list[int] | None = None):
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
    log.info(f"find_handles: {total} companies (YC-direct → Bing → x.com fallback)")

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
            yc_results = []
            if comp["yc_url"]:
                yc_results = await _scrape_yc_founders(page, comp["yc_url"])
                direct = sum(1 for r in yc_results if r["twitter_handle"])
                log.info(f"  YC: {len(yc_results)} candidate(s), {direct} direct X link(s)")

            if not yc_results:
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO founders (company_id, company_name, founder_name, handle_status) "
                        "VALUES (?, ?, ?, 'not_found')",
                        (comp["id"], comp["name"], ""),
                    )
                    conn.commit()
                update_run(run_id, processed=i, log_tail=get_tail())
                continue

            for item in yc_results:
                fname = item["name"]
                direct_handle = item["twitter_handle"]
                handle, source = (None, "")

                if direct_handle:
                    # Verify the direct link still resolves; if so, accept without search.
                    if await _verify_handle(page, direct_handle, fname, comp["name"]):
                        handle, source = direct_handle, "yc_direct"

                if not handle and fname:
                    handle, source = await _find_one(page, fname, comp["name"])

                status = "found" if handle else "not_found"
                followed = False
                if handle and FOLLOW_AFTER_VERIFY:
                    followed = await _follow_handle(page, handle)

                with connect() as conn:
                    conn.execute(
                        "INSERT INTO founders (company_id, company_name, founder_name, "
                        "twitter_handle, handle_status, search_source) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (comp["id"], comp["name"], fname or "(from YC link)",
                         handle, status, source or None),
                    )
                    conn.commit()

                if handle:
                    follow_msg = " [followed]" if followed else (" [follow failed]" if FOLLOW_AFTER_VERIFY else "")
                    log.info(f"  {fname or '(?)'} -> @{handle} ({source}){follow_msg}")
                else:
                    log.info(f"  {fname or '(?)'} -> (not found)")

                await asyncio.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

            update_run(run_id, processed=i, log_tail=get_tail())

        await browser.close()

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info("find_handles done")


def run(company_ids: list[int] | None = None):
    asyncio.run(_async_run(company_ids=company_ids))


if __name__ == "__main__":
    run()

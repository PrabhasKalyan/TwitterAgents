"""Find founder Twitter/X handles by evidence — not by guessing.

Per-company pipeline:

  1. Scrape COMPANY WEBSITE — /team, /about, /founders, /people, /the-team, etc.
     For each `<a href="x.com/…">` or `<a href="twitter.com/…">`, walk up the
     DOM to find the nearest name. Yields (name, handle) pairs.
  2. Scrape YC PAGE founder cards — same name+link pair extraction.
  3. Resolve the COMPANY'S OFFICIAL X handle (website footer → x.com search).
     Crawl that account's `/following` to harvest (display_name, handle)
     candidates — founders almost always follow their own company.

If none of those three tiers produce a candidate, the founder is recorded as
`not_found`. Search engines are deliberately excluded — they 403 from cloud
IPs and their results are noisy enough that strict verification rejects most
of them anyway.

Every candidate is STRICT-VERIFIED:
  - Profile exists and not suspended
  - At least ONE of:
      • Bio contains company name OR website domain  → confidence HIGH
      • Profile follows the company X account        → confidence MEDIUM
      • Name token-overlap ≥ 2/3 AND ≥ MIN_FOLLOWERS → confidence LOW
  - Direct link from YC / website /team page is auto-HIGH (the source itself
    is corroboration).

After verification we record `confidence` and `evidence` (string explaining
which signal won), then click Follow on the founder's profile.

No DB caching of company handles (per spec) — resolved per-company in-memory.
"""
import asyncio
import json
import os
import random
import re
import urllib.parse

from config import (COMPANY_FOLLOWING_CRAWL, FOLLOW_AFTER_VERIFY,
                    MAX_TEAM_PAGES_PER_COMPANY, MIN_FOLLOWERS_FOR_NAME_ONLY,
                    SEARCH_DELAY_MAX, SEARCH_DELAY_MIN, WEBSITE_PAGE_TIMEOUT_MS)
from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CREDS_PATH = os.environ.get("TW_CREDS", "/inputs/twitter_credentials.json")

HANDLE_RE = re.compile(
    r"(?:x\.com|twitter\.com)/(?!i/|search|home|messages|notifications|explore|hashtag|"
    r"intent|share|compose|settings|login|signup|tos|privacy|about|jobs|press|status)"
    r"([A-Za-z0-9_]{2,15})(?:/|$|\?|#)"
)
RESERVED_HANDLES = {"home", "explore", "search", "messages", "notifications", "i",
                    "intent", "share", "hashtag", "compose", "settings", "login",
                    "signup", "tos", "privacy", "about", "jobs", "press",
                    "status", "ycombinator", "twitter", "x"}

TEAM_PATHS = ["/team", "/about", "/about-us", "/founders", "/people",
              "/the-team", "/our-team", "/company", "/who-we-are"]


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


# ---------------------- name + matching helpers ----------------------

NAME_BLACKLIST_SUBSTR = [
    "founder", "team", "company", "about", "active", "hiring", "jobs",
    "solution", "problem", "news", "latest", "make something", "the team",
    "our ", "the problem", "the solution", "contact", "demo", "pitch",
    "subscribe", "newsletter", "investor", "press", "what we", "launches",
    "secure document", "previous launch",
]


def _looks_like_name(s: str) -> bool:
    s = (s or "").strip()
    if len(s) < 4 or len(s) > 60:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    low = s.lower()
    if any(w in low for w in NAME_BLACKLIST_SUBSTR):
        return False
    if not re.search(r"[A-Za-z]{2,}", s):
        return False
    clean = re.sub(r"[^\w\s\-']", " ", s).strip()
    parts = [p for p in clean.split() if any(c.isalpha() for c in p)]
    if not (2 <= len(parts) <= 5):
        return False
    if not all(p[0].isalpha() for p in parts):
        return False
    return True


def _name_tokens(s: str) -> set[str]:
    return {t for t in re.sub(r"[^\w\s]", " ", (s or "").lower()).split() if len(t) >= 2}


def _name_overlap(a: str, b: str) -> float:
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def _domain_of(url: str) -> str:
    if not url:
        return ""
    try:
        host = urllib.parse.urlparse(url if "://" in url else f"http://{url}").netloc
        host = host.lower().lstrip("www.")
        return host
    except Exception:
        return ""


def _company_tokens(company_name: str, website: str) -> set[str]:
    """Tokens used to check if a profile bio mentions this company."""
    toks = set()
    if company_name:
        for t in re.sub(r"[^\w\s]", " ", company_name.lower()).split():
            if len(t) >= 3 and t not in {"the", "and", "for", "inc", "labs", "ai"}:
                toks.add(t)
    dom = _domain_of(website)
    if dom:
        toks.add(dom)
        # Add the bare brand part of the domain (foo.com → foo)
        base = dom.split(".")[0]
        if len(base) >= 3:
            toks.add(base)
    return toks


# ---------------------- company website scraping ----------------------

async def _safe_goto(page, url: str, timeout: int = WEBSITE_PAGE_TIMEOUT_MS) -> bool:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(1200)
        return True
    except Exception as e:
        log.warning(f"  goto failed {url}: {type(e).__name__}")
        return False


async def _safe_eval(page, js: str, default, *args):
    """Wrap page.evaluate — pages can navigate/redirect mid-eval and that raises
    'Execution context was destroyed'. Return `default` on any error."""
    try:
        if args:
            return await page.evaluate(js, *args)
        return await page.evaluate(js)
    except Exception as e:
        msg = str(e)
        if "Execution context was destroyed" in msg or "navigation" in msg.lower():
            log.warning(f"  eval lost context (page navigated); skipping")
        else:
            log.warning(f"  eval failed: {type(e).__name__}: {msg[:120]}")
        return default


async def _find_team_page_urls(page, website: str) -> list[str]:
    """Visit homepage, return a list of likely team-page URLs (homepage + linked /team etc)."""
    if not website:
        return []
    if "://" not in website:
        website = "https://" + website
    if not await _safe_goto(page, website):
        return []
    base = page.url.rstrip("/")
    found = await _safe_eval(
        page,
        """(teamPaths) => {
            const out = new Set();
            const lower = teamPaths.map(p => p.toLowerCase());
            for (const a of document.querySelectorAll('a[href]')) {
                const href = a.getAttribute('href') || '';
                const txt = (a.innerText || '').toLowerCase().trim();
                const h = href.toLowerCase();
                if (lower.some(p => h === p || h.endsWith(p) || h.includes(p + '/') || h.includes(p + '?'))) {
                    out.add(a.href);
                } else if (['team','about','founders','people','our team','about us'].includes(txt)) {
                    out.add(a.href);
                }
            }
            return Array.from(out);
        }""",
        [],
        TEAM_PATHS,
    )
    urls = [base]
    for u in found:
        if u not in urls:
            urls.append(u)
    return urls[:1 + MAX_TEAM_PAGES_PER_COMPANY]


async def _scrape_name_handle_pairs(page, url: str) -> list[dict]:
    """Extract (name, handle) candidates by walking up from each twitter link."""
    if not await _safe_goto(page, url):
        return []
    raw = await _safe_eval(
        page,
        """() => {
            const out = [];
            for (const a of document.querySelectorAll('a[href*="twitter.com/"], a[href*="x.com/"]')) {
                const href = a.getAttribute('href') || '';
                // Walk up to 5 ancestors looking for a short, name-shaped text block.
                let el = a;
                let nameGuess = '';
                for (let i = 0; i < 5 && el && el.parentElement; i++) {
                    el = el.parentElement;
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 400) continue;
                    // Split into lines and find the first that looks like a name
                    const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
                    for (const line of lines) {
                        if (line.length >= 4 && line.length <= 60 &&
                            /[A-Za-z]/.test(line) && !/[0-9@]/.test(line)) {
                            nameGuess = line;
                            break;
                        }
                    }
                    if (nameGuess) break;
                }
                out.push({ href, name: nameGuess });
            }
            return out;
        }""",
        [],
    )
    pairs = []
    seen_handles = set()
    for item in raw:
        href = item.get("href") or ""
        name = (item.get("name") or "").strip()
        m = HANDLE_RE.search(href)
        if not m:
            continue
        handle = m.group(1)
        if handle.lower() in RESERVED_HANDLES or handle in seen_handles:
            continue
        # Reject status/tweet links
        if "/status/" in href or "/intent/" in href:
            continue
        # Reject obvious company-account links: e.g., handle matches "share", etc.
        if not _looks_like_name(name):
            name = ""  # Keep the handle, but with no name guess
        seen_handles.add(handle)
        pairs.append({"name": name, "handle": handle, "source_url": url})
    return pairs


async def _scrape_yc_founder_pairs(page, yc_url: str) -> list[dict]:
    """Same shape as _scrape_name_handle_pairs but tuned for YC company pages.

    Returns BOTH name-only candidates (no handle yet) AND name+handle pairs.
    """
    if not await _safe_goto(page, yc_url):
        return []
    data = await _safe_eval(
        page,
        """() => {
            const out = [];
            const seen = new Set();
            // First pass: founder cards with direct X/Twitter link
            for (const a of document.querySelectorAll('a[href*="twitter.com/"], a[href*="x.com/"]')) {
                const href = a.getAttribute('href') || '';
                if (href.includes('/intent/') || href.includes('/status/')) continue;
                let el = a, nameGuess = '';
                for (let i = 0; i < 5 && el && el.parentElement; i++) {
                    el = el.parentElement;
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 400) continue;
                    const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
                    for (const line of lines) {
                        if (line.length >= 4 && line.length <= 60 &&
                            /[A-Za-z]/.test(line) && !/[0-9@]/.test(line)) {
                            nameGuess = line; break;
                        }
                    }
                    if (nameGuess) break;
                }
                const key = 'L:' + href;
                if (seen.has(key)) continue;
                seen.add(key);
                out.push({ kind: 'link', href, name: nameGuess });
            }
            // Second pass: name-only candidates (founder section h3s)
            for (const el of document.querySelectorAll('h3, .font-bold, [class*="founder"]')) {
                const t = (el.innerText || '').trim();
                if (!t) continue;
                const key = 'N:' + t;
                if (seen.has(key)) continue;
                seen.add(key);
                out.push({ kind: 'name', name: t });
            }
            return out;
        }""",
        [],
    )
    pairs = []
    seen_handles = set()
    seen_names = set()
    for item in data:
        if item.get("kind") == "link":
            m = HANDLE_RE.search(item.get("href") or "")
            if not m:
                continue
            handle = m.group(1)
            if handle.lower() in RESERVED_HANDLES or handle in seen_handles:
                continue
            seen_handles.add(handle)
            name = item.get("name") or ""
            if not _looks_like_name(name):
                name = ""
            pairs.append({"name": name, "handle": handle, "source_url": yc_url})
        else:
            name = (item.get("name") or "").strip()
            if not _looks_like_name(name):
                continue
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            pairs.append({"name": name, "handle": None, "source_url": yc_url})
    return pairs


# ---------------------- company X account resolution ----------------------

async def _company_x_handle_from_website(page, website: str, company_name: str) -> str | None:
    """Scrape homepage + linked pages for the company's own X handle.

    Heuristic: the handle whose lowercase form is closest to the company name's
    primary token, ignoring obvious user/founder links (no /status/, no /intent/).
    """
    if not website:
        return None
    if "://" not in website:
        website = "https://" + website
    if not await _safe_goto(page, website):
        return None
    handles = await _safe_eval(
        page,
        """() => {
            const out = new Set();
            for (const a of document.querySelectorAll('a[href*="twitter.com/"], a[href*="x.com/"]')) {
                const href = a.getAttribute('href') || '';
                if (href.includes('/status/') || href.includes('/intent/')) continue;
                const m = href.match(/(?:x|twitter)\\.com\\/([A-Za-z0-9_]{2,15})(?:\\/|$|\\?)/i);
                if (m) out.add(m[1].toLowerCase());
            }
            return Array.from(out);
        }""",
        [],
    )
    if not handles:
        return None
    co_tokens = _name_tokens(company_name)
    # Score: handle that contains a company token wins
    best, best_score = None, -1.0
    for h in handles:
        if h in RESERVED_HANDLES:
            continue
        score = 0.0
        for t in co_tokens:
            if t in h:
                score = max(score, len(t) / max(1, len(h)))
        if score > best_score:
            best, best_score = h, score
    return best


async def _company_x_handle_via_search(page, company_name: str, website: str) -> str | None:
    """Search x.com for the company; pick the first profile whose bio contains the website domain."""
    domain = _domain_of(website)
    if not company_name:
        return None
    q = urllib.parse.quote(company_name)
    if not await _safe_goto(page, f"https://x.com/search?q={q}&f=user", 25000):
        return None
    cells = await _safe_eval(
        page,
        """() => Array.from(document.querySelectorAll('[data-testid="UserCell"]')).slice(0, 6).map(c => {
            const a = c.querySelector('a[href^="/"]');
            const m = a && (a.getAttribute('href') || '').match(/^\\/([A-Za-z0-9_]{2,15})$/);
            return { handle: m ? m[1] : null, text: (c.innerText || '').toLowerCase() };
        })""",
        [],
    )
    for c in cells:
        h = c.get("handle")
        if not h or h.lower() in RESERVED_HANDLES:
            continue
        text = c.get("text") or ""
        if domain and domain in text:
            return h
        if company_name.lower() in text:
            return h
    return None


async def _company_following(page, co_handle: str) -> list[dict]:
    """Crawl /following list, return [{handle, name, bio}]."""
    if not co_handle:
        return []
    url = f"https://x.com/{co_handle}/following"
    if not await _safe_goto(page, url, 25000):
        return []
    # Scroll a few times to load more
    for _ in range(3):
        try:
            await page.mouse.wheel(0, 1500)
        except Exception:
            break
        await page.wait_for_timeout(800)
    rows = await _safe_eval(
        page,
        """(limit) => {
            const cells = Array.from(document.querySelectorAll('[data-testid="UserCell"]')).slice(0, limit);
            return cells.map(c => {
                const a = c.querySelector('a[href^="/"]');
                const m = a && (a.getAttribute('href') || '').match(/^\\/([A-Za-z0-9_]{2,15})$/);
                const text = c.innerText || '';
                // First line in the cell is usually the display name
                const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                return {
                    handle: m ? m[1] : null,
                    name: lines[0] || '',
                    bio: lines.slice(2).join(' ').slice(0, 280),
                };
            }).filter(r => r.handle);
        }""",
        [],
        COMPANY_FOLLOWING_CRAWL,
    )
    return rows


# ---------------------- verification ----------------------

async def _profile_signals(page, handle: str) -> dict | None:
    """Load profile, return {exists, bio, name, followers, follows_company?}."""
    try:
        await page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(1800)
    except Exception:
        return None
    sig = await _safe_eval(
        page,
        """() => {
            const body = (document.body.innerText || '').toLowerCase();
            if (body.includes("this account doesn't exist") || body.includes("this account doesn’t exist")) {
                return {exists: false};
            }
            if (body.includes('account suspended')) return {exists: false};
            const userNameEl = document.querySelector('[data-testid="UserName"]');
            const displayName = userNameEl ? (userNameEl.innerText || '').split('\\n')[0].trim() : '';
            const bioEl = document.querySelector('[data-testid="UserDescription"]');
            const bio = bioEl ? (bioEl.innerText || '').trim() : '';
            // Followers count
            let followers = null;
            const links = Array.from(document.querySelectorAll('a[href$="/verified_followers"], a[href$="/followers"]'));
            for (const a of links) {
                const txt = (a.innerText || '').replace(/[^0-9KMkm.]/g, '');
                if (!txt) continue;
                let n = parseFloat(txt.toLowerCase());
                if (isNaN(n)) continue;
                if (txt.toLowerCase().endsWith('k')) n *= 1000;
                if (txt.toLowerCase().endsWith('m')) n *= 1000000;
                followers = Math.round(n);
                break;
            }
            return {exists: true, displayName, bio, followers};
        }""",
        None,
    )
    return sig


def _classify(sig: dict, founder_name: str, company_tokens: set[str],
              follows_company: bool, source_was_direct: bool) -> tuple[bool, str, str]:
    """Return (verified, confidence, evidence)."""
    bio = (sig.get("bio") or "").lower()
    display = sig.get("displayName") or ""
    followers = sig.get("followers") or 0

    bio_match = any(t in bio for t in company_tokens if len(t) >= 3)
    name_ovl = _name_overlap(display, founder_name)

    if source_was_direct:
        # Direct link from YC card or website /team is itself strong evidence.
        return True, "high", f"direct_link;bio_match={bio_match};name_overlap={name_ovl:.2f}"

    if bio_match:
        return True, "high", f"bio_match;name_overlap={name_ovl:.2f};followers={followers}"

    if follows_company and name_ovl >= 0.5:
        return True, "medium", f"follows_company;name_overlap={name_ovl:.2f};followers={followers}"

    if name_ovl >= 0.66 and followers >= MIN_FOLLOWERS_FOR_NAME_ONLY:
        return True, "low", f"name_only;name_overlap={name_ovl:.2f};followers={followers}"

    return False, "", f"rejected;bio_match=False;name_overlap={name_ovl:.2f};followers={followers}"


async def _follow_handle(page, handle: str) -> bool:
    try:
        if f"/{handle}" not in page.url:
            await page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1200)
        clicked = await _safe_eval(
            page,
            """() => {
                const btns = Array.from(document.querySelectorAll('[data-testid$="-follow"], [role="button"]'));
                for (const b of btns) {
                    const txt = (b.innerText || '').trim();
                    if (txt === 'Follow') { b.click(); return true; }
                }
                return false;
            }""",
            False,
        )
        if clicked:
            await page.wait_for_timeout(1000)
        return bool(clicked)
    except Exception as e:
        log.warning(f"follow failed for @{handle}: {e}")
        return False


# ---------------------- main per-company logic ----------------------

async def _resolve_company(page, comp: dict) -> dict:
    """Build the per-company evidence pool BEFORE per-founder resolution."""
    pool = {
        "website_pairs": [],   # [{name, handle, source_url}] — direct from team pages
        "yc_pairs": [],        # [{name, handle?, source_url}] — direct + name-only from YC
        "co_handle": None,     # company X handle (for follower cross-ref)
        "co_following": [],    # list of {handle, name, bio} from company's following
    }
    website = (comp.get("website") or "").strip()
    yc_url = (comp.get("yc_url") or "").strip()

    # 1. Company website team pages
    if website:
        urls = await _find_team_page_urls(page, website)
        for u in urls:
            pairs = await _scrape_name_handle_pairs(page, u)
            pool["website_pairs"].extend(pairs)
        # Dedup by handle
        seen = set()
        deduped = []
        for p in pool["website_pairs"]:
            if p["handle"] in seen:
                continue
            seen.add(p["handle"])
            deduped.append(p)
        pool["website_pairs"] = deduped

    # 2. YC page
    if yc_url:
        pool["yc_pairs"] = await _scrape_yc_founder_pairs(page, yc_url)

    # 3. Company X handle + following list (used for cross-reference, NOT followed)
    if website:
        pool["co_handle"] = await _company_x_handle_from_website(page, website, comp["name"])
    if not pool["co_handle"]:
        pool["co_handle"] = await _company_x_handle_via_search(page, comp["name"], website)
    if pool["co_handle"]:
        pool["co_following"] = await _company_following(page, pool["co_handle"])

    return pool


def _founder_candidates(pool: dict, founder_name: str) -> list[tuple[str, str, bool]]:
    """Return [(handle, evidence_source, is_direct_link)] candidates for one founder name."""
    out, seen = [], set()

    def push(handle, source, is_direct):
        if not handle or handle in seen:
            return
        seen.add(handle)
        out.append((handle, source, is_direct))

    # Website pairs: match by name overlap or accept name-blank pairs as generic
    for p in pool["website_pairs"]:
        if p["name"] and _name_overlap(p["name"], founder_name) >= 0.5:
            push(p["handle"], f"website_team({p['source_url']})", True)
    # YC pairs: same
    for p in pool["yc_pairs"]:
        if p.get("handle") and p.get("name") and _name_overlap(p["name"], founder_name) >= 0.5:
            push(p["handle"], "yc_direct", True)
    # Company following: name overlap
    for row in pool["co_following"]:
        if _name_overlap(row.get("name") or "", founder_name) >= 0.66:
            push(row["handle"], f"co_following(@{pool['co_handle']})", False)
    # Untargeted website / YC pairs (no name attached): keep as low-priority candidates
    for p in pool["website_pairs"]:
        if not p["name"]:
            push(p["handle"], f"website_team_namelink({p['source_url']})", True)

    return out


async def _resolve_founder(page, founder_name: str, comp: dict, pool: dict) -> dict:
    """Return {handle, source, confidence, evidence} or {handle: None, ...} if not found."""
    company_tokens = _company_tokens(comp["name"], comp.get("website") or "")
    co_following_handles = {r["handle"] for r in pool["co_following"] if r.get("handle")}

    cands = _founder_candidates(pool, founder_name)

    for handle, source, is_direct in cands:
        sig = await _profile_signals(page, handle)
        if not sig or not sig.get("exists"):
            continue
        follows_co = handle in co_following_handles
        ok, conf, ev = _classify(sig, founder_name, company_tokens, follows_co, is_direct)
        if ok:
            return {"handle": handle, "source": source, "confidence": conf,
                    "evidence": f"{source}|{ev}"}
        await asyncio.sleep(random.uniform(0.8, 1.5))

    return {"handle": None, "source": "", "confidence": "", "evidence": "no_candidate_verified"}


async def _async_run(company_ids: list[int] | None = None):
    from playwright.async_api import async_playwright

    init_db()

    with connect() as conn:
        if company_ids:
            placeholders = ",".join("?" * len(company_ids))
            companies = conn.execute(
                f"SELECT id, name, yc_url, website FROM companies WHERE id IN ({placeholders})",
                company_ids,
            ).fetchall()
        else:
            companies = conn.execute(
                "SELECT id, name, yc_url, website FROM companies WHERE filtered_in = 1 "
                "AND id NOT IN (SELECT DISTINCT company_id FROM founders WHERE company_id IS NOT NULL)"
            ).fetchall()

    total = len(companies)
    run_id = start_run("find_handles", total=total)
    log.info(f"find_handles: {total} companies (website → YC → company_following)")

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
            comp_d = dict(comp)
            log.info(f"[{i}/{total}] {comp_d['name']}")
            try:
                pool = await _resolve_company(page, comp_d)
            except Exception as e:
                log.error(f"  resolve_company failed: {e}")
                pool = {"website_pairs": [], "yc_pairs": [], "co_handle": None, "co_following": []}

            log.info(f"  pool: {len(pool['website_pairs'])} website-direct, "
                     f"{len([p for p in pool['yc_pairs'] if p.get('handle')])} yc-direct, "
                     f"co=@{pool['co_handle'] or '?'}, "
                     f"{len(pool['co_following'])} co-following")

            # Build the master list of founder names to resolve for this company.
            names = []
            seen = set()
            for p in pool["yc_pairs"]:
                n = p.get("name") or ""
                if _looks_like_name(n) and n.lower() not in seen:
                    seen.add(n.lower())
                    names.append(n)
            for p in pool["website_pairs"]:
                n = p.get("name") or ""
                if _looks_like_name(n) and n.lower() not in seen:
                    seen.add(n.lower())
                    names.append(n)

            if not names:
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO founders (company_id, company_name, founder_name, "
                        "handle_status, evidence) VALUES (?, ?, ?, 'not_found', ?)",
                        (comp_d["id"], comp_d["name"], "", "no_founder_names_discovered"),
                    )
                    conn.commit()
                update_run(run_id, processed=i, log_tail=get_tail())
                continue

            for fname in names[:8]:
                result = await _resolve_founder(page, fname, comp_d, pool)
                handle = result["handle"]
                status = "found" if handle else "not_found"

                followed = False
                if handle and FOLLOW_AFTER_VERIFY:
                    followed = await _follow_handle(page, handle)

                follow_status = None
                if handle and FOLLOW_AFTER_VERIFY:
                    follow_status = "followed" if followed else "failed"

                with connect() as conn:
                    conn.execute(
                        "INSERT INTO founders (company_id, company_name, founder_name, "
                        "twitter_handle, handle_status, search_source, confidence, evidence, "
                        "follow_status, followed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "CASE WHEN ?='followed' THEN CURRENT_TIMESTAMP ELSE NULL END)",
                        (comp_d["id"], comp_d["name"], fname, handle, status,
                         result["source"] or None, result["confidence"] or None,
                         result["evidence"], follow_status, follow_status),
                    )
                    conn.commit()

                if handle:
                    fmsg = " [followed]" if followed else " [follow failed]"
                    log.info(f"  {fname} -> @{handle} "
                             f"[{result['confidence']}] via {result['source']}{fmsg}")
                else:
                    log.info(f"  {fname} -> (not found)")

                await asyncio.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

            update_run(run_id, processed=i, log_tail=get_tail())

        await browser.close()

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info("find_handles done")


def run(company_ids: list[int] | None = None):
    asyncio.run(_async_run(company_ids=company_ids))


if __name__ == "__main__":
    run()

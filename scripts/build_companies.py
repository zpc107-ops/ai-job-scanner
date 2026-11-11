"""Phase 1: build data/companies.csv from the Elevation partner portal.

Steps:
  1. Render the portal with Playwright and read every "View Open Positions" card
     (name, category, careers URL).
  2. Classify each link: LinkedIn (excluded), direct ATS link, or company site.
  3. Fetch each non-LinkedIn page with httpx and look for ATS fingerprints
     (final URL after redirects + HTML). Pages with no hit are optionally
     re-checked with Playwright (iframes, scripts and network calls).
  4. For ATSs with a public API, call the endpoint once and record the job count.

Run:  python scripts/build_companies.py
      python scripts/build_companies.py --limit 20 --no-render   # quick test
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

import httpx

PORTAL_URL = "https://elevation-hp-career.base44.app/"
USER_AGENT = "ai-job-scanner/1.0"
TIMEOUT = 20.0
MIN_DOMAIN_INTERVAL = 1.0  # seconds between requests to the same domain
RETRIES = 3

log = logging.getLogger("build_companies")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Company:
    name: str
    category: str
    careers_url: str
    link_type: str = ""        # linkedin_excluded | ats_direct | company_site
    final_url: str = ""        # after redirects
    ats: str = ""              # greenhouse | comeet | workday | ... | unknown
    ats_id: str = ""           # board / slug / tenant/site / company uid
    ats_token: str = ""        # Comeet public token (embedded in public pages)
    ats_slug: str = ""         # Comeet company slug (comeet.com/jobs/{slug}/{uid})
    api_endpoint: str = ""     # public API URL, if the ATS has one
    detected_by: str = ""      # url | html | render
    api_ok: str = ""           # yes | no | ""
    jobs_count: str = ""
    status: str = "ok"         # ok | error | excluded
    error: str = ""


# --------------------------------------------------------------------------- #
# HTTP with per-domain politeness + retries
# --------------------------------------------------------------------------- #
class DomainLimiter:
    """Ensures at least MIN_DOMAIN_INTERVAL between requests to one domain."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()

    def wait(self, url: str) -> None:
        domain = urlparse(url).hostname or ""
        domain = domain.removeprefix("www.")
        with self._guard:
            lock = self._locks.setdefault(domain, threading.Lock())
        with lock:
            delay = self._last.get(domain, 0) + self.interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last[domain] = time.monotonic()


LIMITER = DomainLimiter(MIN_DOMAIN_INTERVAL)


def request(client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        LIMITER.wait(url)
        try:
            resp = client.request(method, url, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < RETRIES - 1:
                time.sleep(2 ** (attempt + 1))
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- #
# ATS fingerprints. Order matters: first match wins.
# Each entry: (ats, regex, how to build ats_id from the match)
# --------------------------------------------------------------------------- #
UID = r"[0-9A-Z]{2}\.[0-9A-Z]{3}"
PATTERNS: list[tuple[str, re.Pattern[str], callable]] = [
    ("comeet", re.compile(rf"careers-api/2\.0/company/({UID})/positions\?token=([0-9A-F]{{20,}})"), lambda m: m.group(1)),
    ("comeet", re.compile(rf"comeet\.(?:com|co)/jobs/[\w.-]+/({UID})"), lambda m: m.group(1)),
    ("comeet", re.compile(rf"""["']?company[-_]uid["']?\s*[:=]\s*["']({UID})["']""", re.I), lambda m: m.group(1)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)"), lambda m: m.group(1)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)"), lambda m: m.group(1)),
    ("greenhouse", re.compile(r"(?:job-boards|boards)\.(?:eu\.)?greenhouse\.io/(?!embed\b)([\w-]+)"), lambda m: m.group(1)),
    ("lever", re.compile(r"(?:jobs\.(?:eu\.)?lever\.co|api\.lever\.co/v0/postings)/([\w.-]+)"), lambda m: m.group(1)),
    ("ashby", re.compile(r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([\w.%-]+)"), lambda m: m.group(1)),
    ("smartrecruiters", re.compile(r"(?:(?:jobs|careers)\.smartrecruiters\.com|api\.smartrecruiters\.com/v1/companies)/([\w-]+)"), lambda m: m.group(1)),
    ("workday", re.compile(r"(?<![\w-])([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/wday/cxs/[\w-]+/([\w-]+)"), lambda m: f"{m.group(1)}/{m.group(3)}"),
    ("workday", re.compile(r"(?<![\w-])([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?!wday\b)([\w-]+)"), lambda m: f"{m.group(1)}/{m.group(3)}"),
    ("workable", re.compile(r"apply\.workable\.com/(?!api\b)([\w-]+)"), lambda m: m.group(1)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([\w-]+)"), lambda m: m.group(1)),
    ("taleo", re.compile(r"(?<![\w-])([\w-]+)\.taleo\.net"), lambda m: m.group(1)),
    ("dueto", re.compile(r"employer\.dueto\.io/([\w-]+)"), lambda m: m.group(1)),
    ("civi", re.compile(r"app\.civi\.co\.il/promos/id=(\w+)"), lambda m: m.group(1)),
    ("adam", re.compile(r"career\.adamtotal\.co\.il/\?token=([\w-]+)"), lambda m: m.group(1)),
    ("topmatch", re.compile(r"careers\.topmatch\.co\.il/([\w-]+)"), lambda m: m.group(1)),
    ("wanted", re.compile(r"(?<![\w-])([\w-]+)\.wanted\.co\.il"), lambda m: m.group(1)),
    ("bamboohr", re.compile(r"(?<![\w-])([\w-]+)\.bamboohr\.com/(?:careers|jobs)"), lambda m: m.group(1)),
    ("breezy", re.compile(r"(?<![\w-])([\w-]+)\.breezy\.hr"), lambda m: m.group(1)),
    ("recruitee", re.compile(r"(?<![\w-])([\w-]+)\.recruitee\.com"), lambda m: m.group(1)),
    ("teamtailor", re.compile(r"(?<![\w-])([\w-]+)\.teamtailor\.com"), lambda m: m.group(1)),
    ("personio", re.compile(r"(?<![\w-])([\w-]+)\.jobs\.personio\.(?:de|com)"), lambda m: m.group(1)),
    ("pinpoint", re.compile(r"(?<![\w-])([\w-]+)\.pinpointhq\.com"), lambda m: m.group(1)),
    ("icims", re.compile(r"careers-([\w-]+)\.icims\.com"), lambda m: m.group(1)),
    ("eightfold", re.compile(r"(?<![\w-])([\w-]+)\.eightfold\.ai/careers"), lambda m: m.group(1)),
    ("oracle", re.compile(r"(?<![\w-])([\w-]+)\.fa\.[\w.]+\.oraclecloud\.com"), lambda m: m.group(1)),
    ("successfactors", re.compile(r"(?:career\d*\.successfactors\.(?:com|eu)|jobs\.sap\.com)[^\"'\s]*company=([\w-]+)"), lambda m: m.group(1)),
]
COMEET_TOKEN = re.compile(r"""["']?token["']?\s*[:=]\s*["']([0-9A-F]{20,})["']""")
COMEET_API_TOKEN = re.compile(rf"careers-api/2\.0/company/{UID}/positions\?token=([0-9A-F]{{20,}})")
# Long base64 / minified runs make the leading-word patterns slow; strip them before matching.
BLOB = re.compile(r"[A-Za-z0-9+/=_-]{500,}")
NOT_IDS = {"embed", "jobs", "careers", "www", "api", "app", "static", "assets", "js", "css", "cdn"}

API_ATS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "comeet"}


def detect(text: str) -> tuple[str, str, str] | None:
    """Return (ats, ats_id, extra) for the first fingerprint found in text."""
    text = BLOB.sub(" ", text)
    for ats, pattern, build in PATTERNS:
        for m in pattern.finditer(text):
            ats_id = build(m)
            if ats_id.lower() in NOT_IDS:
                continue
            extra = ""
            if ats == "comeet":
                tok = COMEET_API_TOKEN.search(text) or COMEET_TOKEN.search(text)
                if tok:
                    extra = tok.group(1)
                else:  # no token on the page: keep the slug to read it from comeet.com later
                    slug = re.search(rf"comeet\.(?:com|co)/jobs/([\w.-]+)/{re.escape(ats_id)}", text)
                    extra = f"slug:{slug.group(1)}" if slug else ""
            elif ats == "workday":
                extra = m.group(2)  # wdN
            return ats, ats_id, extra
    return None


# Weak hints: the ATS is clear but the identifier is not in the page.
HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"[?&](?:gh_jid|gh_src|gh_office)=")),
    ("eightfold", re.compile(r"hideEightfoldBranding|eightfold-font|eightfold\.ai/", re.I)),
    ("comeet", re.compile(r"comeet", re.I)),
    ("workday", re.compile(r"myworkdayjobs\.com", re.I)),
]
JOBS_LINK = re.compile(r"""href=["'](https?://[^"'#]+)["']""")
JOBS_WORDS = re.compile(r"(?:^|[/.?=_-])(?:jobs?|positions?|open-?positions|openings|careers?|vacancies|%d7%9e%d7%a9%d7%a8%d7%95%d7%aa)(?:[/.?=_-]|$)", re.I)
STATIC_FILE = re.compile(r"\.(?:css|js|json|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|pdf|xml|mp4)$", re.I)
SKIP_HOSTS = ("linkedin.", "facebook.", "twitter.", "x.com", "instagram.", "youtube.", "glassdoor.", "google.")


def hint(text: str, page_url: str) -> tuple[str, str, str] | None:
    for ats, pattern in HINTS:
        if pattern.search(text):
            host = (urlparse(page_url).hostname or "") if ats == "eightfold" else ""
            return ats, host, ""
    return None


def jobs_link(html: str, page_url: str) -> str:
    """First link that looks like a job listing page, other than the page itself."""
    here = page_url.rstrip("/")
    for m in JOBS_LINK.finditer(html):
        url = m.group(1)
        host = (urlparse(url).hostname or "").lower()
        if url.rstrip("/") == here or any(s in host for s in SKIP_HOSTS):
            continue
        if STATIC_FILE.search(urlparse(url).path) or "cdn" in host or host.startswith("static."):
            continue
        if JOBS_WORDS.search(urlparse(url).path) or host.startswith(("jobs.", "careers.", "career.")):
            return url
    return ""


def greenhouse_candidates(c: Company) -> list[str]:
    host = urlparse(c.final_url or c.careers_url).hostname or ""
    labels = [p for p in host.split(".") if p not in ("www", "join", "careers", "career", "jobs", "com", "co", "il", "io", "net", "org")]
    name = re.sub(r"[^a-z0-9]", "", c.name.lower())
    out = []
    for cand in [*labels[:1], name]:
        if cand and cand not in out:
            out.append(cand)
    return out


def api_endpoint(c: Company, extra: str) -> str:
    if c.ats == "greenhouse" and c.ats_id:
        return f"https://boards-api.greenhouse.io/v1/boards/{c.ats_id}/jobs?content=true"
    if c.ats == "lever":
        return f"https://api.lever.co/v0/postings/{c.ats_id}?mode=json"
    if c.ats == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{c.ats_id}"
    if c.ats == "smartrecruiters":
        return f"https://api.smartrecruiters.com/v1/companies/{c.ats_id}/postings"
    if c.ats == "workday" and extra:
        tenant, site = c.ats_id.split("/", 1)
        return f"https://{tenant}.{extra}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    if c.ats == "comeet" and c.ats_token:
        return f"https://www.comeet.co/careers-api/2.0/company/{c.ats_id}/positions?token={c.ats_token}"
    return ""


def apply_detection(c: Company, found: tuple[str, str, str], how: str) -> None:
    c.ats, c.ats_id, extra = found
    if c.ats == "comeet":
        if extra.startswith("slug:"):
            c.ats_slug, c.ats_token = extra[5:], ""
        else:
            c.ats_token = extra
    c.detected_by = how
    c.api_endpoint = api_endpoint(c, extra)


# --------------------------------------------------------------------------- #
# Step 1: company list from the portal
# --------------------------------------------------------------------------- #
def fetch_portal_companies() -> list[Company]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(PORTAL_URL, wait_until="networkidle", timeout=60_000)
        selector = "a:has-text('View Open Positions')"
        page.wait_for_selector(selector, state="attached", timeout=30_000)
        # Wait until the number of cards stops growing.
        count, stable = 0, 0
        while stable < 3:
            page.wait_for_timeout(500)
            n = page.locator(selector).count()
            stable = stable + 1 if n == count else 0
            count = n
        cards = page.eval_on_selector_all(
            selector, "els => els.map(a => [a.innerText, a.href])"
        )
        browser.close()

    companies = []
    for text, href in cards:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not ln.startswith("View Open Positions")]
        # Cards with a logo may start with initials; name and category are always last.
        name = lines[-2] if len(lines) > 1 else (lines[0] if lines else "")
        category = lines[-1].title() if len(lines) > 1 else ""
        companies.append(Company(name=name, category=category, careers_url=href.strip()))
    log.info("Portal: %d companies", len(companies))
    return companies


# --------------------------------------------------------------------------- #
# Step 2-4: classify, fetch, detect, verify
# --------------------------------------------------------------------------- #
def classify(c: Company) -> None:
    host = (urlparse(c.careers_url).hostname or "").lower()
    if "linkedin.com" in host:
        c.link_type, c.status = "linkedin_excluded", "excluded"
        return
    found = detect(c.careers_url)
    if found:
        c.link_type = "ats_direct"
        apply_detection(c, found, "url")
    else:
        c.link_type = "company_site"


def inspect_with_http(c: Company, client: httpx.Client) -> None:
    # Direct ATS links only need a fetch when an identifier is missing
    # (Comeet token, which lives in the page source).
    if c.link_type == "ats_direct" and not (c.ats == "comeet" and not c.ats_token):
        return
    resp = request(client, "GET", c.careers_url)
    c.final_url = str(resp.url)
    if resp.status_code >= 400:
        c.status, c.error = "error", f"HTTP {resp.status_code}"
    if c.link_type == "ats_direct":  # Comeet: pick the token out of the page source
        tok = COMEET_API_TOKEN.search(resp.text) or COMEET_TOKEN.search(resp.text)
        if tok:
            c.ats_token = tok.group(1)
            c.api_endpoint = api_endpoint(c, "")
        return
    found = detect(c.final_url)
    how = "url"
    if not found:
        found, how = detect(resp.text), "html"
    if found:
        apply_detection(c, found, how)
        if c.status == "error":  # page answered with an error but still revealed the ATS
            c.status, c.error = "ok", ""


def guess_greenhouse_board(c: Company, client: httpx.Client) -> None:
    """Greenhouse hinted (gh_* params) but no board: try slugs from domain/name."""
    for cand in greenhouse_candidates(c):
        try:
            resp = request(client, "GET", f"https://boards-api.greenhouse.io/v1/boards/{cand}/jobs")
            if resp.status_code == 200 and "jobs" in resp.json():
                c.ats_id = cand
                c.detected_by += "+guess"
                c.api_endpoint = api_endpoint(c, "")
                return
        except Exception:
            continue


def fetch_comeet_token(c: Company, client: httpx.Client) -> None:
    """The company site links to Comeet but hides the token: read it from Comeet's hosted page."""
    try:
        resp = request(client, "GET", f"https://www.comeet.com/jobs/{c.ats_slug}/{c.ats_id}")
        tok = COMEET_TOKEN.search(resp.text)
        if tok:
            c.ats_token = tok.group(1)
            c.api_endpoint = api_endpoint(c, "")
            c.detected_by += "+comeet_page"
    except Exception as exc:  # fail soft
        c.error = (c.error + "; " if c.error else "") + f"comeet token: {exc}"[:200]


def verify_api(c: Company, client: httpx.Client) -> None:
    if c.ats == "greenhouse" and not c.ats_id:
        guess_greenhouse_board(c, client)
    if c.ats == "comeet" and c.ats_id and not c.ats_token and c.ats_slug:
        fetch_comeet_token(c, client)
    if not c.api_endpoint:
        return
    try:
        if c.ats == "workday":
            resp = request(
                client, "POST", c.api_endpoint,
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            )
        else:
            resp = request(client, "GET", c.api_endpoint)
        resp.raise_for_status()
        data = resp.json()
        if c.ats in ("greenhouse", "ashby"):
            n = len(data.get("jobs", []))
        elif c.ats == "smartrecruiters":
            n = data.get("totalFound", len(data.get("content", [])))
        elif c.ats == "workday":
            n = data.get("total", 0)
        else:  # lever, comeet return a list
            n = len(data)
        c.api_ok, c.jobs_count = "yes", str(n)
    except Exception as exc:  # fail soft
        c.api_ok = "no"
        c.error = (c.error + "; " if c.error else "") + f"api: {exc}"[:200]


def process(c: Company, client: httpx.Client) -> Company:
    try:
        classify(c)
        if c.status == "excluded":
            return c
        inspect_with_http(c, client)
    except Exception as exc:  # fail soft: one broken company never stops the run
        c.status, c.error = "error", f"{type(exc).__name__}: {exc}"[:200]
        log.warning("%s: %s", c.name, c.error)
    return c


RENDER_PAGE_TIMEOUT = 75  # seconds per company (incl. one hop); hung pages are abandoned


def render_fallback(companies: list[Company], workers: int) -> None:
    """Re-check company sites with no ATS hit using a real browser."""
    todo = [c for c in companies if c.link_type == "company_site" and not c.ats]
    if not todo:
        return
    log.info("Render fallback: %d pages, %d tabs", len(todo), workers)
    asyncio.run(_render_all(todo, workers))


async def _render_all(todo: list[Company], workers: int) -> None:
    from playwright.async_api import async_playwright

    sem = asyncio.Semaphore(workers)
    done = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(user_agent=USER_AGENT)

        async def one(c: Company) -> None:
            nonlocal done
            async with sem:
                try:
                    # Hard cap: a page that hangs (e.g. a frozen iframe) cannot stall the run.
                    await asyncio.wait_for(_render_company(context, c), RENDER_PAGE_TIMEOUT)
                except asyncio.TimeoutError:
                    c.error = c.error or "render: timeout"
                    log.warning("%s: render timeout", c.name)
                except Exception as exc:  # fail soft
                    c.error = c.error or f"render: {type(exc).__name__}"[:200]
                done += 1
                if done % 20 == 0:
                    log.info("  rendered %d/%d", done, len(todo))

        await asyncio.gather(*(one(c) for c in todo))
        try:
            await asyncio.wait_for(browser.close(), 30)
        except Exception:
            pass


async def _render_company(context, c: Company) -> None:
    text, url = await render(context, c.careers_url)
    c.final_url = c.final_url or url
    found, how = detect(text), "render"
    weak = hint(text, url)
    if not found:  # one hop: the "open positions" page often holds the ATS
        nxt = jobs_link(text, url)
        if nxt:
            text2, url2 = await render(context, nxt)
            found, how = detect(text2), "render+hop"
            if not weak:
                weak = hint(text2, url2)
                if weak:
                    c.final_url = url2
            if found:
                c.final_url = url2
    if not found and weak:
        found, how = weak, "hint"
    if found:
        apply_detection(c, found, how)
        c.status, c.error = "ok", ""


async def render(context, url: str) -> tuple[str, str]:
    """Render a page; return (page url + network urls + frame html, final url)."""
    page = await context.new_page()
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url))
    try:
        await asyncio.to_thread(LIMITER.wait, url)
        try:
            await page.goto(url, wait_until="load", timeout=25_000)
        except Exception:
            pass  # slow page: use whatever loaded
        await page.wait_for_timeout(2_500)  # let widgets (Comeet, Greenhouse embeds) load
        parts = [page.url, *seen]
        for frame in page.frames:
            parts.append(frame.url)
            try:
                parts.append(await asyncio.wait_for(frame.content(), 10))
            except Exception:
                pass
        return "\n".join(parts), page.url
    finally:
        try:
            await asyncio.wait_for(page.close(), 10)
        except Exception:
            pass


def write_csv(companies: list[Company], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel shows Hebrew
        w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Company)])
        w.writeheader()
        for c in companies:
            w.writerow(asdict(c))


def summarize(companies: list[Company]) -> None:
    from collections import Counter

    log.info("Link types: %s", dict(Counter(c.link_type for c in companies)))
    log.info("ATS: %s", dict(Counter(c.ats or "unknown" for c in companies if c.status != "excluded")))
    api = [c for c in companies if c.api_endpoint]
    log.info("Public-API companies: %d (verified ok: %d)", len(api), sum(c.api_ok == "yes" for c in api))
    log.info("Errors: %d", sum(c.status == "error" for c in companies))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/companies.csv")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N companies")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-render", action="store_true", help="skip the Playwright fallback")
    ap.add_argument("--no-verify", action="store_true", help="skip public API checks")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    companies = fetch_portal_companies()
    if args.limit:
        companies = companies[: args.limit]

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en,he;q=0.8"}
    with httpx.Client(headers=headers, timeout=TIMEOUT, follow_redirects=True) as client:
        with ThreadPoolExecutor(args.workers) as pool:
            list(pool.map(lambda c: process(c, client), companies))
        if not args.no_render:
            render_fallback(companies, workers=4)
        if not args.no_verify:
            with ThreadPoolExecutor(args.workers) as pool:
                list(pool.map(lambda c: verify_api(c, client), companies))

    write_csv(companies, Path(args.out))
    summarize(companies)
    log.info("Wrote %s", args.out)


if __name__ == "__main__":
    main()

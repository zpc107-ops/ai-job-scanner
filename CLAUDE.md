# CLAUDE.md — ai-job-scanner

## What this project is
An automated job-scanning pipeline that finds relevant jobs, scores each one against the user's CV with Claude, and sends a daily HTML digest email ranked by score (highest first).

- **Owner:** Zvika. Israel-based Release & Technical Project Manager.
- **Target roles:** TPM, Release Manager, Project / Delivery Manager, Chief of Staff, Technical Account Manager. Also Business Operations / transformation PM and combined PM + Systems Analyst roles.
- **Second user:** the pipeline also serves a second user (Eliad), who has his own CV and digest. Any new source must feed **both** users' scoring flows, not only Zvika's.

## Current architecture (live)
- **Orchestration:** n8n, workflow "Zvika — Daily Job Scanner v4", weekdays at 08:00 Israel time.
- **Source 1:** LinkedIn, scraped through Apify.
- **Scoring:** Claude API, using the CV text embedded in the scoring prompt.
- **Delivery:** Gmail, as an HTML digest sorted by score from high to low.

> First thing in a new session: explore the repo and fill in the "Repo map" section below with the real structure. Do not assume a layout.

## What we're building now: Source 2, company career pages
**Input:** the Elevation talent portal, https://elevation-hp-career.base44.app/

It is a Base44 app (JS-rendered) that lists about 276 partner companies. Each company has a name, a category tag (AI, Cyber, SAAS, Finance, …) and a "View Open Positions" link.

**Link mix (approximate):**
- About 222 links go to the company's own careers site. Many of these embed an ATS (applicant tracking system) such as Comeet, Greenhouse or Lever.
- About 25 links go straight to an ATS:
  - Comeet: 10
  - Workday: 3 (e.g. Intel, Flex)
  - Greenhouse: 2
  - Dueto: 3
  - Civi: 2
  - Adam: 2
  - Jobvite, Taleo, TopMatch, Wanted: 1 each
- 26 links go to LinkedIn, and at least 13 of those point to personal profiles or posts. **LinkedIn links are excluded.** LinkedIn is already covered by Source 1.

### Phases (one at a time; each phase must deliver value on its own)
1. **Company list + ATS detection**: one-off script, run locally. Produces `data/companies.csv`.
2. **n8n branch for ATS-API companies.** Fetch jobs from public ATS APIs inside n8n. Merge them with the LinkedIn jobs, dedupe, add a source tag, then reuse the same scoring and digest.
3. **Python scraper on GitHub Actions** for the remaining sites. It renders pages with Playwright, sends text to Claude for job extraction only when a page has changed, and pushes new jobs to n8n through a webhook every morning before the 08:00 run.
4. **Analysis**: trends by company, category and role, plus time-to-close.

**Current phase: 1 done (2026-11-11), Phase 2 next**

Phase 1 result: 276 companies. 26 excluded as LinkedIn. 64 with a verified public API (Comeet 41, Greenhouse 12, Workday 9, Ashby 2),
with about 10.3k open jobs worldwide (Workday counts are global, so Phase 2 must filter by Israel). 146 have an unknown ATS, which goes to Phase 3.
13 Comeet companies are only hinted (no uid/token found). 18 sites block us or are dead (403/404/DNS).
Lessons: (1) leading `([\w-]+)\.` regexes are quadratic on huge pages, so keep the `(?<![\w-])` lookbehind and the BLOB strip;
(2) sync Playwright `frame.content()` can hang forever, so rendering is async with `asyncio.wait_for` per company.

## Known public ATS endpoints
These are starting points. Verify each one against a live response before relying on it.

| ATS | Endpoint | Where the identifier comes from |
|---|---|---|
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true` | the `{board}` part of the job-board URL |
| Lever | `GET https://api.lever.co/v0/postings/{company}?mode=json` | the company slug in the Lever URL |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{name}` | the board name in the Ashby URL |
| SmartRecruiters | `GET https://api.smartrecruiters.com/v1/companies/{id}/postings` | the company identifier in the SmartRecruiters URL |
| Workday | `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | the tenant and site in the Workday URL |
| Comeet | careers API with company UID + token | both are embedded in the careers page or widget script. Find them in the page source or network calls |

## Engineering rules
- **Language and libraries:** Python 3.11+. Use `httpx` or `requests` for APIs and `playwright` for JS-rendered pages. Keep dependencies in `requirements.txt`.
- **Be polite to sites:**
  - Wait at least 1 second between requests to the same domain.
  - Set timeouts and retries with backoff.
  - Use a generic User-Agent such as `ai-job-scanner/1.0`. Never put a personal email in requests.
  - Run at most once a day.
- **Fail soft.** One broken company must never fail the whole run. Log it, mark it, and continue.
- **Secrets** (Anthropic API key, n8n webhook URL) go in `.env` locally and in GitHub Actions secrets. Never commit them.
- **Privacy:** never commit CV text, digest emails, or personal data. The repo may be public.
- **n8n:**
  - Export workflows as JSON into `n8n/` and edit them there.
  - Explain what changed so the user can re-import it.
  - Never assume access to the live n8n instance.
- **Dedupe key** across sources: normalized company name + normalized job title. When both sources have the same job, prefer the company-site link.
- **Before deleting files or changing the live schedule,** ask first.
- **Communication:** the user writes in Hebrew. Reply in Hebrew. Code, comments and commit messages are in English.
- Keep `README.md` updated when a phase ships.

## Repo map
_(mapped 2026-11-11)_

- `README.md`: project overview (Source 1 + Source 2 Phase 1 usage).
- `scripts/build_companies.py`: Phase 1. Portal (Playwright) → classify links → ATS fingerprinting
  (URL / httpx HTML / Playwright render + one hop / weak hints + Greenhouse board guess) → public API check
  → `data/companies.csv`. Fingerprints are in `PATTERNS` (strong, with ID) and `HINTS` (ATS known, no ID).
- `data/companies.csv`: Phase 1 output (public company data only; Comeet tokens are public page tokens).
- `requirements.txt`, `.env.example`, `.gitignore` (`.env`, `.venv/`, `private/`, `data/raw/` are ignored).
- Portal facts: Base44 app; company list is hard-coded in the JS bundle (no data API). The rendered DOM has
  276 `<a>` cards; card innerText = [logo initials?], name, CATEGORY, "View Open Positions →".
- Verified: Comeet API `https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={token}`; the uid+token
  appear as `COMPANY_DATA = {... "company_uid", "token"}` on comeet.com/jobs pages and as `"company-uid"`/`"token"` in
  embedded widgets. Greenhouse boards API also verified.
- `n8n/zvika-daily-job-scanner-v4.json` (+ `.png` screenshot): sanitized n8n export of the live workflow (13 nodes).
  Placeholders: `{{CV_TEXT ...}}`, `<PHONE>`, `<EMAIL>`, webhook path `REPLACE_WITH_WEBHOOK_PATH`; pinData removed. Flow:
  `Schedule — Weekdays 8am` → `Apify — Trigger Scraper Run` (actor task `zvika-tpm-scanner`) … Apify calls back →
  `Webhook` → `Extract Dataset ID` → `Fetch Jobs from Apify` → `Normalize Jobs` (code) → `Message a model`
  (Anthropic node, `claude-sonnet-4-6`, CV inlined in prompt) → `Parse Claude Response` → `Filter Score 55+` →
  `Aggregate` → `Compose Email` (code, HTML) → `Gmail — Send Digest`.
  New sources should merge in before `Normalize Jobs` / `Message a model`.
  ⚠️ Earlier git commits still contain the unsanitized export (CV, phone, email, webhook path).
- Eliad's workflow is **not** in the repo (the README says pipelines are duplicated per user).

Local environment: Windows 11, Node 24, Python 3.12 (user install, `%LOCALAPPDATA%\Programs\Python\Python312`),
project venv in `.venv/` with Playwright Chromium.

## Decisions log
- 2026-11-11: n8n exports are committed sanitized only (CV, contact details and webhook path replaced with placeholders, pinData dropped).
- 2026-11: Chose a hybrid approach. n8n handles ATS-API companies first (Phase 2). A GitHub Actions scraper handles the rest later (Phase 3). Both feed the existing digest email.

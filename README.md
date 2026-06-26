# 🤖 AI-Powered Daily Job Scanner

An automated pipeline that scrapes LinkedIn job listings daily, analyzes each role against a personal CV using Claude AI, and delivers a ranked email digest with match scores, gap analysis, CV improvement suggestions, and a tailored cover letter — all before 8:00 AM.

---

## 🎯 The Problem

Manual job searching on LinkedIn is time-consuming and inconsistent:
- Scrolling through hundreds of irrelevant listings daily
- Evaluating fit without a structured framework
- Writing cover letters from scratch for each application
- Missing relevant opportunities posted overnight

## 💡 The Solution

A fully automated daily pipeline that handles the entire job discovery and evaluation process end-to-end — delivering only pre-filtered, ranked opportunities directly to your inbox.

---

## 🏗️ Architecture

```
Schedule (08:00)
      ↓
Apify LinkedIn Scraper  ──→  Webhook Trigger
      ↓
Extract & Normalize Jobs (deduplicate, filter)
      ↓
Claude API (analyze each job vs. CV)
      ↓
Parse & Score Results
      ↓
Filter (score ≥ 55)
      ↓
Aggregate → Compose Email (HTML, sorted by score)
      ↓
Gmail → Daily Digest
```

---

## ⚙️ Tech Stack

| Component | Tool | Purpose |
|-----------|------|---------|
| Workflow Automation | [n8n](https://n8n.io) | Orchestrates the full pipeline |
| Job Scraping | [Apify](https://apify.com) | LinkedIn Jobs Scraper |
| AI Analysis | [Claude API](https://anthropic.com) (claude-sonnet-4-6) | Job fit analysis & cover letter generation |
| Hosting | [Railway](https://railway.app) | Self-hosted n8n instance |
| Delivery | Gmail | Daily email digest |

---

## 📊 What the Pipeline Produces

For each job listing that passes the score threshold, the email digest includes:

- **Match Score** (0–100) with color-coded verdict
- **Probability** of getting an interview
- **Strengths** — specific CV elements that map to the role
- **Gaps** — honest assessment of what's missing
- **CV Improvement Suggestions** — how to rephrase existing entries for this specific role
- **Recommended Courses** — short courses (under 10 hours) to bridge skill gaps
- **Tailored Cover Letter** — 150–200 words, written in the candidate's voice
- **Direct Application Link** — one-click to the LinkedIn listing

Jobs are sorted from highest to lowest match score.

---

## 🔄 Pipeline Flow in Detail

### 1. Trigger (Schedule Node)
Runs Monday–Sunday at 08:00 AM (Asia/Jerusalem timezone).

### 2. Apify Scraper
Searches LinkedIn for role-specific keywords with configurable parameters:
```json
{
  "keyword": ["TPM", "Technical Project Manager", "Release Manager", "Delivery Manager"],
  "location": "Israel",
  "maxItems": 150,
  "publishedAt": "r86400"
}
```

### 3. Normalize & Deduplicate
JavaScript node that:
- Flattens the Apify response structure
- Removes duplicate listings (by URL or title+company)
- Truncates descriptions to 2,000 characters
- Filters out listings with no title or very short descriptions

### 4. Claude AI Analysis
Each job is sent to Claude with the candidate's CV and a strict scoring prompt:

**Scoring Rules:**
- 80–100: Strong TPM/Release Manager match, candidate meets 80%+ of requirements
- 65–79: Good match, delivery-focused role
- 55–64: Borderline — included only if core responsibilities align
- Below 55: Filtered out

**Automatic disqualifiers** (score capped at 20–35):
- Roles requiring hands-on software development
- Pure Product Manager roles (roadmap ownership)
- Heavy industry/manufacturing with no domain overlap

### 5. Filter & Aggregate
Only listings scoring ≥ 55 proceed. Results are aggregated into a single payload.

### 6. Email Composition & Delivery
HTML email generated with:
- Summary header (count, date)
- Sortable table of all qualifying roles
- Expandable sections per job with full analysis
- Direct "Apply" button linking to LinkedIn

---

## 📧 Sample Output

```
🎯 8 משרות חדשות לצביקה — 18.6.2026

נמצאו 8 משרות עם ציון 55+

| תפקיד              | חברה        | ציון | סיכוי | קישור  |
|--------------------|-------------|------|-------|--------|
| Project Manager    | Global-e    | 78   | 28%   | [הגש]  |
| Release Manager    | Logica-IT   | 74   | 25%   | [הגש]  |
| Strategic PM       | Nogamy      | 74   | 25%   | [הגש]  |
| ...                | ...         | ...  | ...   | ...    |
```

---

## 🔧 Key Engineering Decisions

**Why n8n over Zapier/Make?**
Self-hosted on Railway for unlimited executions at ~$3–5/month vs. $24+/month for cloud-based alternatives. Full control over data and no execution limits.

**Why Claude over GPT-4?**
Superior instruction-following for structured JSON output and nuanced CV-to-job matching. Consistent formatting across 100+ items per run.

**Deduplication Strategy**
Jobs are deduplicated by URL as primary key, with title+company as fallback. This prevents the same listing from appearing across multiple keyword searches.

**Multi-user Support**
The pipeline is duplicated per user, each with:
- Their own Apify Saved Task (different keyword sets)
- Their own Webhook URL
- Their own Claude prompt (tailored CV + scoring instructions)
- Their own Gmail delivery

---

## 📈 Results

- **~120–150 jobs scraped** per daily run
- **10–20 jobs delivered** after scoring filter (55+ threshold)
- **Processing time:** 10–30 minutes per run
- **Cost:** ~$0.10–0.30/day in Claude API usage + ~$3–5/month Railway hosting

---

## 🚀 Setup Overview

1. **n8n on Railway** — deploy from Railway template
2. **Apify Account** — configure LinkedIn scraper with role-specific keywords
3. **Anthropic API Key** — connect to n8n Anthropic credential
4. **Gmail OAuth** — authorize via Google Cloud Console
5. **Webhook** — connect Apify run completion to n8n webhook URL

---

## 🛠️ Built By

**Zvika Rettig** — Technical Project Manager & Release Manager  
[LinkedIn](https://linkedin.com/in/zvika-rettig) | zpc107@gmail.com

> *Built this tool while actively job searching — because automating the search is exactly what a TPM should do.*

<div align="center">

# 🎯 JobHunter

**Automated internship discovery, scoring, and application assistant for engineering students**

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/Tests-316%20passing-22c55e?style=flat)](tests/)
[![Connectors](https://img.shields.io/badge/Connectors-14%20sources-6366f1?style=flat)](#connectors)
[![License](https://img.shields.io/badge/License-MIT-f59e0b?style=flat)](LICENSE)

JobHunter continuously scans 14 job sources, scores every listing against your engineering profile, removes duplicates, sends you alerts for high-match roles, and helps fill application forms — always stopping before the submit button.

[Quick Start](#quick-start) · [Features](#features) · [Connectors](#connectors) · [Dashboard](#dashboard) · [Application Assistant](#application-assistant) · [Configuration](#configuration) · [Docker](#docker)

</div>

---

## Features

- **14 job source connectors** — company career pages (Bosch, BMW, Baker Hughes, Siemens, Continental, Knorr-Bremse, Valeo, ZF, ABB), EU job boards (EURES, Profession.hu, Jooble, Graduateland), and IMAP email alert parsing (LinkedIn, Indeed, Glassdoor)
- **Deterministic relevance scoring** — 0–100 score with full explanation for every job
- **Deduplication** — fingerprint + fuzzy matching across sources; official career page always preferred
- **Streamlit dashboard** — filter, save, track, and manage applications in a browser UI
- **Email + Telegram notifications** — instant alerts for high-match jobs, deadline warnings, daily digest
- **Application form assistant** — fills safe fields automatically, always pauses before submit (5-layer guard)
- **ATS adapters** — Workday, Greenhouse, and SAP SuccessFactors (covers most large manufacturers)
- **AI analysis** — optional job summarisation, gap analysis, CV keyword suggestions, cover letter drafting (Anthropic or OpenAI)
- **CV tailoring** — keyword diff between your CV and a job description, ATS score estimate
- **Export** — CSV download or Google Sheets sync
- **Scheduler** — runs ingestion every N hours, sends daily digest, checks deadlines
- **Security checker** — audits for committed secrets, database files, and missing `.gitignore` patterns

---

## Quick Start

### Prerequisites

- Python 3.12 or newer
- Git

### 1 — Clone and install

```bash
git clone https://github.com/nourelshawaf/JobHunter.git
cd JobHunter
pip install -e ".[dev]"
```

### 2 — Configure

```bash
cp .env.example .env          # add your credentials (see Configuration below)
cp config.example.yaml config.yaml   # edit keywords, connectors, locations
```

### 3 — Set up the database

```bash
alembic upgrade head
```

### 4 — Run tests (optional but recommended)

```bash
JOBHUNTER_TESTING=1 pytest tests/ -v
# 316 tests — all should pass
```

### 5 — Start the dashboard

```bash
streamlit run jobhunter/dashboard/app.py
# Opens at http://localhost:8501
```

### 6 — Run your first ingestion

```bash
python -m jobhunter.cli ingest
```

That's it. Jobs will appear in the dashboard, scored and deduplicated.

---

## Configuration

### `.env` — secrets and credentials

```bash
# Required for ingestion (no credentials = no external sources)
# Everything has a safe default — start with just the database URL

# ── Database ─────────────────────────────────────────
DATABASE_URL=sqlite:///./data/jobhunter.db
# PostgreSQL: DATABASE_URL=postgresql://user:password@localhost:5432/jobhunter

# ── Email alert ingestion (LinkedIn/Indeed alert emails via IMAP) ─────────
ALERT_EMAIL_HOST=imap.gmail.com
ALERT_EMAIL_PORT=993
ALERT_EMAIL_USER=you@gmail.com
ALERT_EMAIL_PASSWORD=your-16-char-app-password   # not your account password

# ── Outbound notifications ────────────────────────────
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-16-char-app-password
NOTIFY_TO_EMAIL=you@gmail.com

# ── Telegram (optional) ───────────────────────────────
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── AI analysis (optional, disabled by default) ───────
AI_PROVIDER=none     # anthropic | openai | none
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
```

### `config.yaml` — search behaviour

Copy `config.example.yaml` to `config.yaml`. Key settings:

```yaml
connectors:
  enabled:
    - profession_hu          # Hungarian job board
    - eures                  # EU employment portal
    - bosch_careers          # Bosch official careers
    - bmw_careers            # BMW Group careers
    - baker_hughes_careers   # Baker Hughes (Workday)
    - siemens_careers        # Siemens careers
    - continental_careers    # Continental careers
    - knorr_bremse_careers   # Knorr-Bremse careers
    - valeo_careers          # Valeo careers
    - zf_careers             # ZF Group careers
    - abb_careers            # ABB careers
    - jooble_rss             # Jooble RSS feed
    - graduateland           # Graduateland EU student platform
    - email_alerts           # IMAP email alert parser

scoring:
  min_score_to_notify: 75    # send notification above this score
  min_score_to_save: 40      # auto-save above this score
  auto_reject_below: 20      # auto-reject below this score

notifications:
  daily_summary_time: "08:00"
  channels:
    - email
    # - telegram
```

### Gmail app password (for IMAP and SMTP)

1. Go to your Google account → Security → 2-Step Verification (must be enabled)
2. Search for "App passwords" → create one for "Mail"
3. Use the 16-character code as `ALERT_EMAIL_PASSWORD` and `SMTP_PASSWORD`

### LinkedIn / Indeed email alerts

1. On LinkedIn: Jobs → Job Alerts → create an alert → set frequency to Daily
2. On Indeed: run a search → "Get email alerts for this search"
3. Alert emails land in your inbox and are parsed automatically on the next ingestion run

---

## CLI Reference

```bash
python -m jobhunter.cli --help

# Database
python -m jobhunter.cli migrate            # apply pending migrations

# Ingestion
python -m jobhunter.cli ingest             # run all enabled connectors once
python -m jobhunter.cli ingest -c bosch_careers -c eures   # specific connectors only

# Scheduler
python -m jobhunter.cli scheduler          # daemon mode — runs until Ctrl+C
python -m jobhunter.cli scheduler --once   # run all jobs once and exit
python -m jobhunter.cli scheduler --dry-run  # print schedule without executing

# Dashboard
python -m jobhunter.cli serve              # start Streamlit on port 8501

# Status
python -m jobhunter.cli status             # database summary table

# Email
python -m jobhunter.cli test-email         # verify IMAP connection

# Export
python -m jobhunter.cli export-csv                            # → data/applications.csv
python -m jobhunter.cli export-csv -o ~/jobs.csv --min-score 60
python -m jobhunter.cli export-sheets YOUR_SPREADSHEET_ID --credentials creds.json

# Security
python -m jobhunter.cli security-check     # audit for committed secrets / missing gitignore
```

---

## Dashboard

Start with `streamlit run jobhunter/dashboard/app.py` or `python -m jobhunter.cli serve`.

| Tab | Description |
|-----|-------------|
| 🆕 New Jobs | Freshly scored jobs not yet saved or rejected |
| ⭐ High Match | Jobs scoring ≥ your notification threshold |
| 💾 Saved | Jobs you've bookmarked for applications |
| 📋 In Progress | Active applications and their current state |
| 🔍 All Jobs | Full database with filters |
| 📊 Stats | Score distribution, source breakdown, top matches |
| 🖊 Apply | Browser-assisted application form filling |

Sidebar controls: min score filter, source filter, work-mode filter, run ingestion, download CSV.

---

## Connectors

| Name | Source | Method |
|------|--------|--------|
| `profession_hu` | Profession.hu | HTML scraping (public search pages) |
| `eures` | EURES EU Jobs | Public JSON API |
| `bosch_careers` | Bosch Careers | SAP API + JSON-LD fallback |
| `bmw_careers` | BMW Group Careers | SmartRecruiters JSON API + JSON-LD fallback |
| `baker_hughes_careers` | Baker Hughes | Workday JSON API + JSON-LD fallback |
| `siemens_careers` | Siemens Jobs | SAP SuccessFactors API + JSON-LD fallback |
| `continental_careers` | Continental | JSON-LD + HTML card fallback |
| `knorr_bremse_careers` | Knorr-Bremse | SAP API + JSON-LD fallback |
| `valeo_careers` | Valeo | Oracle Taleo API + JSON-LD fallback |
| `zf_careers` | ZF Group | SAP API + JSON-LD fallback |
| `abb_careers` | ABB | Workday JSON API + JSON-LD fallback |
| `jooble_rss` | Jooble | Public RSS feeds (Hungary keyword × location combos) |
| `graduateland` | Graduateland | Public JSON API + JSON-LD fallback |
| `email_alerts` | Your inbox | IMAP parser for LinkedIn / Indeed / Glassdoor alert emails |

All connectors isolate failures — one broken source never stops the others.

---

## Relevance Scoring

Every job is scored 0–100 with a full explanation string.

| Factor | Points |
|--------|--------|
| Core domain match (robotics, mechatronics, automation, embedded, computer vision, AI) | +25 max |
| Internship / working-student / trainee classification | +15 |
| Budapest or Debrecen | +10 |
| English-only role | +10 |
| Technical skill match (Python, C++, ROS 2, OpenCV, PLC, MATLAB…) | +15 max |
| Accepts current BSc students | +8 |
| Hybrid or remote | +4 |
| Posted within last 7 days | +5 |
| Priority company match | +5 |
| Mandatory Hungarian language | −20 |
| Requires 2+ years experience | −10 |
| Requires 3+ years experience | −20 |
| Senior / manager / director in title | auto-reject |
| Deadline passed | auto-reject |

Example explanation: `82/100: +25 domain match: robotics, mechatronics. +15 internship role. +10 Budapest. +8 accepts BSc students. +10 English accessible. +5 Python, C++. +5 priority company: Bosch. −6 experience requirement: 1 year.`

---

## Application Assistant

The system helps fill application forms but **never submits automatically**. The submit guard operates at five independent layers:

1. `_submit_is_forbidden()` — adapter-level method always returns `True`
2. `_safe_click()` — checks every button label against `SUBMIT_PATTERNS` before clicking
3. Aria-label, value, and inner text inspection on every element
4. State machine — adapter moves to `READY_FOR_FINAL_REVIEW` and pauses
5. Tests — `test_no_auto_submit_ever()` proves the guard cannot be bypassed

### Supported ATS platforms

| Adapter | Covers |
|---------|--------|
| Workday | Bosch, BMW, Baker Hughes, ABB, GE, Honeywell, and more |
| Greenhouse | Many tech and scale-up companies |
| SAP SuccessFactors | Siemens, Continental, ZF, Knorr-Bremse, BASF, and more |

### Sensitive fields — always require manual input

Salary expectations · disability · ethnicity · gender · veteran status · criminal history · visa sponsorship · work authorisation · legal declarations · data consent · relocation commitment · start-date commitment · conflict of interest

### How to use it

```
1. Score and save a job in the dashboard
2. Go to the Apply tab → select the job
3. Enter the path to your approved CV
4. Click "Start application assistance"
5. Review the summary: auto-filled fields, sensitive fields, missing required fields
6. Open the application URL in your browser
7. Review every field carefully
8. Submit manually
```

---

## Notifications

### Telegram setup

```
1. Message @BotFather → /newbot → follow prompts → copy the token
2. Start a chat with your bot → send /start
3. Visit: https://api.telegram.org/bot<TOKEN>/getUpdates
4. Copy the "id" value from the result
5. Add to .env:
     TELEGRAM_BOT_TOKEN=<token>
     TELEGRAM_CHAT_ID=<id>
6. Add to config.yaml notifications.channels: [email, telegram]
```

Sends: new high-match jobs · deadline warnings (3 days, 7 days) · daily digest · connector failures · applications ready for review

### Email setup

Uses Gmail app passwords — see the Configuration section above.

---

## AI Analysis (optional)

Disabled by default (`AI_PROVIDER=none`). Set in `.env` to enable:

```bash
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
# or
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

Adds to each job:
- Role summary (2–3 sentences)
- Match explanation grounded in your profile
- Skill gaps
- CV keyword suggestions
- Draft application answers
- Cover letter

**Rules enforced:** never invents skills or experience, every claim linked to profile evidence, missing evidence triggers a warning not a fabrication.

---

## CV Tailoring

```python
from jobhunter.cv_tailor import CVTailor

cv_text = open("my_cv.txt").read()
jd_text = open("job_description.txt").read()

result = CVTailor().tailor(cv_text, jd_text, use_ai=False)

print(result.summary)
# "Found 8/12 JD keywords in CV (ATS estimate: good).
#  4 keywords missing. 0 suggested rewrites. 4 genuine gaps."

print(result.keywords_found)   # ['python', 'c++', 'embedded', ...]
print(result.keywords_missing) # ['ros 2', 'opencv', ...]
print(result.gaps)             # keywords you genuinely don't have yet
```

With `use_ai=True` and a configured AI provider, also generates specific bullet-point rewrites grounded in your existing experience.

---

## Export

```bash
# CSV (always works, no credentials needed)
python -m jobhunter.cli export-csv
python -m jobhunter.cli export-csv -o ~/Desktop/internships.csv --min-score 60

# Google Sheets
# 1. Create a Google Cloud project, enable Sheets + Drive APIs
# 2. Create a service account, download the JSON key
# 3. Share your spreadsheet with the service account email (Editor)
python -m jobhunter.cli export-sheets YOUR_SPREADSHEET_ID --credentials path/to/key.json
```

---

## Docker

```bash
# Start dashboard + background scheduler
docker compose up

# One-shot ingestion
docker compose run --rm ingest

# Apply pending migrations
docker compose run --rm migrate

# Build image only
docker compose build
```

Switch to PostgreSQL: change `DATABASE_URL` in `.env` to a PostgreSQL connection string, run `docker compose up`, and Alembic handles the rest.

---

## Development

```bash
# Run tests
JOBHUNTER_TESTING=1 pytest tests/ -v               # 316 tests
JOBHUNTER_TESTING=1 pytest tests/ --cov=jobhunter  # with coverage

# Linting / type checking
ruff check jobhunter/
mypy jobhunter/

# Generate a new migration after changing models
alembic revision --autogenerate -m "add column X"
alembic upgrade head
```

### Adding a connector

```python
# jobhunter/connectors/company/mycompany.py
from jobhunter.connectors.base import BaseConnector, RawJob

class MyCompanyConnector(BaseConnector):
    name = "mycompany_careers"

    async def _fetch_jobs(self) -> list[RawJob]:
        response = await self._get("https://careers.mycompany.com/jobs.json")
        # ... parse and return list[RawJob]
```

Then register in `pipeline.py` and add to `config.yaml`.

### Adding an ATS adapter

```python
# jobhunter/application/adapters/myats.py
from jobhunter.application.base_adapter import AdapterRegistry, BaseApplicationAdapter

@AdapterRegistry.register
class MyATSAdapter(BaseApplicationAdapter):
    name = "myats"
    URL_PATTERNS = ["jobs.myats.com"]

    async def _detect_fields(self): ...
    async def _fill_fields(self, session): ...
    # Never call _click_submit() — the guard catches it
```

---

## Security

```bash
python -m jobhunter.cli security-check
```

Checks for: committed `.env` with real credentials · database files outside `data/` · hardcoded API keys in source · missing `.gitignore` patterns · browser profile directories in tracked paths.

**Never commit:** `.env` · `*.db` · `data/` · `logs/` · `*.pdf` · `*.docx` · `browser_profiles/`

---

## Project structure

```
jobhunter/
├── jobhunter/
│   ├── connectors/
│   │   ├── base.py               # BaseConnector (rate limiting, retries, backoff)
│   │   ├── boards/               # Profession.hu, EURES, RSS, Jooble, Graduateland
│   │   └── company/              # Bosch, BMW, Baker Hughes, Siemens, Continental,
│   │                             # Knorr-Bremse, Valeo, ZF, ABB
│   ├── models/                   # SQLAlchemy ORM models
│   ├── normalisation/            # RawJob → Job field mapping
│   ├── deduplication/            # Fingerprint + fuzzy matching
│   ├── scoring/                  # Deterministic 0-100 rule engine
│   ├── application/
│   │   ├── base_adapter.py       # 5-layer submit guard + AdapterRegistry
│   │   └── adapters/             # Workday, Greenhouse, SAP SuccessFactors
│   ├── notifications/            # Email + Telegram notifiers
│   ├── dashboard/                # Streamlit UI
│   ├── ai_provider.py            # Anthropic / OpenAI / Null providers
│   ├── cv_tailor.py              # Keyword diff + ATS score estimate
│   ├── export.py                 # CSV + Google Sheets export
│   ├── scheduler.py              # APScheduler daemon
│   ├── security.py               # Security audit checker
│   ├── state_machine.py          # Application status transitions + audit log
│   ├── pipeline.py               # Orchestrates connector → normalise → dedup → score → save
│   ├── database.py               # SQLAlchemy engine (SQLite + PostgreSQL)
│   ├── config.py                 # Pydantic settings + YAML search config
│   └── cli.py                    # Click CLI (9 commands)
├── tests/                        # 316 tests across 12 test files
├── migrations/                   # Alembic migrations
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── config.example.yaml
└── README.md
```

---

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">
Built for engineering students hunting internships in Hungary and across Europe.<br>
<strong>Bosch · BMW · Baker Hughes · Siemens · Continental · Knorr-Bremse · Valeo · ZF · ABB</strong>
</div>

# HR Email Finder

Finds a company's **public** HR / recruiting contact email, with evidence: the page it was
found on, the text around it, and a score that explains itself. If the company publishes no HR
email, it says so and points to the careers page or contact form instead.

This is the version 1 MVP from [DESIGN.md](DESIGN.md) §10 step 1: Company Resolver, Website
Agent, Verifier and Scorer, behind a command-line tool. Web-search, job-board, orchestrator and
the version 2 upgrades come later.

## Install

```bash
cd hr-email-finder
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=...   # optional — see "Running without Claude"
```

Python 3.10+.

## Use

```bash
# Name only: Claude searches the web for the official domain, then reads the site
hr-email-finder "Infosys" --country IN

# Domain known: skips the web search (cheaper)
hr-email-finder "Infosys" --domain infosys.com

# No API key: rule-based crawl of the site you name
hr-email-finder "Infosys" --domain infosys.com --no-llm

# Full JSON report
hr-email-finder "Infosys" --domain infosys.com --json
```

Example output:

```
Company:  Acme (acme.test)
Status:   found

Best HR email
  careers@acme.test   score 0.70   HR
    source:  https://acme.test/careers  (careers page)
    context: "Join our team We're hiring engineers. Send your CV to careers@acme.test and …"
    why:     found on the company's careers page (+0.45); HR or hiring words next to it (+0.15); role-based HR address (+0.10); mailbox not checked

Alternatives
  info@acme.test   score 0.30   General
    https://acme.test/

Careers page:  https://acme.test/careers
Contact form:  https://acme.test/contact
```

Options:

| Flag | What it does |
|---|---|
| `--domain` | company website; skips the Resolver's web search |
| `--country` | ISO country hint (`IN`, `US`, …) for the Resolver |
| `--linkedin-url` | LinkedIn company page, as a Resolver hint |
| `--no-llm` | rule-based crawl only (requires `--domain`) |
| `--allow-named-contacts` | also return personal addresses such as `priya.sharma@…` (off by default, see DESIGN.md §8) |
| `--smtp-probe` | check mailboxes with an SMTP `RCPT TO` probe (off by default — often blocked, and can get your IP blocklisted) |
| `--model` | `claude-opus-5-5` (default) or `claude-sonnet-5-5` |
| `--effort` | `low` (default), `medium`, `high` |
| `--max-pages` | page fetch budget per lookup (default 20) |
| `--json`, `-v` | JSON output; verbose logging |

From Python:

```python
import asyncio
from hr_email_finder import CompanyQuery, find_hr_email

report = asyncio.run(find_hr_email(CompanyQuery(name="Acme", domain="acme.com")))
print(report.best_email.candidate.email if report.best_email else report.explanation)
```

## How it works

1. **Company Resolver** (Claude + web search) — only when no `--domain` is given. Finds the
   official name and domains; if the name is ambiguous it stops and lists the options.
2. **Website Agent** (Claude) — reads the site through two tools, `list_site_pages` and
   `fetch_page`, looking at careers, contact, imprint/impressum and privacy pages first, then
   reports what it found with `submit_findings`. Any email it reports that wasn't on a fetched
   page is dropped. Without an API key (or if the agent fails), a rule-based crawler does the
   same job.
3. **Extraction** handles `mailto:` links, `[at]`/`(dot)` obfuscation, Cloudflare email
   protection and JSON-LD. Whether an email has "HR words nearby" is judged from the
   surrounding prose, so a menu's "Careers" link doesn't count.
4. **Verifier** — syntax, company-owned domain, MX record, free/disposable provider, and that the
   email really is on its source page. Mailbox check only with `--smtp-probe`.
5. **Scorer** — the weighted rules in DESIGN.md §3.7; every point comes with a reason.

The crawler obeys `robots.txt` (RFC 9309), uses a per-host concurrency limit and any
`Crawl-delay`, stays on the company's domains plus known job-application systems (Greenhouse,
Lever, Workday, …), and sends the user agent in `HR_FINDER_USER_AGENT` (a default is provided).

Personal addresses (`firstname.lastname@`) are hidden unless you pass `--allow-named-contacts`;
`sales@`, `press@`, `privacy@` and similar are left out entirely.

## Cost

The report shows `Claude cost` (tokens at list price, excluding web search fees) and the number
of web searches. Giving `--domain` skips the Resolver and its searches; `--no-llm` costs nothing.

## Development

```bash
pytest
```

The tests run offline: websites are served by `httpx.MockTransport`, DNS is stubbed, and Claude
is replaced by a scripted fake client, so they cost nothing and need no API key.

Layout:

```
hr_email_finder/
  cli.py            command line
  pipeline.py       resolve → crawl → verify → score → report
  agents/           resolver.py, website.py (Claude agents + rule-based crawler)
  llm.py            agent loop, cost tracking
  tools/            http.py (fetcher), site.py (crawler), html.py, extract.py, mx.py, verify.py
  scoring.py        DESIGN.md §3.7
  schemas.py        DESIGN.md §4
tests/
```

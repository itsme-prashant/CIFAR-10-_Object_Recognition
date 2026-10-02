"""Command line: hr-email-finder "Company name" [--domain acme.com] [...]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import anthropic

from .config import EFFORT_LEVELS, SUPPORTED_MODELS, ConfigError, Settings
from .llm import AgentError
from .pipeline import find_hr_email
from .schemas import CompanyQuery, FinalReport, RankedCandidate


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hr-email-finder",
        description="Find a company's public HR / recruiting contact email, with evidence.",
    )
    p.add_argument("company", help="company name, e.g. 'Infosys'")
    p.add_argument("--domain", help="the company's website domain; skips the web-search step")
    p.add_argument("--country", help="country hint, ISO code such as IN or US")
    p.add_argument("--linkedin-url", help="LinkedIn company page URL, as a hint for the resolver")
    p.add_argument("--allow-named-contacts", action="store_true",
                   help="also return personal addresses (e.g. priya.sharma@...) — see DESIGN.md §8")
    p.add_argument("--no-llm", action="store_true", help="rule-based crawl only (needs --domain)")
    p.add_argument("--smtp-probe", action="store_true",
                   help="check mailboxes with an SMTP RCPT probe (off by default; may be blocked)")
    p.add_argument("--model", choices=SUPPORTED_MODELS, default=Settings.model)
    p.add_argument("--effort", choices=EFFORT_LEVELS, default=Settings.effort)
    p.add_argument("--max-pages", type=int, default=Settings.max_pages, help="page fetch budget")
    p.add_argument("--json", action="store_true", help="print the full report as JSON")
    p.add_argument("-v", "--verbose", action="store_true", help="log fetches and agent turns")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings(
            model=args.model,
            effort=args.effort,
            use_llm=not args.no_llm,
            max_pages=args.max_pages,
            smtp_probe=args.smtp_probe,
        )
        query = CompanyQuery(
            name=args.company,
            domain=args.domain,
            country=args.country,
            linkedin_url=args.linkedin_url,
            allow_named_contacts=args.allow_named_contacts,
        )
        report = asyncio.run(find_hr_email(query, settings))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except anthropic.AuthenticationError:
        print("error: Claude rejected the API key. Check ANTHROPIC_API_KEY, or run with --no-llm --domain <domain>.",
              file=sys.stderr)
        return 2
    except (AgentError, anthropic.APIError) as exc:
        print(f"error: {getattr(exc, 'message', None) or exc}", file=sys.stderr)
        return 1

    print(report.model_dump_json(indent=2) if args.json else render(report))
    return 0


def render(report: FinalReport) -> str:
    sections: list[list[str]] = []
    header = []
    if report.company:
        header.append(f"Company:  {report.company.canonical_name} ({', '.join(report.company.domains)})")
    header.append(f"Status:   {report.status.replace('_', ' ')}")
    sections.append(header)
    if report.best_email:
        sections.append(["Best HR email", *_candidate_lines(report.best_email, detailed=True)])
    if report.alternatives:
        title = "Alternatives" if report.best_email else "Other contact addresses"
        sections.append([title, *(line for alt in report.alternatives for line in _candidate_lines(alt, detailed=False))])
    pages = []
    if report.careers_portal_url:
        pages.append(f"Careers page:  {report.careers_portal_url}")
    if report.contact_form_url:
        pages.append(f"Contact form:  {report.contact_form_url}")
    if pages:
        sections.append(pages)
    sections.append([report.explanation])
    if report.notes:
        sections.append(["Notes", *(f"  - {note}" for note in report.notes)])
    sections.append([
        f"Pages fetched: {report.pages_fetched} · Web searches: {report.web_searches} · "
        f"Claude cost: ${report.cost_usd:.4f} · Time: {report.duration_ms / 1000:.1f}s"
    ])
    return "\n\n".join("\n".join(section) for section in sections)


def _candidate_lines(r: RankedCandidate, *, detailed: bool) -> list[str]:
    c = r.candidate
    lines = [f"  {c.email}   score {r.score:.2f}   {c.department_hint or ''}".rstrip()]
    if detailed:
        lines.append(f"    source:  {c.source_url}  ({c.source_page_type} page)")
        if c.snippet:
            lines.append(f'    context: "{_shorten(c.snippet, c.email)}"')
        lines.append(f"    why:     {'; '.join(r.reasons)}")
    else:
        lines.append(f"    {c.source_url}")
    return lines


def _shorten(snippet: str, email: str, radius: int = 90) -> str:
    i = snippet.lower().find(email.lower())
    if i < 0:
        return snippet[: 2 * radius]
    start, end = max(0, i - radius), i + len(email) + radius
    return ("…" if start else "") + snippet[start:end] + ("…" if end < len(snippet) else "")

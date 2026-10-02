"""The version 1 lookup: resolve → crawl website → verify → score → report (DESIGN.md §2, §10)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import anthropic
import httpx

from .agents.resolver import resolve_company
from .agents.website import WebsiteFindings, search_website_rule_based, search_website_with_agent
from .config import ConfigError, Settings
from .domains import normalize_domain
from .llm import AgentError, Usage
from .schemas import CandidateKind, CompanyProfile, CompanyQuery, DepartmentHint, EmailCandidate, FinalReport, RankedCandidate
from .scoring import BEST_MIN_SCORE, rank, score_candidate
from .signals import classify_local_part
from .tools.extract import FoundEmail
from .tools.http import Fetcher
from .tools.mx import lookup_mx
from .tools.site import PAGE_PRIORITY, PageRecord, SiteCrawler
from .tools.verify import MailboxVerifier, NoMailboxCheck, SmtpProbe, Verifier

log = logging.getLogger(__name__)

AMBIGUITY_CONFIDENCE = 0.6
MAX_ALTERNATIVES = 5
_HR_HINTS = ("HR", "Recruiting", "Campus")
_KIND_BY_CLASS: dict[str, CandidateKind] = {
    "hr_role": "role_based",
    "general": "generic",
    "other_department": "generic",
    "personal": "named_person",
}


async def find_hr_email(
    query: CompanyQuery,
    settings: Settings | None = None,
    *,
    client: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
    mx_lookup: Callable[[str], Awaitable[list[str] | None]] = lookup_mx,
    mailbox: MailboxVerifier | None = None,
) -> FinalReport:
    settings = settings or Settings()
    started = time.monotonic()
    usage = Usage()
    notes: list[str] = []
    owned_client = None
    if settings.use_llm and client is None:
        client = owned_client = anthropic.AsyncAnthropic()
        if not (client.api_key or client.auth_token or client.credentials):
            client = None
            if not query.domain:
                await owned_client.close()
                raise ConfigError(
                    "No Anthropic credentials found. Set ANTHROPIC_API_KEY, or give the company's "
                    "domain (--domain) to run without Claude."
                )
            notes.append("No Anthropic credentials found, so the rule-based crawler was used.")
    if not settings.use_llm:
        client = None

    try:
        async with Fetcher(settings, transport=transport) as fetcher:
            profile = await _resolve(query, settings, client, usage)
            if profile.alternatives and profile.confidence < AMBIGUITY_CONFIDENCE:
                return _ambiguous_report(query, profile, usage, started)

            crawler = SiteCrawler(fetcher, profile.domains, settings)
            findings = await _search_website(crawler, profile, settings, client, usage, notes)
            profile = profile.model_copy(update={"domains": list(crawler.domains)})

            candidates = build_candidates(crawler, findings, query.allow_named_contacts, notes)
            verifier = Verifier(
                profile.domains,
                mailbox=mailbox or (SmtpProbe() if settings.smtp_probe else NoMailboxCheck()),
                mx_lookup=mx_lookup,
                evidence=crawler.emails_on,
            )
            checks = await asyncio.gather(*(verifier.verify(c) for c in candidates))
            pages_fetched = fetcher.pages_fetched
    finally:
        if owned_client is not None:
            await owned_client.close()

    ranked = rank([score_candidate(c, v) for c, v in zip(candidates, checks)])
    return _report(query, profile, ranked, findings, notes, usage, pages_fetched, started)


async def _resolve(
    query: CompanyQuery, settings: Settings, client: Any, usage: Usage
) -> CompanyProfile:
    if query.domain:  # the user already told us; skip the paid search
        try:
            domain = normalize_domain(query.domain)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        return CompanyProfile(canonical_name=query.name, domains=[domain], hq_country=query.country, confidence=1.0)
    if client is None:
        raise ConfigError("Give the company's domain (--domain) when running without Claude.")
    return await resolve_company(client, query, settings, usage)


async def _search_website(
    crawler: SiteCrawler,
    profile: CompanyProfile,
    settings: Settings,
    client: Any,
    usage: Usage,
    notes: list[str],
) -> WebsiteFindings:
    if client is not None:
        try:
            return await search_website_with_agent(client, crawler, profile, settings, usage)
        except (AgentError, anthropic.APIError) as exc:
            reason = getattr(exc, "message", None) or str(exc)
            log.warning("Website Agent failed: %s", reason)
            notes.append(f"The Website Agent stopped early ({reason}); the rule-based crawler finished the search.")
    return await search_website_rule_based(crawler, profile, settings)


def build_candidates(
    crawler: SiteCrawler, findings: WebsiteFindings, allow_named_contacts: bool, notes: list[str]
) -> list[EmailCandidate]:
    """One candidate per email seen on fetched pages, with its best source first."""
    sightings: dict[str, list[tuple[PageRecord, FoundEmail]]] = {}
    for record in crawler.pages.values():
        for found in record.emails:
            sightings.setdefault(found.email, []).append((record, found))

    candidates: list[EmailCandidate] = []
    hidden: list[str] = []
    for email, seen in sightings.items():
        local_class = classify_local_part(email.split("@")[0])
        hint = findings.hints.get(email)
        if local_class == "other_department" and hint not in _HR_HINTS:
            continue  # sales@, press@, privacy@ ... aren't HR contacts
        kind = _KIND_BY_CLASS[local_class]
        if kind == "named_person" and not allow_named_contacts:
            hidden.append(email)
            continue
        seen.sort(key=lambda s: (PAGE_PRIORITY[s[0].page_type], s[1].hr_context), reverse=True)
        record, found = seen[0]
        candidates.append(
            EmailCandidate(
                email=email,
                kind=kind,
                department_hint=hint or _department_hint(local_class, found.hr_context),
                source_kind=record.source_kind,
                source_url=record.url,
                source_page_type=record.page_type,
                snippet=found.snippet,
                hr_context=found.hr_context,
                other_source_urls=[r.url for r, _ in seen[1:]],
                found_by=findings.found_by,
            )
        )
    if hidden:
        notes.append(
            f"{len(hidden)} personal email address(es) were found but not shown; "
            "re-run with --allow-named-contacts to include them."
        )
    return candidates


def _department_hint(local_class: str, hr_context: bool) -> DepartmentHint | None:
    if local_class == "hr_role" or hr_context:
        return "HR"
    if local_class == "general":
        return "General"
    return None


def _report(
    query: CompanyQuery,
    profile: CompanyProfile,
    ranked: list[RankedCandidate],
    findings: WebsiteFindings,
    notes: list[str],
    usage: Usage,
    pages_fetched: int,
    started: float,
) -> FinalReport:
    accepted = [r for r in ranked if not r.rejected]
    best = accepted[0] if accepted and accepted[0].score >= BEST_MIN_SCORE else None
    alternatives = (accepted[1:] if best else accepted)[:MAX_ALTERNATIVES]
    rejected = [f"Rejected {r.candidate.email}: {r.reasons[0]}." for r in ranked if r.rejected]
    careers_url = findings.careers_url or profile.careers_url
    return FinalReport(
        query=query,
        company=profile,
        status="found" if best else "not_found",
        best_email=best,
        alternatives=alternatives,
        careers_portal_url=careers_url,
        contact_form_url=findings.contact_form_url,
        explanation=_explain(best, alternatives, careers_url, findings.contact_form_url),
        notes=notes + findings.notes + rejected,
        pages_fetched=pages_fetched,
        web_searches=usage.web_searches,
        cost_usd=round(usage.cost_usd, 4),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _explain(
    best: RankedCandidate | None,
    alternatives: list[RankedCandidate],
    careers_url: str | None,
    contact_form_url: str | None,
) -> str:
    if best:
        c = best.candidate
        text = f"Best match: {c.email} (score {best.score:.2f}), found on the {c.source_page_type} page {c.source_url}."
        if alternatives:
            text += f" {len(alternatives)} alternative(s) listed."
        return text
    parts = ["No public HR email found on the company's website."]
    if alternatives:
        parts.append("General contact address(es): " + ", ".join(r.candidate.email for r in alternatives) + ".")
    if careers_url:
        parts.append(f"Apply through the careers page: {careers_url}.")
    if contact_form_url:
        parts.append(f"Or use the contact form: {contact_form_url}.")
    return " ".join(parts)


def _ambiguous_report(
    query: CompanyQuery, profile: CompanyProfile, usage: Usage, started: float
) -> FinalReport:
    options = ", ".join([profile.canonical_name, *profile.alternatives])
    return FinalReport(
        query=query,
        company=profile,
        status="ambiguous_company",
        explanation=f"'{query.name}' could mean several companies: {options}. Re-run with --domain or --country to pick one.",
        web_searches=usage.web_searches,
        cost_usd=round(usage.cost_usd, 4),
        duration_ms=int((time.monotonic() - started) * 1000),
    )

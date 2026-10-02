"""Weighted, explainable candidate score (DESIGN.md §3.7)."""

from __future__ import annotations

from .domains import host_of
from .patterns import has_hr_context
from .schemas import EmailCandidate, RankedCandidate, VerificationResult

CAREERS_PAGE = 0.45
OTHER_COMPANY_PAGE = 0.30
ATS_POSTING = 0.35
THIRD_PARTY_PAGE = 0.15
HR_CONTEXT = 0.15
ROLE_BASED = 0.10
MAILBOX_POINTS = {"valid": 0.15, "catch_all": 0.05}
MULTI_SITE = 0.10
MULTI_PAGE = 0.05
NOT_COMPANY_DOMAIN = -0.50
FREE_OR_DISPOSABLE = -0.30
INFERRED_CAP = 0.50

BEST_MIN_SCORE = 0.40  # below this, a candidate is only an alternative


def score_candidate(c: EmailCandidate, v: VerificationResult) -> RankedCandidate:
    rejection = _rejection(c, v)
    if rejection:
        return RankedCandidate(candidate=c, verification=v, score=0.0, rejected=True, reasons=[rejection])

    score = 0.0
    reasons: list[str] = []

    def apply(points: float, why: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"{why} ({points:+.2f})")

    if c.kind == "inferred":
        reasons.append("guessed from common patterns, never seen on a page")
    elif c.source_kind in ("company_site", "parent_site"):
        if c.source_page_type == "careers":
            apply(CAREERS_PAGE, "found on the company's careers page")
        else:
            apply(OTHER_COMPANY_PAGE, f"found on the company's {c.source_page_type or 'website'} page")
    elif c.source_kind == "ats":
        apply(ATS_POSTING, "found in an official job posting")
    else:
        apply(THIRD_PARTY_PAGE, "found on a third-party page")

    hr_context = c.hr_context if c.hr_context is not None else has_hr_context(c.snippet)
    if hr_context:
        apply(HR_CONTEXT, "HR or hiring words next to it")
    if c.kind == "role_based":
        apply(ROLE_BASED, "role-based HR address")

    if v.mailbox in MAILBOX_POINTS:
        label = "mail server confirmed the mailbox" if v.mailbox == "valid" else "mail server accepts any address"
        apply(MAILBOX_POINTS[v.mailbox], label)
    else:
        reasons.append("mailbox not checked")

    sources = [u for u in [c.source_url, *c.other_source_urls] if u]
    sites = {host_of(u) for u in sources}
    if len(sites) >= 2:
        apply(MULTI_SITE, f"seen on {len(sites)} independent sites")
    elif len(set(sources)) >= 2:
        apply(MULTI_PAGE, f"seen on {len(set(sources))} pages")

    if not v.domain_owned:
        apply(NOT_COMPANY_DOMAIN, "not on a company domain")
    if v.disposable_or_free:
        apply(FREE_OR_DISPOSABLE, "free or disposable mail provider")

    score = max(0.0, min(1.0, score))
    if c.kind == "inferred":
        score = min(score, INFERRED_CAP)
    return RankedCandidate(candidate=c, verification=v, score=round(score, 2), reasons=reasons)


def rank(candidates: list[RankedCandidate]) -> list[RankedCandidate]:
    return sorted(
        candidates,
        key=lambda r: (not r.rejected, r.score, r.candidate.kind == "role_based"),
        reverse=True,
    )


def _rejection(c: EmailCandidate, v: VerificationResult) -> str | None:
    if not v.syntax_ok:
        return "invalid email syntax"
    if v.mx_ok is False:
        return "domain has no mail server (no MX or A record)"
    if v.mailbox == "invalid":
        return "mail server rejected the mailbox"
    if c.kind != "inferred" and v.evidence_confirmed is False:
        return "email not present on its source page"
    return None

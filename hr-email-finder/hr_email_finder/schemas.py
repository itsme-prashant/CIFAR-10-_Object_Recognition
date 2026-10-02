"""Data contracts shared by the agents and the pipeline (DESIGN.md §4)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

CandidateKind = Literal["role_based", "named_person", "generic", "inferred"]
DepartmentHint = Literal["HR", "Recruiting", "Campus", "General", "Other"]
PageType = Literal["careers", "contact", "legal", "privacy", "about", "home", "other"]
SourceKind = Literal[
    "company_site",
    "parent_site",
    "ats",
    "job_board",
    "third_party_page",
    "enrichment_api",
    "browser_render",
    "inferred",
]
MailboxStatus = Literal["valid", "invalid", "catch_all", "unknown"]
ReportStatus = Literal["found", "inferred_only", "not_found", "ambiguous_company"]


class CompanyQuery(BaseModel):
    name: str
    domain: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    allow_named_contacts: bool = False  # DESIGN.md §8


class CompanyProfile(BaseModel):
    canonical_name: str
    domains: list[str]  # primary first
    hq_country: str | None = None
    industry: str | None = None
    size_band: str | None = None
    careers_url: str | None = None
    ats_provider: str | None = None
    alternatives: list[str] = Field(default_factory=list)  # other plausible matches
    confidence: float = 0.5


class EmailCandidate(BaseModel):
    email: str
    kind: CandidateKind
    department_hint: DepartmentHint | None = None
    source_kind: SourceKind = "company_site"
    source_url: str | None  # None only for inferred
    source_page_type: PageType | None = None
    snippet: str | None  # text around the email on the source page
    hr_context: bool | None = None  # HR words in the prose around it (None = judge from snippet)
    other_source_urls: list[str] = Field(default_factory=list)
    found_by: str
    found_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VerificationResult(BaseModel):
    syntax_ok: bool
    domain_owned: bool
    mx_ok: bool | None  # None = DNS lookup failed
    mailbox: MailboxStatus
    disposable_or_free: bool
    evidence_confirmed: bool | None


class RankedCandidate(BaseModel):
    candidate: EmailCandidate
    verification: VerificationResult
    score: float
    rejected: bool = False
    reasons: list[str]


class FinalReport(BaseModel):
    query: CompanyQuery
    company: CompanyProfile | None
    status: ReportStatus
    best_email: RankedCandidate | None = None
    alternatives: list[RankedCandidate] = Field(default_factory=list)
    careers_portal_url: str | None = None
    contact_form_url: str | None = None
    explanation: str
    notes: list[str] = Field(default_factory=list)
    pages_fetched: int = 0
    web_searches: int = 0
    cost_usd: float = 0.0  # Claude tokens at list price; excludes web search fees
    duration_ms: int = 0

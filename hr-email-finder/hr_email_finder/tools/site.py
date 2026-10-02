"""Crawl a company's own website: find likely HR pages, fetch them, collect email evidence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..config import Settings
from ..domains import host_in, host_of
from ..patterns import EMAIL_RE, HR_CONTEXT_RE
from ..schemas import PageType, SourceKind
from .extract import FoundEmail, extract_emails
from .http import Fetcher, FetchError
from .html import parse_html, parse_text

log = logging.getLogger(__name__)

# Checked in order; first match wins.
_PAGE_KEYWORDS: list[tuple[PageType, tuple[str, ...]]] = [
    ("careers", (
        "career", "jobs", "job-", "/job", "join-us", "joinus", "join-our-team", "work-with-us",
        "workwithus", "vacanc", "recruit", "talent", "hiring", "karriere", "stellen", "emploi",
        "carriere", "carrera", "empleo", "lavora", "vacature", "human-resources", "openings",
    )),
    ("contact", ("contact", "kontakt", "contacto", "contatti", "get-in-touch", "reach-us")),
    ("legal", ("impressum", "imprint", "legal-notice", "mentions-legales", "aviso-legal", "legal")),
    ("privacy", ("privacy", "datenschutz", "data-protection", "gdpr")),
    ("about", ("about", "who-we-are", "ueber-uns", "uber-uns", "our-company")),
]
_HR_TOKEN_RE = re.compile(r"(?:^|[^a-z])hr(?:[^a-z]|$)")
PAGE_PRIORITY: dict[PageType, float] = {
    "careers": 5, "contact": 4, "legal": 3, "privacy": 2, "about": 1, "home": 0.5, "other": 0,
}
COMMON_PATHS = ["/careers", "/jobs", "/contact", "/contact-us", "/impressum", "/privacy-policy", "/about"]
ATS_HOST_SUFFIXES = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "smartrecruiters.com", "darwinbox.in",
    "zohorecruit.com", "zohorecruit.in", "recruitee.com", "workable.com", "ashbyhq.com",
    "bamboohr.com", "teamtailor.com", "personio.de", "personio.com", "keka.com",
    "freshteam.com", "jobvite.com", "icims.com", "taleo.net",
)
_SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".zip", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3", ".css", ".js",
)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_MAX_SITEMAP_LINKS = 2000


def classify_page(url: str, label: str = "") -> PageType:
    path = urlsplit(url).path.lower()
    if path in ("", "/") and not label:
        return "home"
    haystack = f"{path} {label.lower().replace(' ', '-')}"
    if _HR_TOKEN_RE.search(haystack):  # "/about/hr", "HR" link text
        return "careers"
    for page_type, keywords in _PAGE_KEYWORDS:
        if any(k in haystack for k in keywords):
            return page_type
    return "home" if path in ("", "/") else "other"


def is_ats_host(host: str) -> bool:
    return host_in(host, list(ATS_HOST_SUFFIXES))


@dataclass(frozen=True)
class RankedLink:
    url: str
    text: str
    page_type: PageType
    score: float
    guessed: bool = False  # a common path we haven't seen linked


@dataclass
class PageRecord:
    url: str
    page_type: PageType
    source_kind: SourceKind
    title: str
    has_contact_form: bool
    emails: list[FoundEmail]
    passages: str  # text near HR words and emails, for the Website Agent
    links: list[RankedLink]  # ranked, crawlable


class SiteCrawler:
    """Fetches pages on the company's domains (and known ATS hosts) and records email evidence."""

    def __init__(self, fetcher: Fetcher, domains: list[str], settings: Settings) -> None:
        self.fetcher = fetcher
        self.domains = list(domains)  # grows if the homepage redirects to another domain
        self.pages: dict[str, PageRecord] = {}  # keyed by final URL
        self._aliases: dict[str, str] = {}  # requested URL → final URL
        self._settings = settings

    @property
    def pages_left(self) -> int:
        return self.fetcher.pages_left

    def source_kind_for(self, url: str) -> SourceKind | None:
        """company_site / ats for crawlable URLs, None for anything else."""
        host = host_of(url)
        if host_in(host, self.domains):
            return "company_site"
        if is_ats_host(host):
            return "ats"
        return None

    def page(self, url: str) -> PageRecord | None:
        return self.pages.get(self._aliases.get(url, url))

    def emails_on(self, url: str) -> set[str]:
        record = self.page(url)
        return {e.email for e in record.emails} if record else set()

    def all_emails(self) -> set[str]:
        return {e.email for record in self.pages.values() for e in record.emails}

    async def fetch(self, url: str) -> PageRecord:
        requested_kind = self.source_kind_for(url)
        if requested_kind is None:
            raise FetchError(
                f"{host_of(url) or url} is not one of the company's domains ({', '.join(self.domains)})"
            )
        if (record := self.page(url)) is not None:
            return record
        fetched = await self.fetcher.get(url)
        parsed = (parse_html if fetched.is_html else parse_text)(fetched.text, fetched.final_url)
        kind = self.source_kind_for(fetched.final_url)
        if kind is None and requested_kind == "company_site" and urlsplit(url).path in ("", "/"):
            # The company's own homepage redirects elsewhere (rebrand, group site): adopt that domain.
            new_domain = host_of(fetched.final_url).removeprefix("www.")
            self.domains.append(new_domain)
            kind = "company_site"
            log.info("%s redirects to %s; added it as a company domain", url, new_domain)
        kind = kind or "third_party_page"
        page_type = "careers" if kind == "ats" else classify_page(fetched.final_url, parsed.title)
        links = [
            ranked
            for link in parsed.links
            if (ranked := self.rank_link(link.url, link.text)) is not None and link.url != fetched.final_url
        ]
        record = PageRecord(
            url=fetched.final_url,
            page_type=page_type,
            source_kind=kind,
            title=parsed.title,
            has_contact_form=parsed.has_contact_form,
            emails=extract_emails(parsed),
            passages=_passages(parsed.text, self._settings.passage_chars),
            links=sorted(links, key=lambda l: -l.score),
        )
        self.pages[record.url] = record
        self._aliases[url] = record.url
        return record

    async def discover(self, domain: str) -> list[RankedLink]:
        """Ranked candidate pages from the homepage, the sitemap and, if needed, common paths."""
        home: PageRecord | None = None
        error: FetchError | None = None
        for scheme in ("https", "http"):
            try:
                home = await self.fetch(f"{scheme}://{domain}/")
                break
            except FetchError as exc:
                error = error or exc  # report the https failure, not the fallback's
        if home is None:
            raise error or FetchError(f"could not fetch the homepage of {domain}")

        links ={link.url: link for link in home.links}
        if _relevant_count(links.values()) < 2:
            for link in await self._sitemap_links(home.url):
                links.setdefault(link.url, link)
        if _relevant_count(links.values()) < 2:
            origin = "{0.scheme}://{0.netloc}".format(urlsplit(home.url))
            for path in COMMON_PATHS:
                page_type = classify_page(path)
                links.setdefault(
                    origin + path,
                    RankedLink(origin + path, "", page_type, PAGE_PRIORITY[page_type] - 1, guessed=True),
                )
        fresh = [link for link in links.values() if self.page(link.url) is None]
        return sorted(fresh, key=lambda l: -l.score)

    def rank_link(self, url: str, text: str = "") -> RankedLink | None:
        kind = self.source_kind_for(url)
        path = urlsplit(url).path.lower()
        if kind is None or path.endswith(_SKIP_EXTENSIONS):
            return None
        page_type: PageType = "careers" if kind == "ats" else classify_page(url, text)
        score = PAGE_PRIORITY[page_type]
        if HR_CONTEXT_RE.search(text):
            score += 0.5
        depth = len([seg for seg in path.split("/") if seg])
        score -= 0.3 * max(0, depth - 2)
        if re.search(r"\d{5,}", path):  # individual postings / articles
            score -= 1
        return RankedLink(url, text, page_type, round(score, 2))

    async def _sitemap_links(self, home_url: str) -> list[RankedLink]:
        sitemap_urls = await self.fetcher.sitemaps(home_url)
        if not sitemap_urls:
            sitemap_urls = ["{0.scheme}://{0.netloc}/sitemap.xml".format(urlsplit(home_url))]
        locs: list[str] = []
        try:
            body = (await self.fetcher.get(sitemap_urls[0])).text
            locs = _LOC_RE.findall(body)
            # A sitemap index lists more sitemaps; read the first child that looks like pages.
            children = [u for u in locs if u.lower().endswith(".xml")]
            if children and len(children) == len(locs):
                child = next((u for u in children if "page" in u.lower()), children[0])
                locs = _LOC_RE.findall((await self.fetcher.get(child)).text)
        except FetchError as exc:
            log.info("no usable sitemap: %s", exc)
        ranked = (self.rank_link(u) for u in locs[:_MAX_SITEMAP_LINKS])
        return [r for r in ranked if r is not None and r.page_type not in ("other", "home")]


def _relevant_count(links) -> int:
    return sum(1 for l in links if l.page_type in ("careers", "contact", "legal"))


def _passages(text: str, limit: int) -> str:
    """Text windows around HR words and emails, merged; the page start if there are none."""
    spans = [m.span() for m in HR_CONTEXT_RE.finditer(text)] + [m.span() for m in EMAIL_RE.finditer(text)]
    if not spans:
        return text[: limit // 3]
    windows: list[list[int]] = []
    for start, end in sorted(spans):
        start, end = max(0, start - 250), min(len(text), end + 250)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    return " … ".join(text[a:b] for a, b in windows)[:limit]

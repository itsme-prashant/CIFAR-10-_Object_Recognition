"""HTML → visible text, links and page signals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

_SKIP_HREF_PREFIXES = ("mailto:", "tel:", "javascript:", "data:", "sms:", "whatsapp:")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Link:
    url: str
    text: str


@dataclass
class ParsedPage:
    url: str
    title: str
    text: str  # visible text, whitespace collapsed
    links: list[Link]
    has_contact_form: bool
    json_ld: list[str]  # raw application/ld+json blocks (often carry an "email" field)
    soup: BeautifulSoup  # kept for mailto / Cloudflare extraction


def collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def parse_html(html: str, url: str) -> ParsedPage:
    soup = BeautifulSoup(html, "html.parser")
    json_ld = [s.get_text() for s in soup.find_all("script", attrs={"type": "application/ld+json"})]
    title = collapse_ws(soup.title.get_text(" ")) if soup.title else ""
    # A form with a free-text field is a contact form; newsletter sign-ups have no textarea.
    has_contact_form = any(form.find("textarea") for form in soup.find_all("form"))
    links = _links(soup, url)
    for tag in soup(["script", "style", "noscript", "template", "svg", "head"]):
        tag.decompose()
    text = collapse_ws(soup.get_text(" "))
    return ParsedPage(url, title, text, links, has_contact_form, json_ld, soup)


def parse_text(body: str, url: str) -> ParsedPage:
    return ParsedPage(url, "", collapse_ws(body), [], False, [], BeautifulSoup("", "html.parser"))


def _links(soup: BeautifulSoup, base_url: str) -> list[Link]:
    seen: dict[str, Link] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith(_SKIP_HREF_PREFIXES):
            continue
        url, _ = urldefrag(urljoin(base_url, href))
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        text = collapse_ws(a.get_text(" ")) or a.get("title", "") or a.get("aria-label", "")
        if url not in seen or (text and not seen[url].text):
            seen[url] = Link(url, text[:120])
    return list(seen.values())

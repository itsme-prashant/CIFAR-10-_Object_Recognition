"""Find email addresses on a parsed page, including common obfuscations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from bs4 import Comment, NavigableString, Tag

from ..patterns import EMAIL_RE, has_hr_context
from .html import ParsedPage, collapse_ws

SNIPPET_RADIUS = 200
_MIN_CONTEXT_CHARS = 40

# "hr [at] acme [dot] com", "hr(at)acme(dot)com", "hr {@} acme {.} com"
_OBF_AT_RE = re.compile(r"\s*[\[({]\s*(?:at|@)\s*[\])}]\s*", re.IGNORECASE)
_OBF_DOT_RE = re.compile(r"\s*[\[({]\s*(?:dot|\.)\s*[\])}]\s*", re.IGNORECASE)
_CF_PLACEHOLDER_RE = re.compile(r"\[email\s*protected\]", re.IGNORECASE)
_CF_HREF_MARKER = "/cdn-cgi/l/email-protection#"

_FILE_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "tif", "tiff",
    "css", "js", "mp4", "webm", "woff", "woff2", "ttf", "pdf",
}
_PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "yourcompany.com", "mysite.com", "sentry.io", "wixpress.com",
}
_PLACEHOLDER_SUFFIXES = (".example.com", ".sentry.io", ".wixpress.com")
_HEX_KEY_RE = re.compile(r"[0-9a-f]{16,}")  # tracking / error-reporting keys, not mailboxes
_BLOCK_TAGS = {
    "p", "li", "td", "th", "dd", "dt", "address", "section", "article", "footer",
    "header", "div", "form", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
}
_TOP_TAGS = {"body", "html", "main", "[document]"}


@dataclass(frozen=True)
class FoundEmail:
    email: str
    snippet: str  # surrounding text, as shown on the page
    method: str  # text | mailto | cloudflare | json_ld | deobfuscated
    hr_context: bool  # HR words in the surrounding prose (menu link labels don't count)


def normalize_email(raw: str) -> str:
    return raw.strip().strip(".,;:<>()[]\"'").lower()


def is_plausible_email(email: str) -> bool:
    local, _, domain = email.rpartition("@")
    if not local or not domain:
        return False
    if domain.rsplit(".", 1)[-1] in _FILE_TLDS:  # logo@2x.png
        return False
    if domain in _PLACEHOLDER_DOMAINS or domain.endswith(_PLACEHOLDER_SUFFIXES):
        return False
    return not _HEX_KEY_RE.fullmatch(local)


def decode_cfemail(hex_string: str) -> str | None:
    """Decode Cloudflare's email-protection encoding (first byte is the XOR key)."""
    try:
        data = bytes.fromhex(hex_string)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    return bytes(b ^ data[0] for b in data[1:]).decode("utf-8", "replace")


def extract_emails(page: ParsedPage) -> list[FoundEmail]:
    """All plausible emails on the page, de-duplicated, each with surrounding text."""
    found: dict[str, FoundEmail] = {}

    def add(raw: str, method: str, element: Tag | None = None, text: str = "", start: int = 0) -> None:
        email = normalize_email(raw)
        if email in found or not EMAIL_RE.fullmatch(email) or not is_plausible_email(email):
            return
        if element is not None:
            block = _context_block(element)
            snippet = _centred(_CF_PLACEHOLDER_RE.sub(email, collapse_ws(block.get_text(" "))), email)
            hr = has_hr_context(_prose(block))
        else:
            snippet = text[max(0, start - SNIPPET_RADIUS) : start + len(email) + SNIPPET_RADIUS]
            hr = has_hr_context(snippet)
        found[email] = FoundEmail(email, collapse_ws(snippet), method, hr)

    for m in EMAIL_RE.finditer(page.text):
        email = m.group().lower()
        node = page.soup.find(string=lambda s: email in s.lower())
        if node is not None and node.parent is not None:
            add(m.group(), "text", element=node.parent)
        else:  # split across tags, e.g. hr@<b>acme</b>.com
            add(m.group(), "text", text=page.text, start=m.start())

    for a in page.soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            for raw in unquote(href[7:].split("?", 1)[0]).split(","):
                add(raw, "mailto", element=a)
        elif _CF_HREF_MARKER in href:
            if decoded := decode_cfemail(href.split(_CF_HREF_MARKER, 1)[1]):
                add(decoded, "cloudflare", element=a)

    for el in page.soup.select("[data-cfemail]"):
        if decoded := decode_cfemail(el.get("data-cfemail", "")):
            add(decoded, "cloudflare", element=el)

    for block in page.json_ld:
        for m in EMAIL_RE.finditer(block):
            add(m.group(), "json_ld", text="Structured data: " + block, start=m.start() + 17)

    deobfuscated = _OBF_DOT_RE.sub(".", _OBF_AT_RE.sub("@", page.text))
    if deobfuscated != page.text:
        for m in EMAIL_RE.finditer(deobfuscated):
            add(m.group(), "deobfuscated", text=deobfuscated, start=m.start())

    return list(found.values())


def _context_block(el: Tag) -> Tag:
    """Nearest block with enough prose around the element, never the whole page body."""
    node = el
    for _ in range(6):
        parent = node.parent
        if parent is None or parent.name in _TOP_TAGS:
            break
        node = parent
        if node.name in _BLOCK_TAGS and len(_prose(node)) >= _MIN_CONTEXT_CHARS:
            break
    return node


def _prose(node: Tag) -> str:
    """Text of a block without the labels of ordinary links (menus say 'Careers' everywhere)."""
    parts = []
    for s in node.find_all(string=True):
        if isinstance(s, Comment) or not isinstance(s, NavigableString):
            continue
        link = s.find_parent("a")
        if link is not None and not _is_email_link(link):
            continue
        parts.append(str(s))
    return collapse_ws(" ".join(parts))


def _is_email_link(a: Tag) -> bool:
    href = a.get("href", "").lower()
    return href.startswith("mailto:") or _CF_HREF_MARKER in href


def _centred(text: str, email: str) -> str:
    i = text.lower().find(email.lower())
    if i < 0:
        return text[: 2 * SNIPPET_RADIUS]
    return text[max(0, i - SNIPPET_RADIUS) : i + len(email) + SNIPPET_RADIUS]

"""Plain-Python tools that the agents call. No LLM in here."""
from __future__ import annotations

import ipaddress
import re
import smtplib
import socket
from urllib.parse import urlparse

import dns.exception
import dns.resolver
import httpx

USER_AGENT = "hr-email-finder/0.1 (research; respects robots-style public pages only)"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[\(\{]\s*at\s*[\]\)\}]\s*([A-Za-z0-9.\-]+)\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*([A-Za-z]{2,})",
    re.I,
)
ROLE_LOCAL_PARTS = ["careers", "jobs", "recruiting", "recruitment", "talent", "hr", "people", "humanresources"]
MAX_PAGE_CHARS = 15_000


def extract_emails(text: str) -> list[str]:
    found = {m.lower() for m in EMAIL_RE.findall(text)}
    for local, host, tld in OBFUSCATED_RE.findall(text):
        found.add(f"{local}@{host}.{tld}".lower())
    # drop asset filenames such as logo@2x.png
    return sorted(e for e in found if not e.rsplit(".", 1)[-1] in {"png", "jpg", "jpeg", "gif", "svg", "webp"})


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def fetch_page(url: str) -> dict:
    """Fetch a public web page; return text excerpt, emails and mailto links."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"error": "only http(s) URLs are allowed"}
    if not _is_public_host(parsed.hostname):
        return {"error": "host is not a public address"}
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True)
    except httpx.HTTPError as e:
        return {"error": f"fetch failed: {e}"}
    html = r.text
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "status": r.status_code,
        "final_url": str(r.url),
        "emails_found": extract_emails(html),
        "text": text[:MAX_PAGE_CHARS],
    }


def mx_lookup(domain: str) -> dict:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        hosts = sorted((r.preference, str(r.exchange).rstrip(".")) for r in answers)
        return {"domain": domain, "accepts_mail": True, "mx": [h for _, h in hosts]}
    except dns.exception.DNSException as e:
        return {"domain": domain, "accepts_mail": False, "error": type(e).__name__}


def generate_candidates(first: str, last: str, domain: str) -> list[str]:
    f, l = re.sub(r"\W", "", first.lower()), re.sub(r"\W", "", last.lower())
    if not f or not l:
        return []
    locals_ = [f"{f}.{l}", f"{f}{l}", f"{f[0]}{l}", f"{f[0]}.{l}", f, f"{f}_{l}", f"{l}.{f}", f"{f}{l[0]}", l]
    return [f"{p}@{domain}" for p in locals_]


def apply_pattern(pattern: str, first: str, last: str, domain: str) -> str | None:
    """pattern uses {first},{last},{f},{l} placeholders, e.g. '{f}{last}'."""
    f, l = re.sub(r"\W", "", first.lower()), re.sub(r"\W", "", last.lower())
    if not f or not l:
        return None
    try:
        local = pattern.format(first=f, last=l, f=f[0], l=l[0])
    except (KeyError, IndexError):
        return None
    return f"{local}@{domain}"


def smtp_probe(email: str, helo: str = "example.com", mail_from: str = "probe@example.com") -> dict:
    """OPT-IN RCPT TO check. Many servers are catch-all or block this; treat as weak evidence.

    Never sends a message. Port 25 is often blocked on cloud hosts.
    """
    domain = email.rsplit("@", 1)[1]
    mx = mx_lookup(domain)
    if not mx.get("accepts_mail"):
        return {"email": email, "result": "no_mx"}
    try:
        with smtplib.SMTP(mx["mx"][0], 25, timeout=10) as s:
            s.helo(helo)
            s.mail(mail_from)
            code, _ = s.rcpt(email)
            catch_all, _ = s.rcpt(f"zz-nonexistent-{abs(hash(email)) % 10**8}@{domain}")
        if code == 250 and catch_all == 250:
            return {"email": email, "result": "catch_all"}
        return {"email": email, "result": "accepted" if code == 250 else "rejected", "code": code}
    except (smtplib.SMTPException, OSError) as e:
        return {"email": email, "result": "unknown", "error": type(e).__name__}

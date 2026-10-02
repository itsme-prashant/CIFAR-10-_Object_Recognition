"""Deterministic checks on a candidate email (DESIGN.md §3.6)."""

from __future__ import annotations

import asyncio
import re
import secrets
import smtplib
import socket
from typing import Awaitable, Callable, Protocol

from ..domains import LABEL_RE, host_in
from ..schemas import EmailCandidate, MailboxStatus, VerificationResult
from .mx import lookup_mx

FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "ymail.com",
    "rocketmail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com",
    "mac.com", "aol.com", "proton.me", "protonmail.com", "pm.me", "zoho.com", "zohomail.com",
    "zohomail.in", "yandex.com", "yandex.ru", "mail.com", "email.com", "gmx.com", "gmx.de",
    "gmx.net", "web.de", "rediffmail.com", "qq.com", "163.com", "126.com", "naver.com",
}
DISPOSABLE_PROVIDERS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
    "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc",
}
_LOCAL_RE = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*", re.IGNORECASE)


def syntax_ok(email: str) -> bool:
    if len(email) > 254 or email.count("@") != 1:
        return False
    local, domain = email.split("@")
    if not local or len(local) > 64 or not _LOCAL_RE.fullmatch(local):
        return False
    try:
        labels = domain.encode("idna").decode("ascii").lower().split(".")
    except UnicodeError:
        return False
    return len(labels) >= 2 and not labels[-1].isdigit() and all(LABEL_RE.fullmatch(l) for l in labels)


class MailboxVerifier(Protocol):
    async def check(self, email: str, mx_hosts: list[str]) -> MailboxStatus: ...


class NoMailboxCheck:
    """Default: don't contact mail servers."""

    async def check(self, email: str, mx_hosts: list[str]) -> MailboxStatus:
        return "unknown"


class SmtpProbe:
    """RCPT TO probe against the best MX, with catch-all detection.

    Opt-in only: probing from your own IP gets it blocklisted quickly, many networks block
    outbound port 25, and catch-all servers make results weak. Prefer a verification provider
    in production (implement MailboxVerifier).
    """

    def __init__(self, timeout_s: float = 10.0, helo_host: str | None = None) -> None:
        self._timeout_s = timeout_s
        self._helo_host = helo_host or socket.getfqdn()

    async def check(self, email: str, mx_hosts: list[str]) -> MailboxStatus:
        if not mx_hosts:
            return "unknown"
        return await asyncio.to_thread(self._probe, email, mx_hosts[0])

    def _probe(self, email: str, mx_host: str) -> MailboxStatus:
        domain = email.rsplit("@", 1)[1]
        try:
            with smtplib.SMTP(mx_host, 25, timeout=self._timeout_s, local_hostname=self._helo_host) as smtp:
                smtp.ehlo_or_helo_if_needed()
                code, _ = smtp.mail("")  # null reverse-path
                if code >= 400:
                    return "unknown"
                code, _ = smtp.rcpt(email)
                if code in (550, 551, 553):
                    return "invalid"
                if code not in (250, 251):
                    return "unknown"
                code, _ = smtp.rcpt(f"no-such-user-{secrets.token_hex(6)}@{domain}")
                return "catch_all" if code in (250, 251) else "valid"
        except (OSError, smtplib.SMTPException):
            return "unknown"


class Verifier:
    def __init__(
        self,
        company_domains: list[str],
        mailbox: MailboxVerifier | None = None,
        mx_lookup: Callable[[str], Awaitable[list[str] | None]] = lookup_mx,
        evidence: Callable[[str], set[str]] | None = None,
    ) -> None:
        self._domains = company_domains
        self._mailbox = mailbox or NoMailboxCheck()
        self._mx_lookup = mx_lookup
        self._evidence = evidence  # source URL → emails actually present on that page
        self._mx: dict[str, asyncio.Future] = {}

    async def verify(self, candidate: EmailCandidate) -> VerificationResult:
        email = candidate.email
        domain = email.rpartition("@")[2]
        owned = host_in(domain, self._domains)
        free = not owned and (domain in FREE_PROVIDERS or domain in DISPOSABLE_PROVIDERS)
        evidence = None
        if self._evidence is not None and candidate.source_url is not None:
            evidence = email in self._evidence(candidate.source_url)
        if not syntax_ok(email):
            return VerificationResult(
                syntax_ok=False, domain_owned=owned, mx_ok=None, mailbox="unknown",
                disposable_or_free=free, evidence_confirmed=evidence,
            )
        if domain not in self._mx:  # share one lookup per domain across concurrent checks
            self._mx[domain] = asyncio.ensure_future(self._mx_lookup(domain))
        mx_hosts = await self._mx[domain]
        mailbox: MailboxStatus = "unknown"
        if mx_hosts and not free:
            mailbox = await self._mailbox.check(email, mx_hosts)
        return VerificationResult(
            syntax_ok=True,
            domain_owned=owned,
            mx_ok=None if mx_hosts is None else bool(mx_hosts),
            mailbox=mailbox,
            disposable_or_free=free,
            evidence_confirmed=evidence,
        )

import asyncio

import pytest

from hr_email_finder.domains import normalize_domain
from hr_email_finder.schemas import EmailCandidate
from hr_email_finder.tools.verify import Verifier, syntax_ok


def cand(email: str, url: str | None = "https://acme.test/careers") -> EmailCandidate:
    return EmailCandidate(email=email, kind="role_based", source_url=url, snippet=None, found_by="test")


@pytest.mark.parametrize(
    "email, ok",
    [
        ("hr@acme.com", True),
        ("first.last+jobs@sub.acme.co.in", True),
        ("hr@acme", False),
        ("hr..x@acme.com", False),
        (".hr@acme.com", False),
        ("hr@-acme.com", False),
        ("hr@acme.123", False),
        ("a" * 65 + "@acme.com", False),
    ],
)
def test_syntax(email, ok):
    assert syntax_ok(email) is ok


@pytest.mark.parametrize(
    "raw, expected",
    [("https://www.Acme.com/careers", "acme.com"), ("acme.co.in", "acme.co.in"), ("www.acme.test.", "acme.test")],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", ["acme", "http://", "acme..com", "-acme.com"])
def test_normalize_domain_rejects(raw):
    with pytest.raises(ValueError):
        normalize_domain(raw)


def test_verifier_checks_and_shares_mx_lookups():
    lookups: list[str] = []

    async def mx(domain: str):
        lookups.append(domain)
        await asyncio.sleep(0)
        return {"acme.test": ["mx.acme.test"], "gmail.com": ["gmail-smtp-in.l.google.com"]}.get(domain, [])

    evidence = {"https://acme.test/careers": {"hr@acme.test", "jobs@acme.test", "acme.jobs@gmail.com"}}
    verifier = Verifier(["acme.test"], mx_lookup=mx, evidence=lambda url: evidence.get(url, set()))

    async def run():
        return await asyncio.gather(*(verifier.verify(cand(e)) for e in [
            "hr@acme.test", "jobs@acme.test", "acme.jobs@gmail.com", "hr@careers.acme.test", "ghost@acme.test",
        ]))

    hr, jobs, gmail, sub, ghost = asyncio.run(run())
    assert lookups.count("acme.test") == 1  # shared across concurrent checks
    assert hr.domain_owned and hr.mx_ok and hr.mailbox == "unknown" and hr.evidence_confirmed
    assert jobs.evidence_confirmed
    assert gmail.disposable_or_free and not gmail.domain_owned
    assert sub.domain_owned and sub.mx_ok is False  # careers.acme.test has no mail server
    assert ghost.evidence_confirmed is False


def test_company_on_a_free_provider_domain_is_not_penalised():
    verifier = Verifier(["gmail.com"], mx_lookup=lambda d: _const(["mx"]))
    result = asyncio.run(verifier.verify(cand("careers@gmail.com", url=None)))
    assert result.domain_owned and not result.disposable_or_free


def test_dns_failure_leaves_mx_unknown():
    verifier = Verifier(["acme.test"], mx_lookup=lambda d: _const(None))
    assert asyncio.run(verifier.verify(cand("hr@acme.test"))).mx_ok is None


async def _const(value):
    return value

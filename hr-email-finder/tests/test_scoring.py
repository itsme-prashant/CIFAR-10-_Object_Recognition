import pytest

from hr_email_finder.patterns import has_hr_context
from hr_email_finder.schemas import EmailCandidate, VerificationResult
from hr_email_finder.scoring import rank, score_candidate
from hr_email_finder.signals import classify_local_part


def candidate(**overrides) -> EmailCandidate:
    fields = dict(
        email="careers@acme.test",
        kind="role_based",
        source_url="https://acme.test/careers",
        source_page_type="careers",
        snippet="Send your CV to careers@acme.test",
        found_by="test",
    )
    fields.update(overrides)
    return EmailCandidate(**fields)


def verification(**overrides) -> VerificationResult:
    fields = dict(syntax_ok=True, domain_owned=True, mx_ok=True, mailbox="unknown",
                  disposable_or_free=False, evidence_confirmed=True)
    fields.update(overrides)
    return VerificationResult(**fields)


@pytest.mark.parametrize(
    "local, expected",
    [
        ("hr", "hr_role"),
        ("careers.india", "hr_role"),
        ("hrteam", "hr_role"),
        ("talent-acquisition", "hr_role"),
        ("hrishikesh", "personal"),
        ("priya.sharma", "personal"),
        ("info", "general"),
        ("sales", "other_department"),
        ("no-reply", "other_department"),
    ],
)
def test_classify_local_part(local, expected):
    assert classify_local_part(local) == expected


def test_hr_context_ignores_words_inside_emails():
    assert has_hr_context("Send your CV to info@acme.test")
    assert not has_hr_context("Write to careers@acme.test")


def test_careers_page_role_address():
    r = score_candidate(candidate(), verification())
    assert r.score == 0.70
    assert not r.rejected
    assert "mailbox not checked" in r.reasons


def test_confirmed_mailbox_adds_points():
    assert score_candidate(candidate(), verification(mailbox="valid")).score == 0.85
    assert score_candidate(candidate(), verification(mailbox="catch_all")).score == 0.75


def test_generic_address_on_contact_page():
    r = score_candidate(candidate(email="info@acme.test", kind="generic", source_page_type="contact",
                                  snippet="Office: info@acme.test"), verification())
    assert r.score == 0.30


def test_penalties():
    assert score_candidate(candidate(), verification(domain_owned=False)).score == 0.20
    assert score_candidate(candidate(), verification(domain_owned=False, disposable_or_free=True)).score == 0.0


def test_multiple_pages_bonus():
    r = score_candidate(candidate(other_source_urls=["https://acme.test/contact"]), verification())
    assert r.score == 0.75


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"syntax_ok": False}, "invalid email syntax"),
        ({"mx_ok": False}, "no mail server"),
        ({"mailbox": "invalid"}, "rejected the mailbox"),
        ({"evidence_confirmed": False}, "not present"),
    ],
)
def test_rejections(overrides, reason):
    r = score_candidate(candidate(), verification(**overrides))
    assert r.rejected and r.score == 0.0
    assert reason in r.reasons[0]


def test_dns_failure_is_not_a_rejection():
    assert not score_candidate(candidate(), verification(mx_ok=None)).rejected


def test_inferred_is_capped():
    r = score_candidate(candidate(kind="inferred", source_kind="inferred", source_url=None, snippet=None),
                        verification(mailbox="valid", evidence_confirmed=None))
    assert r.score <= 0.50


def test_rank_puts_rejected_last():
    good = score_candidate(candidate(), verification())
    bad = score_candidate(candidate(email="x@acme.test"), verification(mx_ok=False))
    generic = score_candidate(candidate(email="info@acme.test", kind="generic", source_page_type="contact"), verification())
    assert [r.candidate.email for r in rank([bad, generic, good])] == ["careers@acme.test", "info@acme.test", "x@acme.test"]

import asyncio
import json

import httpx
import pytest

from hr_email_finder.cli import render
from hr_email_finder.config import ConfigError, Settings
from hr_email_finder.pipeline import find_hr_email
from hr_email_finder.schemas import CompanyQuery

from conftest import FakeClient, FakeSite, fake_mx, response, tool_use

HOME = """<html><head><title>Acme Corp</title></head><body>
<nav><a href="/careers">Careers</a> <a href="/contact">Contact us</a> <a href="/blog/12345">Blog</a></nav>
<footer>© Acme · <a href="mailto:info@acme.test">info@acme.test</a></footer></body></html>"""
CAREERS = """<html><head><title>Careers at Acme</title></head><body><h1>Join our team</h1>
<p>We're hiring engineers. Send your CV to careers@acme.test and our recruiting team will reply.</p>
<p>Campus hiring questions: priya.sharma@acme.test</p></body></html>"""
CONTACT = """<html><head><title>Contact</title></head><body><p>Sales: sales@acme.test</p>
<form><input name="name"><textarea name="message"></textarea></form></body></html>"""

ACME = {
    "https://acme.test/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
    "https://acme.test/": HOME,
    "https://acme.test/careers": CAREERS,
    "https://acme.test/contact": CONTACT,
}


def lookup(pages, query, settings=None, client=None):
    site = FakeSite(pages)
    report = asyncio.run(find_hr_email(
        query, settings or Settings(use_llm=False), client=client, transport=site.transport, mx_lookup=fake_mx,
    ))
    return report, site


def test_rule_based_lookup_finds_careers_address():
    report, site = lookup(ACME, CompanyQuery(name="Acme", domain="https://www.acme.test"))
    assert report.status == "found"
    best = report.best_email
    assert best.candidate.email == "careers@acme.test"
    assert best.candidate.source_url == "https://acme.test/careers"
    assert best.candidate.found_by == "website_crawler"
    assert best.score == 0.70
    assert [a.candidate.email for a in report.alternatives] == ["info@acme.test"]
    info = report.alternatives[0]
    assert info.score == 0.30 and info.candidate.department_hint == "General"  # the menu's "Careers" link isn't context
    assert report.careers_portal_url == "https://acme.test/careers"
    assert report.contact_form_url == "https://acme.test/contact"
    assert any("1 personal email" in n for n in report.notes)  # priya.sharma@ hidden by default
    assert all("sales@" not in r.candidate.email for r in report.alternatives)
    assert report.pages_fetched == 3 and report.cost_usd == 0
    assert site.count("https://acme.test/blog/12345") == 0

    text = render(report)
    assert "careers@acme.test" in text and "Send your CV" in text
    json.loads(report.model_dump_json())


def test_named_contacts_can_be_included():
    report, _ = lookup(ACME, CompanyQuery(name="Acme", domain="acme.test", allow_named_contacts=True))
    emails = [r.candidate.email for r in [report.best_email, *report.alternatives]]
    assert "priya.sharma@acme.test" in emails


def test_not_found_points_to_careers_portal():
    pages = {
        "https://acme.test/": '<a href="/contact">Contact</a> <a href="https://boards.greenhouse.io/acme">Jobs</a>',
        "https://acme.test/contact": "<p>Write to info@acme.test</p>",
        "https://boards.greenhouse.io/acme": "<h1>Open positions</h1><p>Apply online.</p>",
    }
    report, _ = lookup(pages, CompanyQuery(name="Acme", domain="acme.test"))
    assert report.status == "not_found" and report.best_email is None
    assert [a.candidate.email for a in report.alternatives] == ["info@acme.test"]
    assert report.careers_portal_url == "https://boards.greenhouse.io/acme"
    assert "Apply through the careers page" in report.explanation


def resolver_submit(**overrides):
    profile = dict(canonical_name="Acme Corporation", domains=["https://www.acme.test/"], hq_country="IN",
                   industry="Software", size_band="51-500", careers_url=None, ats_provider=None,
                   alternatives=[], confidence=0.9)
    profile.update(overrides)
    return response(tool_use("submit_company_profile", **profile))


def test_agents_end_to_end_with_fake_claude():
    client = FakeClient([
        resolver_submit(),
        response(tool_use("list_site_pages", domain="acme.test")),
        response(tool_use("fetch_page", url="https://acme.test/careers")),
        response(tool_use(
            "submit_findings",
            candidates=[
                {"email": "careers@acme.test", "department_hint": "Recruiting", "note": "careers page"},
                {"email": "hr@acme.test", "department_hint": "HR", "note": "made up"},
            ],
            careers_url="https://acme.test/careers", contact_form_url=None, notes="",
        )),
    ])
    report, _ = lookup(ACME, CompanyQuery(name="Acme", country="IN"), Settings(), client)

    assert report.company.canonical_name == "Acme Corporation"
    assert report.company.domains == ["acme.test"]
    best = report.best_email.candidate
    assert best.email == "careers@acme.test"
    assert best.department_hint == "Recruiting" and best.found_by == "website_agent"
    assert any("weren't on any fetched page: hr@acme.test" in n for n in report.notes)
    assert report.cost_usd > 0

    resolver_tools = {t.get("type", t.get("name")): t for t in client.calls[0]["tools"]}
    assert resolver_tools["web_search_20260209"]["user_location"] == {"type": "approximate", "country": "IN"}
    assert "web_fetch_20260209" in resolver_tools
    page_result = json.loads(client.tool_results(3)[0]["content"])
    assert page_result["page_type"] == "careers"
    assert page_result["emails"][0]["email"] == "careers@acme.test"


def test_agent_failure_falls_back_to_rule_based_crawler():
    client = FakeClient([response(tool_use("fetch_page", url="https://acme.test/careers")), response(), response()])
    report, _ = lookup(ACME, CompanyQuery(name="Acme", domain="acme.test"), Settings(), client)
    assert report.best_email.candidate.email == "careers@acme.test"
    assert any("rule-based crawler finished" in n for n in report.notes)


def test_ambiguous_company_stops_early():
    client = FakeClient([resolver_submit(alternatives=["Acme Tyres", "Acme Hospitals"], confidence=0.4)])
    report, site = lookup(ACME, CompanyQuery(name="Acme"), Settings(), client)
    assert report.status == "ambiguous_company"
    assert "Acme Tyres" in report.explanation
    assert site.requests == []


def test_missing_credentials(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", "/nonexistent")  # no `ant auth login` profile either
    with pytest.raises(ConfigError, match="No Anthropic credentials"):
        lookup(ACME, CompanyQuery(name="Acme"), Settings())
    report, _ = lookup(ACME, CompanyQuery(name="Acme", domain="acme.test"), Settings())
    assert report.best_email.candidate.email == "careers@acme.test"
    assert any("No Anthropic credentials" in n for n in report.notes)


def test_rule_based_mode_requires_domain():
    with pytest.raises(ConfigError, match="--domain"):
        lookup(ACME, CompanyQuery(name="Acme"))

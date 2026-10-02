import asyncio

import httpx
import pytest

from hr_email_finder.config import Settings
from hr_email_finder.tools.http import Fetcher, FetchError
from hr_email_finder.tools.site import SiteCrawler, classify_page

from conftest import FakeSite


@pytest.mark.parametrize(
    "url, label, expected",
    [
        ("https://acme.test/", "", "home"),
        ("https://acme.test/en/careers/", "", "careers"),
        ("https://acme.test/karriere", "", "careers"),
        ("https://acme.test/about/hr", "", "careers"),
        ("https://acme.test/page?id=3", "Work with us", "careers"),
        ("https://acme.test/contact-us", "", "contact"),
        ("https://acme.de/impressum", "", "legal"),
        ("https://acme.test/privacy-policy", "", "privacy"),
        ("https://acme.test/about-us", "", "about"),
        ("https://acme.test/hrishikesh-bio", "", "other"),
        ("https://acme.test/blog/post", "", "other"),
    ],
)
def test_classify_page(url, label, expected):
    assert classify_page(url, label) == expected


def crawl(site: FakeSite, steps, domains=("acme.test",), **settings):
    async def run():
        async with Fetcher(Settings(use_llm=False, **settings), transport=site.transport) as fetcher:
            crawler = SiteCrawler(fetcher, list(domains), Settings(use_llm=False, **settings))
            return crawler, await steps(crawler)

    return asyncio.run(run())


HOME = """<html><head><title>Acme</title></head><body>
<a href="/blog/2024/10/12345-news">News</a> <a href="/contact">Contact us</a>
<a href="https://jobs.lever.co/acme">Open roles</a> <a href="/careers">Careers</a>
<a href="https://twitter.com/acme">Twitter</a> <a href="/brochure.pdf">Brochure</a></body></html>"""


def test_discover_ranks_relevant_links_and_stays_on_company_hosts():
    site = FakeSite({"https://acme.test/": HOME})
    _, links = crawl(site, lambda c: c.discover("acme.test"))
    urls = [l.url for l in links]
    assert urls[:3] == ["https://acme.test/careers", "https://jobs.lever.co/acme", "https://acme.test/contact"]
    assert "https://twitter.com/acme" not in urls
    assert not any(u.endswith(".pdf") for u in urls)
    assert site.count("https://acme.test/sitemap.xml") == 0  # homepage had enough relevant links


def test_discover_falls_back_to_sitemap_then_common_paths():
    site = FakeSite({
        "https://acme.test/": "<p>Welcome</p>",
        "https://acme.test/robots.txt": httpx.Response(200, text="Sitemap: https://acme.test/site.xml\n"),
        "https://acme.test/site.xml": httpx.Response(
            200, text="<urlset><url><loc>https://acme.test/join-us</loc></url><url><loc>https://acme.test/x</loc></url></urlset>",
            headers={"content-type": "application/xml"},
        ),
    })
    _, links = crawl(site, lambda c: c.discover("acme.test"))
    assert links[0].url == "https://acme.test/join-us" and not links[0].guessed
    assert any(l.guessed and l.url == "https://acme.test/contact" for l in links)
    assert all(l.url != "https://acme.test/x" for l in links)


def test_fetch_refuses_other_hosts_but_allows_ats():
    site = FakeSite({"https://jobs.lever.co/acme": "<p>Apply: talent@acme.test</p>"})

    async def steps(crawler):
        with pytest.raises(FetchError, match="not one of the company's domains"):
            await crawler.fetch("https://evil.test/page")
        return await crawler.fetch("https://jobs.lever.co/acme")

    crawler, record = crawl(site, steps)
    assert record.source_kind == "ats" and record.page_type == "careers"
    assert crawler.emails_on("https://jobs.lever.co/acme") == {"talent@acme.test"}
    assert site.count("https://evil.test/page") == 0


def test_homepage_redirect_to_new_domain_is_adopted():
    site = FakeSite({
        "https://oldname.test/": httpx.Response(301, headers={"location": "https://newname.test/"}),
        "https://newname.test/": '<a href="/careers">Careers</a>',
    })
    crawler, links = crawl(site, lambda c: c.discover("oldname.test"), domains=("oldname.test",))
    assert crawler.domains == ["oldname.test", "newname.test"]
    assert links[0].url == "https://newname.test/careers"

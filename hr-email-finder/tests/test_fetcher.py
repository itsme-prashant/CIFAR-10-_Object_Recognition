import asyncio

import httpx
import pytest

from hr_email_finder.config import Settings
from hr_email_finder.tools.http import Fetcher, FetchError

from conftest import FakeSite


def fetch(site: FakeSite, *urls: str, **settings):
    async def run():
        async with Fetcher(Settings(use_llm=False, **settings), transport=site.transport) as fetcher:
            results = []
            for url in urls:
                try:
                    results.append(await fetcher.get(url))
                except FetchError as exc:
                    results.append(exc)
            return results, fetcher.pages_fetched

    return asyncio.run(run())


def test_robots_disallow_is_respected():
    site = FakeSite({
        "https://acme.test/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
        "https://acme.test/private/team": "<p>secret</p>",
        "https://acme.test/careers": "<p>jobs</p>",
    })
    (private, careers), _ = fetch(site, "https://acme.test/private/team", "https://acme.test/careers")
    assert isinstance(private, FetchError) and "robots.txt" in str(private)
    assert careers.text == "<p>jobs</p>"
    assert site.count("https://acme.test/private/team") == 0
    assert site.count("https://acme.test/robots.txt") == 1  # cached per origin


@pytest.mark.parametrize("robots_status, allowed", [(404, True), (403, True), (503, False)])
def test_robots_status_rules(robots_status, allowed):
    site = FakeSite({
        "https://acme.test/robots.txt": httpx.Response(robots_status),
        "https://acme.test/": "<p>home</p>",
    })
    (result,), _ = fetch(site, "https://acme.test/")
    assert isinstance(result, FetchError) is not allowed


def test_redirects_are_followed_and_robots_checked_per_hop():
    site = FakeSite({
        "https://acme.test/jobs": httpx.Response(301, headers={"location": "/careers"}),
        "https://acme.test/careers": "<p>careers</p>",
        "https://acme.test/old": httpx.Response(302, headers={"location": "https://other.test/blocked"}),
        "https://other.test/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
        "https://other.test/blocked": "<p>nope</p>",
    })
    (jobs, old), _ = fetch(site, "https://acme.test/jobs", "https://acme.test/old")
    assert jobs.final_url == "https://acme.test/careers"
    assert isinstance(old, FetchError)
    assert site.count("https://other.test/blocked") == 0


def test_budget_cache_and_content_types():
    site = FakeSite({
        "https://acme.test/a": "<p>a</p>",
        "https://acme.test/file": httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}),
        "https://acme.test/b": "<p>b</p>",
    })
    (a, a_again, pdf, b), pages = fetch(
        site, "https://acme.test/a", "https://acme.test/a", "https://acme.test/file", "https://acme.test/b", max_pages=2
    )
    assert a is a_again and site.count("https://acme.test/a") == 1
    assert isinstance(pdf, FetchError) and "application/pdf" in str(pdf)
    assert isinstance(b, FetchError) and "budget" in str(b)
    assert pages == 2


def test_parallel_requests_for_one_url_share_a_fetch():
    site = FakeSite({"https://acme.test/careers": "<p>jobs</p>"})

    async def run():
        async with Fetcher(Settings(use_llm=False), transport=site.transport) as fetcher:
            pages = await asyncio.gather(*(fetcher.get("https://acme.test/careers") for _ in range(3)))
            return pages, fetcher.pages_fetched

    pages, fetched = asyncio.run(run())
    assert pages[0] is pages[1] is pages[2]
    assert fetched == 1 and site.count("https://acme.test/careers") == 1


def test_large_bodies_are_truncated():
    site = FakeSite({"https://acme.test/big": "x" * 5000})
    (page,), _ = fetch(site, "https://acme.test/big", max_page_bytes=1000)
    assert len(page.text) == 1000

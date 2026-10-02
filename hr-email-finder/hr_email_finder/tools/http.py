"""Polite async fetching: robots.txt (RFC 9309), per-host limits, a page budget and a cache."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml")
_MAX_CRAWL_DELAY_S = 10.0


class FetchError(Exception):
    """A page could not be fetched. The message is safe to show to an agent."""


@dataclass(frozen=True)
class FetchedPage:
    url: str  # as requested
    final_url: str  # after redirects
    status: int
    content_type: str
    text: str

    @property
    def is_html(self) -> bool:
        return not self.content_type or "html" in self.content_type


@dataclass(frozen=True)
class _Raw:
    status: int
    headers: httpx.Headers
    body: bytes
    charset: str | None
    is_redirect: bool


class _AllowAll:
    def can_fetch(self, useragent: str, url: str) -> bool:
        return True

    def crawl_delay(self, useragent: str) -> None:
        return None

    def site_maps(self) -> None:
        return None


class _DisallowAll(_AllowAll):
    def can_fetch(self, useragent: str, url: str) -> bool:
        return False


class Fetcher:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=settings.request_timeout_s,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            },
        )
        self._robots: dict[str, asyncio.Future] = {}
        self._host_slots: dict[str, asyncio.Semaphore] = {}
        self._delay_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._cache: dict[str, FetchedPage] = {}
        self._in_flight: dict[str, asyncio.Future] = {}
        self.pages_fetched = 0

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    @property
    def pages_left(self) -> int:
        return max(0, self._settings.max_pages - self.pages_fetched)

    async def get(self, url: str) -> FetchedPage:
        if url in self._cache:
            return self._cache[url]
        if url not in self._in_flight:  # parallel requests for one URL share a single fetch
            self._in_flight[url] = asyncio.ensure_future(self._get(url))
        try:
            return await self._in_flight[url]
        finally:
            self._in_flight.pop(url, None)

    async def _get(self, url: str) -> FetchedPage:
        if self.pages_left <= 0:
            raise FetchError("page budget used up")
        self.pages_fetched += 1

        current = url
        for _ in range(self._settings.max_redirects + 1):
            robots = await self._robots_for(current)
            if not robots.can_fetch(self._settings.user_agent, current):
                raise FetchError(f"robots.txt disallows {current}")
            raw = await self._request(current, robots)
            if not raw.is_redirect:
                break
            current = urljoin(current, raw.headers["location"])
        else:
            raise FetchError(f"too many redirects from {url}")

        if raw.status >= 400:
            raise FetchError(f"HTTP {raw.status} for {current}")
        content_type = raw.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type and not content_type.startswith(_TEXT_TYPES):
            raise FetchError(f"skipped {content_type} content at {current}")
        try:
            text = raw.body.decode(raw.charset or "utf-8", errors="replace")
        except LookupError:  # unknown charset name
            text = raw.body.decode("utf-8", errors="replace")

        page = FetchedPage(url, current, raw.status, content_type, text)
        self._cache[url] = self._cache[current] = page
        log.info("fetched %s (%d, %d chars)", current, raw.status, len(text))
        return page

    async def sitemaps(self, url: str) -> list[str]:
        """Sitemap URLs listed in the site's robots.txt."""
        robots = await self._robots_for(url)
        return list(robots.site_maps() or [])

    async def _request(self, url: str, robots: RobotFileParser | _AllowAll) -> _Raw:
        host = urlsplit(url).netloc
        slot = self._host_slots.setdefault(host, asyncio.Semaphore(self._settings.per_host_concurrency))
        async with slot:
            await self._respect_crawl_delay(host, robots)
            try:
                async with self._client.stream("GET", url) as resp:
                    body = bytearray()
                    if not resp.is_redirect:
                        async for chunk in resp.aiter_bytes():
                            body += chunk
                            if len(body) >= self._settings.max_page_bytes:
                                break
                    return _Raw(
                        resp.status_code,
                        resp.headers,
                        bytes(body[: self._settings.max_page_bytes]),
                        resp.charset_encoding,
                        resp.is_redirect,
                    )
            except httpx.HTTPError as exc:
                raise FetchError(f"network error fetching {url}: {type(exc).__name__}") from exc

    async def _respect_crawl_delay(self, host: str, robots: RobotFileParser | _AllowAll) -> None:
        delay = robots.crawl_delay(self._settings.user_agent)
        if not delay:
            return
        async with self._delay_locks.setdefault(host, asyncio.Lock()):
            wait = self._last_request.get(host, 0.0) + min(float(delay), _MAX_CRAWL_DELAY_S) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()

    async def _robots_for(self, url: str) -> RobotFileParser | _AllowAll:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = asyncio.ensure_future(self._load_robots(origin))
        return await self._robots[origin]

    async def _load_robots(self, origin: str) -> RobotFileParser | _AllowAll:
        try:
            resp = await self._client.get(origin + "/robots.txt", follow_redirects=True)
        except httpx.HTTPError:
            return _DisallowAll()  # RFC 9309 §2.3.1.4: unreachable → assume complete disallow
        if resp.status_code >= 500:
            return _DisallowAll()
        if resp.status_code >= 400:
            return _AllowAll()  # RFC 9309 §2.3.1.3: unavailable → no restrictions
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser

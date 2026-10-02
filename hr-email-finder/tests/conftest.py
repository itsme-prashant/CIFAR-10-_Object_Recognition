"""Test helpers: a fake website, a fake DNS lookup and a scripted fake Claude client."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Callable

import httpx


class FakeSite:
    """httpx transport serving fixed pages keyed by full URL; records every request."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = pages
        self.requests: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        page = self.pages.get(url)
        if page is None:
            return httpx.Response(404, text="not found")
        if isinstance(page, httpx.Response):
            return page
        return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})

    def count(self, url: str) -> int:
        return self.requests.count(url)


async def fake_mx(domain: str) -> list[str] | None:
    return [f"mx.{domain}"]


def usage(input_tokens: int = 1000, output_tokens: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        server_tool_use=None,
    )


def response(*blocks: Any, stop_reason: str | None = None, model: str = "claude-opus-5-5") -> SimpleNamespace:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), usage=usage(), model=model, stop_details=None)


_ids = iter(range(1, 1_000_000))


def tool_use(name: str, **tool_input: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=tool_input)


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


class FakeClient:
    """Stands in for AsyncAnthropic: returns scripted responses from beta.messages.create."""

    def __init__(self, script: list[SimpleNamespace | Callable[[dict[str, Any]], SimpleNamespace]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> SimpleNamespace:
        kwargs["messages"] = copy.copy(kwargs["messages"])  # snapshot; the loop keeps appending
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("FakeClient script ran out")
        step = self.script.pop(0)
        return step(kwargs) if callable(step) else step

    def tool_results(self, call_index: int) -> list[dict[str, Any]]:
        """tool_result blocks the loop sent in a given request's last user message."""
        return self.calls[call_index]["messages"][-1]["content"]

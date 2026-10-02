"""A small async agent loop on the Claude Messages API, with cost tracking.

A manual loop rather than the SDK's beta Tool Runner: the Python runner ends silently on a
`pause_turn` from server tools (web search/fetch), and we want per-response budget and cost
accounting plus concurrent execution of parallel tool calls.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from .config import Settings

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# List prices per million tokens: input, output, cache read (DESIGN.md §7).
PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
}
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute cache writes
MAX_TOKENS = 16_000


class AgentError(Exception):
    """An agent run ended without a usable result."""


class ToolError(Exception):
    """Raised by a tool handler; the message goes back to the model as an error result."""


@dataclass
class ClientTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]

    def to_param(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0
    web_fetches: int = 0
    cost_usd: float = 0.0

    def add(self, response: Any, configured_model: str) -> None:
        u = response.usage
        cache_read = u.cache_read_input_tokens or 0
        cache_write = u.cache_creation_input_tokens or 0
        self.requests += 1
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write
        if u.server_tool_use is not None:
            self.web_searches += u.server_tool_use.web_search_requests or 0
            self.web_fetches += u.server_tool_use.web_fetch_requests or 0
        # A refusal fallback can serve the turn on another model; price unknown ones as configured.
        price_in, price_out, price_cache = PRICES_PER_MTOK.get(
            response.model, PRICES_PER_MTOK[configured_model]
        )
        self.cost_usd += (
            u.input_tokens * price_in
            + cache_write * price_in * CACHE_WRITE_MULTIPLIER
            + cache_read * price_cache
            + u.output_tokens * price_out
        ) / 1_000_000


async def run_agent(
    client: Any,
    settings: Settings,
    *,
    system: str,
    prompt: str,
    tools: Sequence[ClientTool],
    finish_tool: str,
    usage: Usage,
    server_tools: Sequence[dict[str, Any]] = (),
) -> None:
    """Loop until the model calls `finish_tool` successfully. Raises AgentError otherwise."""
    by_name = {t.name: t for t in tools}
    params: dict[str, Any] = {
        "model": settings.model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "tools": [*server_tools, *(t.to_param() for t in tools)],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": settings.effort},
        "cache_control": {"type": "ephemeral"},  # caches the growing conversation between turns
    }
    if settings.refusal_fallbacks:
        params.update(betas=[FALLBACK_BETA], fallbacks="default")

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    nudged = False
    for turn in range(1, settings.agent_max_turns + 1):
        response = await client.beta.messages.create(messages=messages, **params)
        usage.add(response, settings.model)
        log.info("turn %d: stop_reason=%s", turn, response.stop_reason)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AgentError(f"the model declined the request ({getattr(details, 'category', None) or 'no category'})")
        if response.stop_reason == "max_tokens":
            raise AgentError("a response hit max_tokens")
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue  # a server tool paused mid-turn; resending lets it continue

        calls = [block for block in response.content if block.type == "tool_use"]
        if not calls:
            if nudged:
                raise AgentError(f"the agent stopped without calling {finish_tool}")
            nudged = True
            messages.append({"role": "user", "content": f"Call {finish_tool} now to report your result."})
            continue

        results = await asyncio.gather(*(_run_tool(by_name, call) for call in calls))
        messages.append({"role": "user", "content": list(results)})  # all results in one message
        if any(call.name == finish_tool and not result.get("is_error") for call, result in zip(calls, results)):
            return

    raise AgentError(f"the agent used all {settings.agent_max_turns} turns without calling {finish_tool}")


async def _run_tool(by_name: dict[str, ClientTool], call: Any) -> dict[str, Any]:
    tool = by_name.get(call.name)
    if tool is None:
        return _tool_result(call.id, f"Unknown tool {call.name!r}.", error=True)
    try:
        return _tool_result(call.id, await tool.handler(call.input))
    except ToolError as exc:
        return _tool_result(call.id, str(exc), error=True)


def _tool_result(tool_use_id: str, content: str, *, error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if error:
        result["is_error"] = True
    return result


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON-schema helper for strict tools: a value or null."""
    return {"anyOf": [schema, {"type": "null"}]}

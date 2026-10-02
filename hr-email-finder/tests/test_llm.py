import asyncio

import pytest

from hr_email_finder.config import Settings
from hr_email_finder.llm import AgentError, ClientTool, ToolError, Usage, run_agent

from conftest import FakeClient, response, text, tool_use

SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def make_tools(log: list[str]):
    async def lookup(args):
        log.append("lookup")
        return "result"

    async def broken(args):
        raise ToolError("that page is not allowed")

    async def finish(args):
        log.append("finish")
        return "done"

    return [
        ClientTool("lookup", "d", SCHEMA, lookup),
        ClientTool("broken", "d", SCHEMA, broken),
        ClientTool("finish", "d", SCHEMA, finish),
    ]


def run(client, log, **settings):
    usage = Usage()
    asyncio.run(run_agent(
        client, Settings(**settings), system="sys", prompt="go",
        tools=make_tools(log), finish_tool="finish", usage=usage,
    ))
    return usage


def test_parallel_tools_one_result_message_then_finish():
    client = FakeClient([
        response(tool_use("lookup"), tool_use("broken")),
        response(tool_use("finish")),
    ])
    log: list[str] = []
    usage = run(client, log)
    assert log == ["lookup", "finish"]
    results = client.tool_results(1)
    assert [r.get("is_error", False) for r in results] == [False, True]
    assert results[1]["content"] == "that page is not allowed"
    assert usage.requests == 2
    assert usage.cost_usd == pytest.approx(2 * (1000 * 4 + 100 * 20) / 1e6)


def test_request_parameters():
    client = FakeClient([response(tool_use("finish"))])
    run(client, [], effort="medium")
    params = client.calls[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "medium"}
    assert params["fallbacks"] == "default" and params["betas"] == ["server-side-fallback-2026-07-01"]
    assert all(t["strict"] for t in params["tools"])
    assert "tool_choice" not in params  # forced tool choice is rejected on current models


def test_pause_turn_is_resumed():
    client = FakeClient([response(text("searching"), stop_reason="pause_turn"), response(tool_use("finish"))])
    run(client, [])
    assert client.calls[1]["messages"][-1]["role"] == "assistant"


def test_nudges_once_then_fails():
    client = FakeClient([response(text("all done")), response(tool_use("finish"))])
    run(client, [])
    assert "Call finish now" in client.calls[1]["messages"][-1]["content"]

    client = FakeClient([response(text("done")), response(text("really done"))])
    with pytest.raises(AgentError, match="without calling finish"):
        run(client, [])


def test_refusal_and_turn_limit_raise():
    with pytest.raises(AgentError, match="declined"):
        run(FakeClient([response(text(""), stop_reason="refusal")]), [])
    with pytest.raises(AgentError, match="all 2 turns"):
        run(FakeClient([response(tool_use("lookup")), response(tool_use("lookup"))]), [], agent_max_turns=2)

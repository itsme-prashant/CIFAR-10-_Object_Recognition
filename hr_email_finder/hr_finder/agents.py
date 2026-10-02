"""LLM agents. Each is a small Claude tool-use loop that ends by calling `submit_result`."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from . import tools as T

MODEL = os.environ.get("HR_FINDER_MODEL", "claude-opus-5-5")
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 8}
FETCH_PAGE_SCHEMA = {
    "name": "fetch_page",
    "description": "Fetch a public web page. Returns text, HTTP status and any email addresses found in the HTML.",
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    },
}
MX_SCHEMA = {
    "name": "mx_lookup",
    "description": "Check whether a domain has MX records (can receive email).",
    "input_schema": {
        "type": "object",
        "properties": {"domain": {"type": "string"}},
        "required": ["domain"],
        "additionalProperties": False,
    },
}
LOCAL_HANDLERS: dict[str, Callable[..., Any]] = {
    "fetch_page": lambda url: T.fetch_page(url),
    "mx_lookup": lambda domain: T.mx_lookup(domain),
}


@dataclass
class Agent:
    name: str
    system: str
    result_schema: dict
    tools: list[dict] = field(default_factory=list)
    max_turns: int = 16
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)

    def run(self, task: str) -> dict:
        submit = {
            "name": "submit_result",
            "description": "Call exactly once when finished, with your final structured answer.",
            "input_schema": self.result_schema,
        }
        tools = [*self.tools, submit]
        messages: list[dict] = [{"role": "user", "content": task}]
        for _ in range(self.max_turns):
            resp = self.client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=self.system,
                tools=tools,
                messages=messages,
                output_config={"effort": "medium"},
            )
            if resp.stop_reason == "refusal":
                return {"error": "model refused"}
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":  # server-side search still running
                continue
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                if block.name == "submit_result":
                    return block.input
                fn = LOCAL_HANDLERS.get(block.name)
                try:
                    out = fn(**block.input) if fn else {"error": f"unknown tool {block.name}"}
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(out)})
                except Exception as e:  # report to the model rather than crash the run
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True}
                    )
            if not results:
                messages.append({"role": "user", "content": "Finish by calling submit_result."})
            else:
                messages.append({"role": "user", "content": results})
        return {"error": f"{self.name} hit max_turns"}


PUBLIC_ONLY = (
    "Use only publicly published information (company sites, job postings, press pages, "
    "public professional profiles surfaced by search). Never invent an email address; if you did not "
    "see it on a page, say so. Do not try to bypass logins or paywalls."
)


def domain_agent() -> Agent:
    return Agent(
        name="DomainAgent",
        system=f"You resolve a company name to its official primary email/web domain. {PUBLIC_ONLY} "
        "Prefer the domain used in the company's own careers page or in its staff emails; "
        "watch for lookalike or parent-company domains. Confirm with mx_lookup.",
        tools=[WEB_SEARCH, FETCH_PAGE_SCHEMA, MX_SCHEMA],
        result_schema={
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "domain": {"type": "string", "description": "e.g. acme.com, no scheme"},
                "careers_url": {"type": "string"},
                "alt_domains": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "notes": {"type": "string"},
            },
            "required": ["company", "domain", "confidence"],
        },
    )


def contact_agent() -> Agent:
    return Agent(
        name="ContactAgent",
        system=f"You find HR / recruiting / talent-acquisition contacts for a company. {PUBLIC_ONLY} "
        "Check the careers page, contact/about pages, job postings (often list a recruiter or "
        "an apply-by-email address), and search results. Record for every email the exact URL where "
        "you saw it. Also record named HR/recruiting people (name, title, source URL) even without an email.",
        tools=[WEB_SEARCH, FETCH_PAGE_SCHEMA],
        result_schema={
            "type": "object",
            "properties": {
                "emails": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "email": {"type": "string"},
                            "owner": {"type": "string", "description": "person name or 'generic mailbox'"},
                            "title": {"type": "string"},
                            "source_url": {"type": "string"},
                        },
                        "required": ["email", "source_url"],
                    },
                },
                "people": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "first_name": {"type": "string"},
                            "last_name": {"type": "string"},
                            "title": {"type": "string"},
                            "source_url": {"type": "string"},
                        },
                        "required": ["first_name", "last_name", "title", "source_url"],
                    },
                },
            },
            "required": ["emails", "people"],
        },
    )


def pattern_agent() -> Agent:
    return Agent(
        name="PatternAgent",
        system=f"You work out the company's email address format from PUBLIC examples. {PUBLIC_ONLY} "
        "Find 2+ real employee emails at the domain (press releases, papers, GitHub commits, "
        "contact pages), pair each with the person's name, and infer the format as a template using "
        "{first}, {last}, {f} (first initial), {l} (last initial), e.g. '{first}.{last}'. "
        "If examples disagree or you found fewer than 2, say confidence is low.",
        tools=[WEB_SEARCH, FETCH_PAGE_SCHEMA],
        result_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "template, or empty string if unknown"},
                "examples": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "email": {"type": "string"},
                            "name": {"type": "string"},
                            "source_url": {"type": "string"},
                        },
                        "required": ["email", "name", "source_url"],
                    },
                },
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["pattern", "examples", "confidence"],
        },
    )

"""Settings for one lookup."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

SUPPORTED_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
EFFORT_LEVELS = ("low", "medium", "high")

DEFAULT_USER_AGENT = "HREmailFinder/0.1 (public HR contact discovery; respects robots.txt)"


class ConfigError(Exception):
    """The lookup can't run with the given inputs or settings."""


@dataclass(frozen=True)
class Settings:
    model: str = "claude-opus-5-5"
    effort: str = "low"  # worker agents run at low effort (DESIGN.md §3)
    use_llm: bool = True
    refusal_fallbacks: bool = True  # server-side fallback model if a request is declined

    # Crawling (DESIGN.md §7). robots.txt fetches don't count toward max_pages.
    max_pages: int = 20
    per_host_concurrency: int = 3
    request_timeout_s: float = 15.0
    max_page_bytes: int = 2_000_000
    max_redirects: int = 5
    user_agent: str = field(
        default_factory=lambda: os.environ.get("HR_FINDER_USER_AGENT", DEFAULT_USER_AGENT)
    )

    # Agents
    agent_max_turns: int = 12
    passage_chars: int = 6_000  # page text handed to the Website Agent per fetch
    resolver_max_searches: int = 5
    resolver_max_fetches: int = 3

    # Verification
    smtp_probe: bool = False  # RCPT TO probe; off by default (DESIGN.md §3.6)

    def __post_init__(self) -> None:
        if self.model not in SUPPORTED_MODELS:
            raise ConfigError(f"model must be one of {', '.join(SUPPORTED_MODELS)}")
        if self.effort not in EFFORT_LEVELS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORT_LEVELS)}")
        if self.max_pages < 1:
            raise ConfigError("max_pages must be at least 1")

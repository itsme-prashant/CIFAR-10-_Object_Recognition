"""Hostname helpers."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

LABEL_RE = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")


def normalize_domain(value: str) -> str:
    """Turn 'https://www.Acme.com/careers' into 'acme.com'. Raises ValueError if invalid."""
    raw = value.strip().lower()
    host = urlsplit(raw if "://" in raw else "//" + raw).hostname or ""
    host = host.rstrip(".").removeprefix("www.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"not a valid domain: {value!r}") from exc
    labels = host.split(".")
    if len(labels) < 2 or labels[-1].isdigit() or not all(LABEL_RE.fullmatch(l) for l in labels):
        raise ValueError(f"not a valid domain: {value!r}")
    return host


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def host_in(host: str, domains: list[str]) -> bool:
    """True if host is one of the domains or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domains)

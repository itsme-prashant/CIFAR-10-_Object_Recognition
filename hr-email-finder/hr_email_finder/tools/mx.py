"""Mail-server (MX) lookups."""

from __future__ import annotations

import dns.asyncresolver
import dns.exception
import dns.resolver

_LIFETIME_S = 5.0


async def lookup_mx(domain: str) -> list[str] | None:
    """Mail hosts for a domain, best first.

    [] means the domain can't receive mail; None means the lookup itself failed.
    """
    try:
        answer = await dns.asyncresolver.resolve(domain, "MX", lifetime=_LIFETIME_S)
    except dns.resolver.NXDOMAIN:
        return []
    except dns.resolver.NoAnswer:
        return await _implicit_mx(domain)
    except dns.exception.DNSException:
        return None
    hosts = [str(r.exchange).rstrip(".") for r in sorted(answer, key=lambda r: r.preference)]
    return [h for h in hosts if h]  # a null MX ("." — RFC 7505) leaves nothing


async def _implicit_mx(domain: str) -> list[str] | None:
    """No MX record: mail goes to the domain's own address record (RFC 5321 §5.1)."""
    for rtype in ("A", "AAAA"):
        try:
            await dns.asyncresolver.resolve(domain, rtype, lifetime=_LIFETIME_S)
            return [domain]
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except dns.exception.DNSException:
            return None
    return []

"""Orchestrator + deterministic VerifierAgent.

DomainAgent -> (ContactAgent || PatternAgent) -> Verifier/Ranker
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import agents, tools as T

CONFIDENCE_ORDER = {"published": 3, "pattern-inferred": 2, "generic-role": 1}


def _same_domain(email: str, domains: set[str]) -> bool:
    return email.rsplit("@", 1)[-1].lower() in domains


def verify_and_rank(domain_info: dict, contacts: dict, pattern: dict, smtp_check: bool = False) -> list[dict]:
    domains = {domain_info["domain"].lower(), *(d.lower() for d in domain_info.get("alt_domains", []))}
    primary = domain_info["domain"].lower()
    mx_ok = T.mx_lookup(primary).get("accepts_mail", False)
    out: dict[str, dict] = {}

    def add(email, kind, owner, source, note=""):
        email = email.lower().strip()
        if email in out and CONFIDENCE_ORDER[out[email]["kind"]] >= CONFIDENCE_ORDER[kind]:
            return
        out[email] = {"email": email, "kind": kind, "owner": owner, "source": source, "note": note}

    # 1. Emails actually published on the web -> highest trust (domain must match the company).
    for e in contacts.get("emails", []):
        if _same_domain(e["email"], domains):
            add(e["email"], "published", e.get("owner", ""), e.get("source_url", ""))

    # 2. Named HR people x inferred pattern.
    template = pattern.get("pattern") or ""
    for p in contacts.get("people", []):
        owner = f'{p["first_name"]} {p["last_name"]} ({p["title"]})'
        if template and pattern.get("confidence") in ("high", "medium"):
            email = T.apply_pattern(template, p["first_name"], p["last_name"], primary)
            if email:
                add(email, "pattern-inferred", owner, p["source_url"], f"format {template}")
        elif not template:
            for email in T.generate_candidates(p["first_name"], p["last_name"], primary)[:3]:
                add(email, "pattern-inferred", owner, p["source_url"], "pattern unknown; common guess")

    # 3. Generic role mailboxes as a fallback.
    for local in T.ROLE_LOCAL_PARTS[:4]:
        add(f"{local}@{primary}", "generic-role", "generic mailbox", "", "guess; not seen published")

    ranked = sorted(out.values(), key=lambda r: -CONFIDENCE_ORDER[r["kind"]])
    for r in ranked:
        r["domain_accepts_mail"] = mx_ok
        if smtp_check and r["kind"] != "published":
            r["smtp"] = T.smtp_probe(r["email"])["result"]
    return ranked


def find_hr_emails(company: str, smtp_check: bool = False, log=print) -> dict:
    log(f"[1/3] DomainAgent: resolving domain for {company!r}")
    domain_info = agents.domain_agent().run(f"Company: {company}")
    if "domain" not in domain_info:
        return {"company": company, "error": domain_info.get("error", "domain not found"), "results": []}
    domain = domain_info["domain"]
    log(f"      -> {domain} ({domain_info['confidence']})")

    log("[2/3] ContactAgent + PatternAgent running in parallel")
    task = f"Company: {company}\nOfficial domain: {domain}\nCareers URL: {domain_info.get('careers_url', 'unknown')}"
    with ThreadPoolExecutor(2) as pool:
        f_contacts = pool.submit(agents.contact_agent().run, task)
        f_pattern = pool.submit(agents.pattern_agent().run, task)
        contacts, pattern = f_contacts.result(), f_pattern.result()
    contacts = contacts if "emails" in contacts else {"emails": [], "people": []}
    pattern = pattern if "pattern" in pattern else {"pattern": "", "examples": [], "confidence": "low"}

    log("[3/3] Verifier: MX check, domain match, ranking")
    results = verify_and_rank(domain_info, contacts, pattern, smtp_check)
    return {"company": company, "domain_info": domain_info, "pattern": pattern, "results": results}

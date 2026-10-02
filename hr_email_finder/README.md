# HR Email Finder (multi-agent)

Finds publicly available HR / recruiting email addresses for a company, with a source and confidence label for each.

```
Orchestrator (pipeline.py)
 ├─ DomainAgent    web search + fetch + MX  → official domain, careers URL
 ├─ ContactAgent ┐ web search + fetch       → published HR emails, named recruiters   (run in parallel)
 ├─ PatternAgent ┘ web search + fetch       → company email format from public examples
 └─ Verifier       (deterministic Python)   → domain match, MX check, ranking, optional SMTP probe
```

Each LLM agent is a Claude tool-use loop that ends by calling `submit_result` with a JSON schema.

## Result confidence
- `published`: seen on a public page (source URL included).
- `pattern-inferred`: a named HR person plus the company's inferred format. Not seen published, so it is a guess.
- `generic-role`: careers@/jobs@/etc. Guess only.

## Run
```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python main.py "Stripe"            # add --json or --smtp-check
python -m pytest
```
`HR_FINDER_MODEL` overrides the model (default `claude-opus-5-5`).

## Notes
- Uses public information only. Inferred addresses are guesses. Check them before relying on them.
- `--smtp-check` is opt-in. Many servers are catch-all or block it, and port 25 is often blocked on cloud hosts.
- Use for legitimate recruiting or job-application outreach. Follow anti-spam law (CAN-SPAM, GDPR) when emailing.

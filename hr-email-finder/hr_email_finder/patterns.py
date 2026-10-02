"""Regular expressions shared by extraction, crawling and scoring."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[a-z0-9][a-z0-9._%+-]{0,63}@"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?![\w-])",
    re.IGNORECASE,
)

HR_CONTEXT_RE = re.compile(
    r"\b(?:hr|human resources?|careers?|jobs?|job openings?|recruit\w*|talent\w*|hiring|"
    r"resumes?|résumés?|cvs?|curriculum vitae|apply (?:now|online|here|to)|applications?|"
    r"vacanc\w*|internships?|placements?|join (?:us|our team)|karriere|bewerbung\w*|"
    r"stellenangebot\w*|emploi|recrutement|empleo|candidat\w*)\b",
    re.IGNORECASE,
)


def has_hr_context(text: str | None) -> bool:
    """HR words in text. Emails are removed first ('careers@' isn't context)."""
    return bool(text and HR_CONTEXT_RE.search(EMAIL_RE.sub(" ", text)))

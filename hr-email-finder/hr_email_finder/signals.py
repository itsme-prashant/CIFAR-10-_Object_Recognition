"""Text signals shared by the crawler and the scorer."""

from __future__ import annotations

import re
from typing import Literal

LocalPartClass = Literal["hr_role", "general", "other_department", "personal"]

_HR_TOKENS = {
    "hr", "hrd", "careers", "career", "jobs", "job", "recruitment", "recruiting", "recruit",
    "recruiter", "recruiters", "talent", "people", "peopleops", "hiring", "hire", "resume",
    "resumes", "cv", "cvs", "apply", "applications", "karriere", "bewerbung", "campus",
    "internship", "internships", "placement", "placements", "staffing",
}
_HR_COMPACT = {
    "humanresources", "talentacquisition", "joinus", "workwithus", "jobsatcompany", "hrteam",
}
# "hrteam", "hrindia" — but not names such as "hrishikesh"
_HR_PREFIX_SUFFIXES = {"team", "dept", "department", "desk", "ops", "admin", "manager", "head", "india", "global"}
_GENERAL = {
    "info", "contact", "contactus", "hello", "hi", "office", "enquiries", "enquiry", "inquiries",
    "inquiry", "mail", "admin", "general", "reception", "team",
}
_OTHER_DEPARTMENT = {
    "sales", "support", "help", "helpdesk", "press", "media", "pr", "marketing", "privacy", "dpo",
    "gdpr", "legal", "compliance", "billing", "accounts", "finance", "invoice", "invoices", "ir",
    "investor", "investors", "investorrelations", "noreply", "donotreply", "webmaster", "abuse",
    "security", "partners", "partnerships", "orders", "service", "customerservice", "customercare",
    "care", "feedback", "newsletter", "events", "procurement", "purchase", "vendor", "vendors",
}


def classify_local_part(local: str) -> LocalPartClass:
    local = local.lower()
    tokens = set(re.split(r"[._+-]+", local)) - {""}
    compact = re.sub(r"[._+-]+", "", local)
    if (
        tokens & _HR_TOKENS
        or compact in _HR_COMPACT
        or (compact.startswith("hr") and compact[2:] in _HR_PREFIX_SUFFIXES)
    ):
        return "hr_role"
    if tokens & _OTHER_DEPARTMENT or compact in _OTHER_DEPARTMENT:
        return "other_department"
    if tokens & _GENERAL or compact in _GENERAL:
        return "general"
    return "personal"

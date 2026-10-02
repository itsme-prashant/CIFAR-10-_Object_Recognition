"""Find a company's public HR / recruiting contact email, with evidence.

See DESIGN.md for the architecture. Typical use:

    import asyncio
    from hr_email_finder import CompanyQuery, find_hr_email

    report = asyncio.run(find_hr_email(CompanyQuery(name="Acme", domain="acme.com")))
"""

from .config import ConfigError, Settings
from .pipeline import find_hr_email
from .schemas import CompanyQuery, FinalReport

__all__ = ["CompanyQuery", "ConfigError", "FinalReport", "Settings", "find_hr_email"]
__version__ = "0.1.0"

"""Pure normalization and validation of individual contact fields.

Principles:
- Never invent data. A value is cleaned, accepted, or rejected with a reason; it is never guessed.
- Identity uses the lowercased full email address. Provider-specific tricks (dropping dots, stripping
  "+tags") are deliberately NOT applied: two such addresses may be different people at a work domain.
- Company domains are derived only from explicit domain/website columns, or from a work (non-freemail)
  email address, and the source is always recorded.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    code: str
    field: str
    message: str
    severity: Severity = Severity.WARNING

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message, "severity": self.severity.value}


# Cell values that mean "no data" in exported spreadsheets. Treated as empty, never as real values.
PLACEHOLDERS = frozenset({
    "", "-", "--", "---", "n/a", "na", "n.a.", "none", "null", "nil", "unknown", "not available",
    "not found", "not provided", "no email", "tbd", "#n/a", "?", "nan",
})

FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "yahoo.in", "ymail.com",
    "rocketmail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "hotmail.co.in", "live.com", "live.in",
    "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me",
    "gmx.com", "gmx.net", "gmx.de", "mail.com", "yandex.com", "yandex.ru", "zohomail.com", "zohomail.in",
    "rediffmail.com", "qq.com", "163.com", "126.com", "web.de", "tutanota.com", "fastmail.com",
})

ROLE_LOCAL_PARTS = frozenset({
    "info", "sales", "contact", "contactus", "admin", "administrator", "support", "hello", "office",
    "enquiry", "enquiries", "inquiry", "inquiries", "hr", "careers", "jobs", "marketing", "billing",
    "accounts", "team", "noreply", "no-reply", "donotreply", "reception", "help", "mail", "general",
    "customercare", "care", "feedback", "webmaster", "postmaster", "abuse", "privacy", "legal",
})

_WHITESPACE = re.compile(r"\s+")
_MULTI_EMAIL = re.compile(r"[,;\s]")
_DOMAIN_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_LINKEDIN_PERSON = re.compile(r"^/in/([^/?#]{2,100})/?$", re.IGNORECASE)

MAX_LENGTHS = {
    "first_name": 150, "last_name": 150, "full_name": 300, "title": 300, "company_name": 300,
    "industry": 200, "country": 100, "location": 300, "segment": 200,
}


def clean_text(value: str | None, max_length: int | None = None) -> str | None:
    """Trim, collapse internal whitespace, drop control characters, and map placeholders to None."""
    if value is None:
        return None
    # Any whitespace (newline, tab, NBSP...) becomes a space; other invisible characters are dropped.
    text = "".join(" " if ch.isspace() else ch for ch in value if ch.isspace() or ch.isprintable())
    text = _WHITESPACE.sub(" ", text).strip()
    if text.casefold() in PLACEHOLDERS:
        return None
    if max_length is not None and len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


@dataclass(frozen=True)
class EmailResult:
    original: str | None
    normalized: str | None
    domain: str | None
    issues: tuple[Issue, ...]

    @property
    def is_valid(self) -> bool:
        return self.normalized is not None


def normalize_email(value: str | None) -> EmailResult:
    raw = clean_text(value)
    if raw is None:
        return EmailResult(None, None, None, ())
    raw = raw.strip("<>").removeprefix("mailto:").strip()
    if _MULTI_EMAIL.search(raw):
        return EmailResult(raw, None, None, (Issue(
            "email_multiple", "email",
            "Cell contains more than one value; split it so each person has one email address.", Severity.ERROR),))
    try:
        # Syntax only. No DNS/SMTP probing at import time: it is slow, unreliable, and probing mail
        # servers is a deliverability risk. Pre-send checks happen in the sending pipeline.
        result = validate_email(raw, check_deliverability=False)
    except EmailNotValidError as exc:
        return EmailResult(raw, None, None, (Issue("email_invalid", "email", f"Invalid email address: {exc}",
                                                   Severity.ERROR),))
    normalized = result.normalized.lower()
    local, _, domain = normalized.rpartition("@")
    issues: list[Issue] = []
    if domain in FREEMAIL_DOMAINS:
        issues.append(Issue("email_freemail", "email",
                            "Personal mailbox provider, not a work address; company domain not derived from it."))
    if local in ROLE_LOCAL_PARTS:
        issues.append(Issue("email_role_account", "email",
                            "Role-based address (e.g. info@, sales@); usually not an individual person."))
    return EmailResult(raw, normalized, domain, tuple(issues))


def normalize_domain(value: str | None) -> str | None:
    """Return a bare, lowercase registrable hostname (without 'www.') or None if not a valid public host.

    Accepts "Acme.com", "https://www.acme.com/about", "acme.com:443". Sub-domains other than "www" are
    kept (uk.acme.com stays distinct): merging them would need a public-suffix decision we don't guess.
    """
    raw = clean_text(value)
    if raw is None:
        return None
    candidate = raw if "://" in raw else f"//{raw}"
    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    host = host.removeprefix("www.")
    try:
        ipaddress.ip_address(host)
        return None  # IP addresses are not company domains
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = host.split(".")
    if len(labels) < 2 or len(host) > 253 or not all(_DOMAIN_LABEL.match(label) for label in labels):
        return None
    if labels[-1].isdigit() or host in {"localhost", "local"} or labels[-1] in {"local", "internal", "localhost"}:
        return None
    return host


def normalize_website(value: str | None) -> tuple[str | None, Issue | None]:
    raw = clean_text(value)
    if raw is None:
        return None, None
    domain = normalize_domain(raw)
    if domain is None:
        return None, Issue("website_invalid", "company_website", f"Not a valid website: {raw[:100]}")
    scheme = urlsplit(raw).scheme.lower() if "://" in raw else "https"
    if scheme not in {"http", "https"}:
        return None, Issue("website_invalid", "company_website", "Website must be an http(s) address.")
    return f"{scheme}://{domain}", None


def normalize_linkedin(value: str | None) -> tuple[str | None, Issue | None]:
    """Accept only personal profile URLs (linkedin.com/in/<handle>); company pages are rejected."""
    raw = clean_text(value)
    if raw is None:
        return None, None
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        parts = None
    host = (parts.hostname or "").lower() if parts else ""
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None, Issue("linkedin_invalid", "linkedin_url", "Not a LinkedIn URL; value ignored.")
    match = _LINKEDIN_PERSON.match(parts.path) if parts else None
    if not match:
        return None, Issue("linkedin_not_profile", "linkedin_url",
                           "Not a personal LinkedIn profile (expected linkedin.com/in/...); value ignored.")
    return f"https://www.linkedin.com/in/{match.group(1)}", None


def build_full_name(first: str | None, last: str | None, full: str | None) -> str | None:
    """Use the supplied full name; otherwise join first and last. Never split a full name into parts."""
    if full:
        return full
    joined = " ".join(p for p in (first, last) if p)
    return joined or None


def name_key(full_name: str | None) -> str | None:
    """Case/spacing-insensitive key used only to *flag* possible duplicates, never to merge."""
    if not full_name:
        return None
    return _WHITESPACE.sub(" ", full_name.casefold()).strip()


def company_name_key(name: str | None) -> str | None:
    return _WHITESPACE.sub(" ", name.casefold()).strip() if name else None

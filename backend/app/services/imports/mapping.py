"""Column mapping: CSV header -> prospect field, custom attribute, or ignore."""

from __future__ import annotations

import re
from enum import StrEnum


class Target(StrEnum):
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    FULL_NAME = "full_name"
    EMAIL = "email"
    TITLE = "title"
    COMPANY_NAME = "company_name"
    COMPANY_DOMAIN = "company_domain"
    COMPANY_WEBSITE = "company_website"
    LINKEDIN_URL = "linkedin_url"
    INDUSTRY = "industry"
    COUNTRY = "country"
    LOCATION = "location"
    # Campaign/segment assignment. Campaigns arrive in Milestone 6; the value is kept for enrollment then.
    SEGMENT = "segment"
    ATTRIBUTE = "attribute"
    IGNORE = "ignore"


SINGLE_USE_TARGETS = frozenset(t for t in Target if t not in {Target.ATTRIBUTE, Target.IGNORE})

TARGET_LABELS: dict[Target, str] = {
    Target.FIRST_NAME: "First name",
    Target.LAST_NAME: "Last name",
    Target.FULL_NAME: "Full name",
    Target.EMAIL: "Work email",
    Target.TITLE: "Job title",
    Target.COMPANY_NAME: "Company name",
    Target.COMPANY_DOMAIN: "Company domain",
    Target.COMPANY_WEBSITE: "Company website",
    Target.LINKEDIN_URL: "LinkedIn profile URL",
    Target.INDUSTRY: "Industry",
    Target.COUNTRY: "Country / region",
    Target.LOCATION: "Location",
    Target.SEGMENT: "Campaign / segment",
    Target.ATTRIBUTE: "Custom attribute",
    Target.IGNORE: "Ignore",
}

# Exact matches on a normalized header (lowercase, punctuation removed). Ambiguous headers such as
# "Other Emails (Company / General)" or "Company LinkedIn / Person LinkedIn" are deliberately absent:
# a wrong automatic guess is worse than asking the user.
_ALIASES: dict[Target, set[str]] = {
    Target.EMAIL: {"email", "emails", "work email", "email address", "e mail", "business email", "person email",
                   "contact email", "email id", "individual email", "work email address", "corporate email"},
    Target.FIRST_NAME: {"first name", "firstname", "given name", "fname", "first"},
    Target.LAST_NAME: {"last name", "lastname", "surname", "family name", "lname", "last"},
    Target.FULL_NAME: {"name", "full name", "fullname", "contact name", "person name", "contact", "prospect name"},
    Target.TITLE: {"title", "job title", "position", "designation", "role", "job role", "person title"},
    Target.COMPANY_NAME: {"company", "company name", "organization", "organisation", "organization name",
                          "organisation name", "account name", "account", "employer", "hospital name",
                          "institution", "institution name", "business name"},
    Target.COMPANY_DOMAIN: {"domain", "company domain", "website domain", "organization domain"},
    Target.COMPANY_WEBSITE: {"website", "company website", "web site", "company url", "homepage", "url",
                             "organization website", "website url"},
    Target.LINKEDIN_URL: {"linkedin", "linkedin url", "person linkedin url", "linkedin profile",
                          "linkedin profile url", "person linkedin", "personal linkedin"},
    Target.INDUSTRY: {"industry", "industries", "sector", "vertical"},
    Target.COUNTRY: {"country", "country region", "region", "country name"},
    Target.LOCATION: {"location", "city", "address", "person location", "geography"},
    Target.SEGMENT: {"segment", "campaign", "list", "list name", "segment name", "campaign name"},
}
_ALIAS_LOOKUP = {alias: target for target, aliases in _ALIASES.items() for alias in aliases}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_header(header: str) -> str:
    return _NON_ALNUM.sub(" ", header.casefold()).strip()


def attribute_key(header: str) -> str:
    """Stable snake_case key for a custom attribute derived from its header."""
    return (_NON_ALNUM.sub("_", header.casefold()).strip("_") or "attribute")[:64]


def suggest_mapping(headers: list[str]) -> dict[str, Target]:
    """Map recognized headers to fields (first match wins); everything else is ignored by default.

    Unmapped columns are never lost: every original cell value is stored with the import row.
    """
    mapping: dict[str, Target] = {}
    used: set[Target] = set()
    for header in headers:
        target = _ALIAS_LOOKUP.get(normalize_header(header))
        if target is not None and target not in used:
            mapping[header] = target
            used.add(target)
        else:
            mapping[header] = Target.IGNORE
    return mapping


class MappingError(ValueError):
    pass


def validate_mapping(headers: list[str], mapping: dict[str, str]) -> dict[str, Target]:
    unknown = set(mapping) - set(headers)
    if unknown:
        raise MappingError(f"Mapping references columns not in the file: {sorted(unknown)[:5]}")
    resolved: dict[str, Target] = {}
    for header in headers:
        value = mapping.get(header, Target.IGNORE.value)
        try:
            resolved[header] = Target(value)
        except ValueError as exc:
            raise MappingError(f"Unknown target {value!r} for column {header!r}") from exc
    counts: dict[Target, list[str]] = {}
    for header, target in resolved.items():
        counts.setdefault(target, []).append(header)
    duplicated = {t.value: hs for t, hs in counts.items() if t in SINGLE_USE_TARGETS and len(hs) > 1}
    if duplicated:
        raise MappingError(f"Each field can be mapped from only one column: {duplicated}")
    attribute_keys = [attribute_key(h) for h, t in resolved.items() if t is Target.ATTRIBUTE]
    if len(attribute_keys) != len(set(attribute_keys)):
        raise MappingError("Two custom-attribute columns produce the same attribute name; rename one header.")
    if not any(t in {Target.EMAIL, Target.FULL_NAME, Target.FIRST_NAME} for t in resolved.values()):
        raise MappingError("Map at least one identifying column: work email, full name, or first name.")
    return resolved

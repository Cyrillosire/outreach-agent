"""Row-level validation and in-file duplicate detection (pure; no database access)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.states import ImportRowStatus
from app.services.imports.mapping import Target, attribute_key
from app.services.imports.normalize import (
    MAX_LENGTHS,
    Issue,
    Severity,
    build_full_name,
    clean_text,
    company_name_key,
    name_key,
    normalize_domain,
    normalize_email,
    normalize_linkedin,
    normalize_website,
)


@dataclass
class RowResult:
    row_number: int
    raw: dict[str, str]
    normalized: dict[str, Any] | None
    issues: list[Issue] = field(default_factory=list)
    status: ImportRowStatus = ImportRowStatus.VALID
    duplicate_of_row: int | None = None

    @property
    def has_errors(self) -> bool:
        return any(i.severity is Severity.ERROR for i in self.issues)


def _value(raw: dict[str, str], mapping: dict[str, Target], target: Target) -> str | None:
    for header, mapped in mapping.items():
        if mapped is target:
            return raw.get(header)
    return None


def validate_row(row_number: int, raw: dict[str, str], mapping: dict[str, Target], *,
                 ragged: bool = False) -> RowResult:
    issues: list[Issue] = []

    def text(target: Target) -> str | None:
        return clean_text(_value(raw, mapping, target), MAX_LENGTHS.get(target.value))

    email = normalize_email(_value(raw, mapping, Target.EMAIL))
    issues.extend(email.issues)

    first, last, full = text(Target.FIRST_NAME), text(Target.LAST_NAME), text(Target.FULL_NAME)
    full_name = build_full_name(first, last, full)
    company_name = text(Target.COMPANY_NAME)

    website, website_issue = normalize_website(_value(raw, mapping, Target.COMPANY_WEBSITE))
    if website_issue:
        issues.append(website_issue)
    linkedin, linkedin_issue = normalize_linkedin(_value(raw, mapping, Target.LINKEDIN_URL))
    if linkedin_issue:
        issues.append(linkedin_issue)

    domain, domain_source = None, None
    explicit_domain = clean_text(_value(raw, mapping, Target.COMPANY_DOMAIN))
    if explicit_domain:
        domain = normalize_domain(explicit_domain)
        domain_source = "company_domain" if domain else None
        if domain is None:
            issues.append(Issue("company_domain_invalid", "company_domain",
                                f"Not a valid domain: {explicit_domain[:100]}; value ignored."))
    if domain is None and website:
        domain, domain_source = normalize_domain(website), "company_website"
    if domain is None and email.is_valid and not any(i.code == "email_freemail" for i in email.issues):
        domain, domain_source = email.domain, "email"

    attributes = {
        attribute_key(header): cleaned
        for header, target in mapping.items()
        if target is Target.ATTRIBUTE and (cleaned := clean_text(raw.get(header), 2000)) is not None
    }

    if email.original and not email.is_valid:
        pass  # the email error is already recorded
    elif not email.is_valid and not full_name:
        issues.append(Issue("no_person", "row",
                            "No person identified (needs a work email or a name). Company-only rows can't be "
                            "imported as prospects.", Severity.ERROR))
    elif not email.is_valid and not (company_name or domain):
        issues.append(Issue("insufficient_identity", "row",
                            "A name without an email or company can't be identified or researched reliably.",
                            Severity.ERROR))
    elif not email.is_valid:
        issues.append(Issue("no_email", "email",
                            "No email address: this prospect can be researched but not emailed until one is added."))
    if not full_name and email.is_valid:
        issues.append(Issue("no_name", "full_name", "No name supplied; personalization will be limited."))
    if not (company_name or domain):
        issues.append(Issue("no_company", "company_name", "No company identified; company research is not possible."))
    if ragged:
        issues.append(Issue("extra_cells", "row", "Row has more cells than the header; extra cells were ignored."))

    result = RowResult(row_number=row_number, raw=raw, normalized=None, issues=issues)
    if result.has_errors:
        result.status = ImportRowStatus.INVALID
        return result

    result.normalized = {
        "first_name": first,
        "last_name": last,
        "full_name": full_name,
        "email": email.original if email.is_valid else None,
        "email_normalized": email.normalized,
        "title": text(Target.TITLE),
        "company_name": company_name,
        "company_domain": domain,
        "domain_source": domain_source,
        "company_website": website,
        "linkedin_url": linkedin,
        "industry": text(Target.INDUSTRY),
        "country": text(Target.COUNTRY),
        "location": text(Target.LOCATION),
        "segment": text(Target.SEGMENT),
        "attributes": attributes,
    }
    return result


def validate_rows(rows: list[tuple[int, dict[str, str]]], mapping: dict[str, Target],
                  ragged_rows: set[int] | None = None) -> list[RowResult]:
    """Validate every row and mark later occurrences of the same email as in-file duplicates."""
    ragged_rows = ragged_rows or set()
    results = [validate_row(n, raw, mapping, ragged=n in ragged_rows) for n, raw in rows]
    first_seen: dict[str, int] = {}
    for result in results:
        if result.status is not ImportRowStatus.VALID or result.normalized is None:
            continue
        key = result.normalized["email_normalized"]
        if key is None:
            continue
        if key in first_seen:
            result.status = ImportRowStatus.DUPLICATE_IN_FILE
            result.duplicate_of_row = first_seen[key]
            result.issues.append(Issue("duplicate_in_file", "email",
                                       f"Same email as row {first_seen[key]}; only the first occurrence is imported."))
        else:
            first_seen[key] = result.row_number

    # Same full name at the same company is a duplicate contact (user-specified rule): only the first row is kept.
    seen_people: list[tuple[PersonIdentity, int]] = []
    for result in results:
        if result.status is not ImportRowStatus.VALID or result.normalized is None:
            continue
        ident = person_identity(result.normalized)
        if ident is None:
            continue
        match = next((row for other, row in seen_people if ident.same_person(other)), None)
        if match is not None:
            result.status = ImportRowStatus.DUPLICATE_IN_FILE
            result.duplicate_of_row = match
            result.issues.append(Issue("duplicate_name_in_file", "full_name",
                                       f"Same name and company as row {match}; only the first occurrence is imported."))
        else:
            seen_people.append((ident, result.row_number))
    return results


@dataclass(frozen=True)
class PersonIdentity:
    """Name + company identity. Same person = exact same name (case/space-insensitive) at the same company, where
    "same company" means the exact same company name, or the same domain when both are known."""

    name: str
    company_name: str | None
    domain: str | None

    def same_person(self, other: PersonIdentity) -> bool:
        if self.name != other.name:
            return False
        if self.company_name and other.company_name and self.company_name == other.company_name:
            return True
        return bool(self.domain and other.domain and self.domain == other.domain)


def person_identity(normalized: dict[str, Any]) -> PersonIdentity | None:
    name = name_key(normalized.get("full_name"))
    company = company_name_key(normalized.get("company_name"))
    domain = normalized.get("company_domain")
    if not name or not (company or domain):
        return None
    return PersonIdentity(name, company, domain)

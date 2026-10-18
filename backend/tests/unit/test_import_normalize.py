import pytest

from app.services.imports.normalize import (
    Severity,
    build_full_name,
    clean_text,
    normalize_domain,
    normalize_email,
    normalize_linkedin,
    normalize_website,
)


@pytest.mark.parametrize("value,expected", [
    ("  Jane   Doe ", "Jane Doe"),
    ("N/A", None), ("-", None), ("not found", None), ("NULL", None), ("", None), (None, None),
    ("Head\tof\nIT", "Head of IT"),
    ("Zoë​", "Zoë"),  # zero-width space stripped
])
def test_clean_text(value, expected) -> None:  # type: ignore[no-untyped-def]
    assert clean_text(value) == expected


def test_clean_text_truncates() -> None:
    assert clean_text("x" * 500, 10) == "x" * 10


@pytest.mark.parametrize("raw,normalized,domain", [
    ("Jane.Doe@Acme.COM", "jane.doe@acme.com", "acme.com"),
    ("  <jane@acme.com> ", "jane@acme.com", "acme.com"),
    ("mailto:jane@acme.com", "jane@acme.com", "acme.com"),
    ("jane+outreach@acme.com", "jane+outreach@acme.com", "acme.com"),  # tags are NOT stripped
])
def test_valid_emails(raw: str, normalized: str, domain: str) -> None:
    r = normalize_email(raw)
    assert r.is_valid and r.normalized == normalized and r.domain == domain


@pytest.mark.parametrize("raw,code", [
    ("not-an-email", "email_invalid"),
    ("jane@", "email_invalid"),
    ("a@b.com; c@d.com", "email_multiple"),
    ("a@b.com, c@d.com", "email_multiple"),
])
def test_invalid_emails_are_errors(raw: str, code: str) -> None:
    r = normalize_email(raw)
    assert not r.is_valid
    assert r.issues[0].code == code and r.issues[0].severity is Severity.ERROR


def test_placeholder_email_is_empty_not_invalid() -> None:
    r = normalize_email("N/A")
    assert r.original is None and not r.is_valid and r.issues == ()


def test_freemail_and_role_accounts_are_warnings() -> None:
    r = normalize_email("info@gmail.com")
    assert r.is_valid
    assert {i.code for i in r.issues} == {"email_freemail", "email_role_account"}
    assert all(i.severity is Severity.WARNING for i in r.issues)


@pytest.mark.parametrize("raw,expected", [
    ("Acme.com", "acme.com"),
    ("https://www.Acme.com/about?x=1", "acme.com"),
    ("acme.com:443", "acme.com"),
    ("www.acme.co.uk.", "acme.co.uk"),
    ("uk.acme.com", "uk.acme.com"),  # sub-domains are kept distinct
    ("münchen.de", "xn--mnchen-3ya.de"),
    ("192.168.1.10", None), ("localhost", None), ("printer.local", None), ("acme", None),
    ("not a domain", None), ("-bad-.com", None), ("N/A", None),
])
def test_normalize_domain(raw: str, expected: str | None) -> None:
    assert normalize_domain(raw) == expected


def test_normalize_website() -> None:
    assert normalize_website("www.acme.com/contact") == ("https://acme.com", None)
    assert normalize_website("http://acme.com")[0] == "http://acme.com"
    url, issue = normalize_website("ftp://acme.com")
    assert url is None and issue is not None
    assert normalize_website("garbage value")[1].code == "website_invalid"  # type: ignore[union-attr]


@pytest.mark.parametrize("raw,expected", [
    ("https://www.linkedin.com/in/jane-doe-123/", "https://www.linkedin.com/in/jane-doe-123"),
    ("linkedin.com/in/jane", "https://www.linkedin.com/in/jane"),
    ("http://in.linkedin.com/in/jane?trk=x", "https://www.linkedin.com/in/jane"),
])
def test_linkedin_profiles(raw: str, expected: str) -> None:
    assert normalize_linkedin(raw) == (expected, None)


@pytest.mark.parametrize("raw,code", [
    ("https://www.linkedin.com/company/acme", "linkedin_not_profile"),
    ("https://evil.example/in/jane", "linkedin_invalid"),
    ("https://linkedin.com.evil.example/in/jane", "linkedin_invalid"),
])
def test_linkedin_rejections(raw: str, code: str) -> None:
    url, issue = normalize_linkedin(raw)
    assert url is None and issue is not None and issue.code == code


def test_full_name_is_never_split_or_guessed() -> None:
    assert build_full_name("Jane", "Doe", None) == "Jane Doe"
    assert build_full_name(None, None, "Dr. A. K. Verma") == "Dr. A. K. Verma"
    assert build_full_name("Jane", None, None) == "Jane"
    assert build_full_name(None, None, None) is None

from app.domain.states import ImportRowStatus
from app.services.imports.mapping import Target
from app.services.imports.validation import validate_row, validate_rows

M = {"Name": Target.FULL_NAME, "Email": Target.EMAIL, "Company": Target.COMPANY_NAME,
     "Domain": Target.COMPANY_DOMAIN, "Website": Target.COMPANY_WEBSITE, "LinkedIn": Target.LINKEDIN_URL,
     "Tier": Target.ATTRIBUTE, "Segment": Target.SEGMENT, "Junk": Target.IGNORE}


def row(**values: str) -> dict[str, str]:
    base = {k: "" for k in M}
    base.update(values)
    return base


def codes(result) -> set[str]:  # type: ignore[no-untyped-def]
    return {i.code for i in result.issues}


def test_complete_row_normalizes_everything() -> None:
    r = validate_row(2, row(Name=" Jane  Doe ", Email="Jane@Acme.com", Company="Acme", Website="www.acme.com",
                            LinkedIn="linkedin.com/in/jane", Tier="A", Segment="Hospitals", Junk="zzz"), M)
    assert r.status is ImportRowStatus.VALID and r.issues == []
    n = r.normalized
    assert n is not None
    assert n["full_name"] == "Jane Doe" and n["email_normalized"] == "jane@acme.com"
    assert n["company_domain"] == "acme.com" and n["domain_source"] == "company_website"
    assert n["linkedin_url"] == "https://www.linkedin.com/in/jane"
    assert n["attributes"] == {"tier": "A"} and n["segment"] == "Hospitals"
    assert "junk" not in n["attributes"]


def test_domain_precedence_explicit_then_website_then_work_email() -> None:
    explicit = validate_row(2, row(Email="j@mail.acme.com", Domain="acme.com", Website="other.com"), M)
    assert explicit.normalized["company_domain"] == "acme.com"  # type: ignore[index]
    from_email = validate_row(2, row(Name="J", Email="j@acme.com"), M)
    assert from_email.normalized["company_domain"] == "acme.com"  # type: ignore[index]
    assert from_email.normalized["domain_source"] == "email"  # type: ignore[index]


def test_freemail_never_becomes_company_domain() -> None:
    r = validate_row(2, row(Name="J", Email="j@gmail.com"), M)
    assert r.normalized["company_domain"] is None  # type: ignore[index]
    assert {"email_freemail", "no_company"} <= codes(r)


def test_company_only_row_is_rejected_with_explanation() -> None:
    r = validate_row(2, row(Company="Apollo Hospitals", Website="apollohospitals.com"), M)
    assert r.status is ImportRowStatus.INVALID and "no_person" in codes(r)
    assert r.normalized is None


def test_name_without_email_needs_a_company() -> None:
    assert "insufficient_identity" in codes(validate_row(2, row(Name="Jane Doe"), M))
    ok = validate_row(2, row(Name="Jane Doe", Company="Acme"), M)
    assert ok.status is ImportRowStatus.VALID and "no_email" in codes(ok)


def test_invalid_email_rejects_row_rather_than_silently_dropping_email() -> None:
    r = validate_row(2, row(Name="Jane", Email="jane(at)acme", Company="Acme"), M)
    assert r.status is ImportRowStatus.INVALID and "email_invalid" in codes(r)


def test_bad_optional_values_are_warnings_and_dropped() -> None:
    r = validate_row(2, row(Email="j@acme.com", Name="J", LinkedIn="https://linkedin.com/company/acme",
                            Domain="not a domain"), M)
    assert r.status is ImportRowStatus.VALID
    assert {"linkedin_not_profile", "company_domain_invalid"} <= codes(r)
    assert r.normalized["linkedin_url"] is None  # type: ignore[index]
    assert r.normalized["company_domain"] == "acme.com"  # type: ignore[index]  (falls back to email domain)


def test_in_file_duplicates_by_email_keep_first() -> None:
    results = validate_rows([(2, row(Email="a@acme.com", Name="A")), (3, row(Email="A@ACME.com", Name="A2")),
                             (4, row(Email="b@acme.com", Name="B"))], M)
    assert [r.status for r in results] == [ImportRowStatus.VALID, ImportRowStatus.DUPLICATE_IN_FILE,
                                          ImportRowStatus.VALID]
    assert results[1].duplicate_of_row == 2


def test_same_name_same_company_is_flagged_not_merged() -> None:
    results = validate_rows([(2, row(Name="Jane Doe", Company="Acme")), (3, row(Name="jane  doe", Company="ACME"))], M)
    assert all(r.status is ImportRowStatus.VALID for r in results)
    assert "possible_duplicate" in codes(results[1]) and "possible_duplicate" not in codes(results[0])


def test_same_name_different_company_is_not_flagged() -> None:
    results = validate_rows([(2, row(Name="Jane Doe", Company="Acme")), (3, row(Name="Jane Doe", Company="Beta"))], M)
    assert all("possible_duplicate" not in codes(r) for r in results)


def test_ragged_row_warning() -> None:
    results = validate_rows([(2, row(Email="a@acme.com"))], M, ragged_rows={2})
    assert "extra_cells" in codes(results[0])

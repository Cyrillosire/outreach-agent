import pytest

from app.services.imports.csv_parser import CsvError, CsvLimits, parse_csv
from app.services.imports.mapping import MappingError, Target, attribute_key, suggest_mapping, validate_mapping

LIMITS = CsvLimits(max_bytes=1024 * 1024, max_rows=100, max_columns=20)


def test_parses_utf8_with_bom_and_records_row_numbers() -> None:
    data = "﻿Name,Email\nJane,jane@acme.com\n\nJohn,john@acme.com\n".encode()
    parsed = parse_csv(data, LIMITS)
    assert parsed.headers == ["Name", "Email"]
    assert parsed.encoding == "utf-8"
    assert [n for n, _ in parsed.rows] == [2, 4]  # spreadsheet row numbers; blank row skipped
    assert parsed.blank_rows_skipped == 1
    assert parsed.rows[0][1] == {"Name": "Jane", "Email": "jane@acme.com"}


def test_windows_1252_fallback() -> None:
    parsed = parse_csv("Name,Email\nJosé,jose@acme.com\n".encode("cp1252"), LIMITS)
    assert parsed.encoding == "windows-1252"
    assert parsed.rows[0][1]["Name"] == "José"


@pytest.mark.parametrize("delimiter", [";", "\t", "|"])
def test_delimiter_detection(delimiter: str) -> None:
    text = f"Name{delimiter}Email{delimiter}Title\nJane{delimiter}jane@acme.com{delimiter}CTO\n"
    parsed = parse_csv(text.encode(), LIMITS)
    assert parsed.delimiter == delimiter and parsed.rows[0][1]["Title"] == "CTO"


def test_quoted_commas_and_newlines() -> None:
    parsed = parse_csv(b'Name,Notes\n"Doe, Jane","line one\nline two"\n', LIMITS)
    assert parsed.rows[0][1] == {"Name": "Doe, Jane", "Notes": "line one\nline two"}


def test_duplicate_and_blank_headers_are_disambiguated() -> None:
    parsed = parse_csv(b"Email,email,,Name\na@x.com,b@x.com,z,Jane\n", LIMITS)
    assert parsed.headers == ["Email", "email (2)", "Column 3", "Name"]


def test_ragged_rows_padded_and_flagged() -> None:
    parsed = parse_csv(b"A,B\n1\n2,3,EXTRA\n", LIMITS)
    assert parsed.rows[0][1] == {"A": "1", "B": ""}
    assert parsed.ragged_rows == [3]


def test_formula_like_values_are_kept_as_plain_data() -> None:
    parsed = parse_csv(b"Name,Note\n=HYPERLINK(\"x\"),+1\n", LIMITS)
    assert parsed.rows[0][1]["Name"] == '=HYPERLINK("x")'


@pytest.mark.parametrize("data,message", [
    (b"", "empty"),
    (b"Name,Email\n", "no data rows"),
    (b"\x00\x01\x02binary", "binary"),
    (b"x" * (LIMITS.max_bytes + 1), "larger than"),
    (("," * 30 + "\n1\n").encode(), "columns"),
    (("Name\n" + "a\n" * 101).encode(), "more than 100"),
], ids=["empty", "header-only", "binary", "too-large", "too-many-columns", "too-many-rows"])
def test_rejections(data: bytes, message: str) -> None:
    with pytest.raises(CsvError, match=message):
        parse_csv(data, LIMITS)


def test_sha256_recorded() -> None:
    assert len(parse_csv(b"Name\nJane\n", LIMITS).sha256) == 64


# --- Mapping -------------------------------------------------------------------------------------

def test_suggest_mapping_for_hims_style_export() -> None:
    headers = ["Assigned To", "Full Name", "Job Title", "Company Name", "Individual Email",
               "Other Emails (Company / General)", "Location", "Company LinkedIn / Person LinkedIn", "Industries"]
    m = suggest_mapping(headers)
    assert m["Full Name"] is Target.FULL_NAME
    assert m["Job Title"] is Target.TITLE
    assert m["Company Name"] is Target.COMPANY_NAME
    assert m["Individual Email"] is Target.EMAIL
    assert m["Location"] is Target.LOCATION
    assert m["Industries"] is Target.INDUSTRY
    # Ambiguous columns are never guessed.
    assert m["Other Emails (Company / General)"] is Target.IGNORE
    assert m["Company LinkedIn / Person LinkedIn"] is Target.IGNORE
    assert m["Assigned To"] is Target.IGNORE


def test_suggest_mapping_ignores_scraped_css_headers() -> None:
    m = suggest_mapping(["zp_rZciu href", "label___30pdd", "text-body-sm-regular"])
    assert set(m.values()) == {Target.IGNORE}


def test_suggest_mapping_uses_first_match_only() -> None:
    m = suggest_mapping(["Email", "Work Email"])
    assert m == {"Email": Target.EMAIL, "Work Email": Target.IGNORE}


def test_validate_mapping_rejects_duplicates_unknown_and_missing_identity() -> None:
    headers = ["A", "B", "C"]
    with pytest.raises(MappingError, match="only one column"):
        validate_mapping(headers, {"A": "email", "B": "email"})
    with pytest.raises(MappingError, match="not in the file"):
        validate_mapping(headers, {"Z": "email"})
    with pytest.raises(MappingError, match="Unknown target"):
        validate_mapping(headers, {"A": "salary"})
    with pytest.raises(MappingError, match="identifying column"):
        validate_mapping(headers, {"A": "title"})


def test_validate_mapping_allows_many_attributes_and_defaults_to_ignore() -> None:
    resolved = validate_mapping(["Email", "City", "Tier", "Junk"], {"Email": "email", "City": "attribute",
                                                                    "Tier": "attribute"})
    assert resolved["Junk"] is Target.IGNORE
    with pytest.raises(MappingError, match="same attribute name"):
        validate_mapping(["Email", "Tier!", "tier"], {"Email": "email", "Tier!": "attribute", "tier": "attribute"})


def test_attribute_key() -> None:
    assert attribute_key("Company Employee Count (Apollo)") == "company_employee_count_apollo"

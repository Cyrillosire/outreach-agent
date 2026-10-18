"""Bounded, defensive CSV parsing.

Handles BOM, UTF-8 vs Windows-1252 exports, delimiter detection (, ; tab |), duplicate/blank headers,
ragged rows, and size limits. Cell values are kept verbatim as strings; nothing is evaluated (values
starting with "=" are data, not formulas).
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field

_DELIMITERS = [",", ";", "\t", "|"]
MAX_CELL_CHARS = 10_000


class CsvError(ValueError):
    """User-facing parse failure (message is safe to show)."""


@dataclass(frozen=True)
class CsvLimits:
    max_bytes: int
    max_rows: int
    max_columns: int


@dataclass
class ParsedCsv:
    headers: list[str]
    # (row_number, {header: value}); row_number is spreadsheet-style (header = 1).
    rows: list[tuple[int, dict[str, str]]]
    encoding: str
    delimiter: str
    sha256: str
    size_bytes: int
    blank_rows_skipped: int = 0
    ragged_rows: list[int] = field(default_factory=list)


def _decode(data: bytes) -> tuple[str, str]:
    if b"\x00" in data[:4096]:
        raise CsvError("The file looks binary (or UTF-16). Export it as CSV (UTF-8) and try again.")
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "windows-1252"
    except UnicodeDecodeError as exc:
        raise CsvError("Could not detect the file's text encoding. Save it as CSV UTF-8.") from exc


def _detect_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITERS)).delimiter
    except csv.Error:
        # Fall back to the delimiter that appears most in the header line.
        header = text.splitlines()[0] if text else ""
        return max(_DELIMITERS, key=header.count)


def _dedupe_headers(raw: list[str]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(raw, start=1):
        name = " ".join(value.split()) or f"Column {index}"
        key = name.casefold()
        if key in seen:
            seen[key] += 1
            name = f"{name} ({seen[key]})"
        else:
            seen[key] = 1
        headers.append(name)
    return headers


def parse_csv(data: bytes, limits: CsvLimits) -> ParsedCsv:
    if len(data) == 0:
        raise CsvError("The file is empty.")
    if len(data) > limits.max_bytes:
        raise CsvError(f"The file is larger than {limits.max_bytes // (1024 * 1024)} MB.")
    text, encoding = _decode(data)
    if not text.strip():
        raise CsvError("The file is empty.")
    delimiter = _detect_delimiter(text)

    csv.field_size_limit(MAX_CELL_CHARS * 2)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header_row = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise CsvError("Could not read a header row.") from exc
    if len(header_row) > limits.max_columns:
        raise CsvError(f"The file has {len(header_row)} columns; the maximum is {limits.max_columns}.")
    headers = _dedupe_headers(header_row)
    if len(headers) < 2 and not any(headers):
        raise CsvError("Could not find column headers in the first row.")

    parsed = ParsedCsv(headers=headers, rows=[], encoding=encoding, delimiter=delimiter,
                       sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    try:
        for record_index, values in enumerate(reader, start=2):
            if not any(v.strip() for v in values):
                parsed.blank_rows_skipped += 1
                continue
            if len(parsed.rows) >= limits.max_rows:
                raise CsvError(f"The file has more than {limits.max_rows} data rows. Split it into smaller files.")
            if len(values) > len(headers) and any(v.strip() for v in values[len(headers):]):
                parsed.ragged_rows.append(record_index)
            padded = (values + [""] * len(headers))[: len(headers)]
            parsed.rows.append((record_index, {h: v[:MAX_CELL_CHARS] for h, v in zip(headers, padded, strict=True)}))
    except csv.Error as exc:
        raise CsvError(f"The file is not valid CSV near row {reader.line_num}: {exc}") from exc
    if not parsed.rows:
        raise CsvError("The file has a header row but no data rows.")
    return parsed

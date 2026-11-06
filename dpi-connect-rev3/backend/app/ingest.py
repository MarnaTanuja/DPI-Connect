"""
Turn an uploaded file or pasted text into a list of record dicts.

Supported:
  * JSON      - array of objects, a single object, or {"records"|"data"|"items"|"rows": [...]}
  * JSONL     - one JSON object per line
  * CSV / TSV - delimiter sniffed (comma, semicolon, tab, pipe), header row required
  * XLSX      - first worksheet (or the one named by `sheet`), header row required

Format is auto-detected from the filename and then the content, unless
`fmt` is given. Cell text is lightly typed: clean integers/floats become
numbers, everything else stays text (so ids like "00123" or phone numbers
keep their leading zeros), empty cells become null.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from typing import Any, Optional

MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_COLUMNS = 200
PREVIEW_ROWS = 8

_DELIMS = ",;\t|"
_LIST_KEYS = ("records", "data", "items", "rows", "results")


class IngestError(ValueError):
    """User-facing parse problem (becomes an HTTP 400)."""


def _coerce_cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    if s == "":
        return None
    if re.fullmatch(r"-?(0|[1-9]\d{0,14})", s):
        return int(s)
    if re.fullmatch(r"-?(0|[1-9]\d{0,14})\.\d{1,10}", s):
        return float(s)
    return s


def _clean_headers(raw: list[Any]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, h in enumerate(raw, start=1):
        name = str(h).strip() if h is not None else ""
        name = re.sub(r"\s+", "_", name) or f"column_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def _rows_to_records(rows: list[list[Any]], warnings: list[str]) -> list[dict[str, Any]]:
    rows = [r for r in rows if any(c is not None and str(c).strip() != "" for c in r)]
    if len(rows) < 2:
        raise IngestError("Need a header row plus at least one data row.")
    headers = _clean_headers(rows[0])
    if len(headers) > MAX_COLUMNS:
        raise IngestError(f"Too many columns ({len(headers)}); the limit is {MAX_COLUMNS}.")
    if len(rows) - 1 > MAX_RECORDS:
        raise IngestError(f"Too many rows ({len(rows) - 1}); the limit is {MAX_RECORDS}.")
    if all(re.fullmatch(r"-?\d+(\.\d+)?", h) for h in headers):
        warnings.append("Every header looks like a number - is the first row really a header?")
    ragged = 0
    records = []
    for r in rows[1:]:
        if len(r) != len(headers):
            ragged += 1
        r = list(r) + [None] * (len(headers) - len(r))
        records.append({h: _coerce_cell(v) for h, v in zip(headers, r)})
    if ragged:
        warnings.append(f"{ragged} row(s) had a different number of cells than the header; short rows were padded, extra cells dropped.")
    return records


def _parse_delimited(text: str, warnings: list[str], forced: Optional[str]) -> list[dict[str, Any]]:
    sample = text[:4096]
    if forced == "tsv":
        delim = "\t"
    elif forced == "csv":
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=_DELIMS).delimiter
        except csv.Error:
            delim = ","
    else:
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=_DELIMS).delimiter
        except csv.Error:
            first = sample.splitlines()[0] if sample.splitlines() else ""
            delim = max(_DELIMS, key=first.count) if any(first.count(d) for d in _DELIMS) else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    return _rows_to_records(rows, warnings)


def _parse_json(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise IngestError(f"Invalid JSON: {e.msg} (line {e.lineno}, column {e.colno}).")
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list) or not data:
        raise IngestError("JSON must be a non-empty array of objects.")
    if not all(isinstance(r, dict) for r in data):
        raise IngestError("Every item in the JSON array must be an object ({...}).")
    return data


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise IngestError(f"Invalid JSON on line {n}: {e.msg}.")
        if not isinstance(obj, dict):
            raise IngestError(f"Line {n} is not a JSON object.")
        out.append(obj)
    if not out:
        raise IngestError("No records found.")
    return out


def _parse_xlsx(data: bytes, sheet: Optional[str], warnings: list[str]) -> list[dict[str, Any]]:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # corrupt / not an xlsx
        raise IngestError(f"Could not read that Excel file: {e}")
    if sheet:
        if sheet not in wb.sheetnames:
            raise IngestError(f"Sheet '{sheet}' not found. Available: {', '.join(wb.sheetnames)}")
        ws = wb[sheet]
    else:
        ws = wb[wb.sheetnames[0]]
        if len(wb.sheetnames) > 1:
            warnings.append(f"Workbook has {len(wb.sheetnames)} sheets; used '{wb.sheetnames[0]}'.")
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    return _rows_to_records(rows, warnings)


def detect_format(filename: Optional[str], text: Optional[str], data: Optional[bytes]) -> str:
    name = (filename or "").lower()
    for ext, fmt in ((".xlsx", "xlsx"), (".xlsm", "xlsx"), (".jsonl", "jsonl"), (".ndjson", "jsonl"),
                     (".json", "json"), (".tsv", "tsv"), (".tab", "tsv"), (".csv", "csv"), (".txt", "auto")):
        if name.endswith(ext) and fmt != "auto":
            return fmt
    if data is not None and data[:2] == b"PK":
        return "xlsx"
    body = (text or "").lstrip()
    if body.startswith("[") or (body.startswith("{") and "\n{" not in body.strip()):
        return "json"
    if body.startswith("{"):
        return "jsonl"
    return "csv"


def parse_dataset(
    *, filename: Optional[str] = None, data: Optional[bytes] = None,
    text: Optional[str] = None, fmt: str = "auto", sheet: Optional[str] = None,
) -> dict[str, Any]:
    """Returns {format, records, warnings}."""
    if data is None and text is None:
        raise IngestError("Provide a file or paste some data.")
    raw_len = len(data) if data is not None else len((text or "").encode("utf-8"))
    if raw_len > MAX_BYTES:
        raise IngestError(f"That is {raw_len / 1_048_576:.1f} MB; the limit is {MAX_BYTES // 1_048_576} MB.")
    if raw_len == 0 or (text is not None and not text.strip()):
        raise IngestError("The data is empty.")

    fmt = (fmt or "auto").lower()
    if fmt not in ("auto", "csv", "tsv", "json", "jsonl", "xlsx"):
        raise IngestError(f"Unknown format '{fmt}'.")

    if text is None and data is not None and not (fmt == "xlsx" or data[:2] == b"PK"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    if text is not None:
        text = text.lstrip("\ufeff")

    if fmt == "auto":
        fmt = detect_format(filename, text, data)

    warnings: list[str] = []
    if fmt == "xlsx":
        if data is None:
            raise IngestError("Excel files must be uploaded as a file, not pasted.")
        records = _parse_xlsx(data, sheet, warnings)
    elif fmt == "json":
        records = _parse_json(text or "")
    elif fmt == "jsonl":
        records = _parse_jsonl(text or "")
    else:
        records = _parse_delimited(text or "", warnings, forced=fmt)

    if len(records) > MAX_RECORDS:
        raise IngestError(f"Too many records ({len(records)}); the limit is {MAX_RECORDS}.")
    if not any(records):
        raise IngestError("No usable records were found.")
    return {"format": fmt, "records": records, "warnings": warnings}

"""File upload + copy/paste ingestion."""
import io
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import ingest
from app.main import app

client = TestClient(app)


def _records(text, **kw):
    return ingest.parse_dataset(text=text, **kw)["records"]


def test_csv_types_and_blanks():
    recs = _records("id,name,zip,score,note\n1,Ada,00123,9.5,\n2,Alan,560001,7,hi\n")
    assert recs[0] == {"id": 1, "name": "Ada", "zip": "00123", "score": 9.5, "note": None}   # zero-padded stays text
    assert recs[1]["zip"] == 560001                                                       # clean integers become numbers


@pytest.mark.parametrize("sep", [",", ";", "\t", "|"])
def test_delimiters_are_sniffed(sep):
    text = sep.join(["a", "b", "c"]) + "\n" + sep.join(["1", "x", "y"]) + "\n" + sep.join(["2", "p", "q"]) + "\n"
    assert _records(text) == [{"a": 1, "b": "x", "c": "y"}, {"a": 2, "b": "p", "c": "q"}]


def test_pasted_spreadsheet_rows_are_tab_separated():
    out = ingest.parse_dataset(text="citizen_id\tfull name\nC1\tAda Lovelace\nC2\tAlan Turing")
    assert out["format"] == "csv" and out["records"][0] == {"citizen_id": "C1", "full_name": "Ada Lovelace"}


def test_json_shapes():
    arr = '[{"a": 1}, {"a": 2}]'
    assert len(_records(arr)) == 2
    assert len(_records('{"records": [{"a": 1}, {"a": 2}, {"a": 3}]}')) == 3
    assert _records('{"a": 1, "b": {"c": 2}}') == [{"a": 1, "b": {"c": 2}}]
    jl = ingest.parse_dataset(text='{"a": 1}\n{"a": 2}\n')
    assert jl["format"] == "jsonl" and len(jl["records"]) == 2


def test_bom_duplicate_and_blank_headers():
    recs = ingest.parse_dataset(data="\ufeffid,name,name,\n1,a,b,c\n".encode("utf-8"), filename="x.csv")["records"]
    assert list(recs[0]) == ["id", "name", "name_2", "column_4"]


def test_xlsx(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["id", "dob", "amount"])
    import datetime
    ws.append([1, datetime.date(2000, 1, 5), 12.5])
    ws.append([2, datetime.date(1999, 2, 6), None])
    buf = io.BytesIO()
    wb.save(buf)
    out = ingest.parse_dataset(data=buf.getvalue(), filename="people.xlsx")
    assert out["format"] == "xlsx"
    assert out["records"][0] == {"id": 1, "dob": "2000-01-05T00:00:00", "amount": 12.5}
    assert out["records"][1]["amount"] is None


@pytest.mark.parametrize("text,needle", [
    ("", "empty"),
    ("[1, 2, 3]", "object"),
    ("{bad json", "Invalid JSON"),
    ("id,name\n", "header row plus"),
])
def test_friendly_errors(text, needle):
    with pytest.raises(ingest.IngestError, match=needle):
        ingest.parse_dataset(text=text)


def test_size_limit(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_BYTES", 50)
    with pytest.raises(ingest.IngestError, match="limit"):
        ingest.parse_dataset(text="a,b\n" + "1,2\n" * 100)


def test_excel_cannot_be_pasted():
    with pytest.raises(ingest.IngestError, match="uploaded as a file"):
        ingest.parse_dataset(text="a,b\n1,2", fmt="xlsx")


# ------------------------------------------------------------------ API
def _org(name):
    return client.post("/api/orgs/register", json={"name": name, "contact_email": "x@y.z"}).json()


def test_preview_then_import_pasted_csv_and_uploaded_file():
    org = _org("IngestOrgA")
    h = {"X-API-Key": org["api_key"]}
    csv_text = "citizen_id,full_name,dob\nC1,Ada Lovelace,1815-12-10\nC2,Alan Turing,1912-06-23\n"

    prev = client.post("/api/datasets/preview", headers=h, data={"text": csv_text})
    assert prev.status_code == 200, prev.text
    body = prev.json()
    assert body["record_count"] == 2 and body["format"] == "csv"
    kinds = {c["name"]: c["value_kind"] for c in body["columns"]}
    assert kinds["dob"] == "date" and kinds["citizen_id"] == "code"
    assert client.get("/api/datasets/mine", headers=h).json() == []        # preview saves nothing

    pasted = client.post("/api/datasets/import", headers=h, data={"name": "pasted", "text": csv_text})
    assert pasted.status_code == 200 and pasted.json()["record_count"] == 2

    up = client.post("/api/datasets/import", headers=h, data={"name": "uploaded"},
                     files={"file": ("people.json", json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]), "application/json")})
    assert up.status_code == 200 and up.json()["record_count"] == 3
    assert {d["name"] for d in client.get("/api/datasets/mine", headers=h).json()} == {"pasted", "uploaded"}


def test_import_errors_and_auth():
    org = _org("IngestOrgB")
    h = {"X-API-Key": org["api_key"]}
    assert client.post("/api/datasets/import", headers=h, data={"name": "x", "text": "{oops"}).status_code == 400
    assert client.post("/api/datasets/import", headers=h, data={"name": "  ", "text": "a,b\n1,2"}).status_code == 400
    assert client.post("/api/datasets/import", headers=h, data={"name": "x"}).status_code == 400
    assert client.post("/api/datasets/preview", data={"text": "a,b\n1,2"}).status_code in (401, 422)

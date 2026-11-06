"""
Automatic FULL OUTER JOIN: no join fields supplied by the caller, no
output_fields, no manual curl step — the key is detected from the data and
the combined dataset contains matched rows, A-only rows and B-only rows,
with A's and B's field names unioned.
"""
import os

os.environ["DPI_CONNECT_MONGO_MOCK"] = "1"
os.environ.pop("MISTRAL_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from tests.helpers import admin_decide  # noqa: E402

client = TestClient(app)


def _register(name):
    return client.post("/api/orgs/register", json={"name": name, "contact_email": f"{name}@x.com"}).json()


def _auth(k):
    return {"X-API-Key": k}


def _combined(records_a, records_b, suffix, **extra):
    a = _register(f"HealthOrg{suffix}")
    b = _register(f"TransportOrg{suffix}")
    ds_a = client.post("/api/datasets", headers=_auth(a["api_key"]),
                       json={"name": "healthcare_registry", "sample_records": records_a}).json()
    ds_b = client.post("/api/datasets", headers=_auth(b["api_key"]),
                       json={"name": "vehicle_registry", "sample_records": records_b}).json()
    conn = client.post("/api/connections", headers=_auth(a["api_key"]), json={
        "counterparty_org_id": b["org_id"],
        "requester_dataset_id": ds_a["id"],
        "counterparty_dataset_id": ds_b["id"],
        "purpose": "combined registry",
        **extra,
    })
    assert conn.status_code == 200, conn.text
    conn = conn.json()
    assert conn["mode"] == "joined"
    cid = conn["id"]
    assert admin_decide(client, cid).status_code == 200
    client.post(f"/api/connections/{cid}/consent", headers=_auth(b["api_key"]),
                json={"agreed": True, "signatory_name": "B Officer", "agreement_text": "ok"})
    res = client.post(f"/api/connections/{cid}/exchange", headers=_auth(a["api_key"]))
    assert res.status_code == 200, res.text
    return conn, res.json()


def test_auto_detected_full_outer_join():
    records_a = [
        {"citizen_id": "C1", "patient_name": "Ada", "blood_group": "O+"},
        {"citizen_id": "C2", "patient_name": "Alan", "blood_group": "A-"},
        {"citizen_id": "C3", "patient_name": "Grace", "blood_group": "B+"},
    ]
    records_b = [
        {"owner_id": "C2", "vehicle_no": "KA01AA1111", "vehicle_type": "car"},
        {"owner_id": "C4", "vehicle_no": "KA01BB2222", "vehicle_type": "bike"},
    ]
    conn, res = _combined(records_a, records_b, "1")

    assert conn["requester_join_field"] == "citizen_id"
    assert conn["counterparty_join_field"] == "owner_id"

    rows = res["combined_dataset"]
    # full outer: 2 A-only + 1 matched + 1 B-only == 4, never more than 3 + 2
    assert res["matched_count"] == 1
    assert res["unmatched_requester_count"] == 2
    assert res["unmatched_counterparty_count"] == 1
    assert len(rows) == 4 <= len(records_a) + len(records_b)

    # field names from A and B together
    cols = set().union(*(r.keys() for r in rows)) - {"_source_org"}
    assert {"citizen_id", "patient_name", "blood_group", "vehicle_no", "vehicle_type"} <= cols

    matched = [r for r in rows if "+" in r["_source_org"]][0]
    assert matched["patient_name"] == "Alan" and matched["vehicle_no"] == "KA01AA1111"
    b_only = [r for r in rows if r["_source_org"] == "TransportOrg1"][0]
    assert b_only["citizen_id"] == "C4" and b_only.get("patient_name") in (None, "")


def test_duplicate_keys_never_multiply_rows():
    """A repeated key on both sides must NOT produce a cartesian product."""
    records_a = [{"ref": "R1", "a_val": 1}, {"ref": "R1", "a_val": 2}]
    records_b = [{"ref": "R1", "b_val": 9}, {"ref": "R1", "b_val": 8}, {"ref": "R1", "b_val": 7}]
    _conn, res = _combined(records_a, records_b, "2",
                           requester_join_field="ref", counterparty_join_field="ref")
    rows = res["combined_dataset"]
    assert len(rows) <= len(records_a) + len(records_b)
    assert len(rows) == 3  # 2 pairs + 1 leftover B row, not 2 * 3


def test_no_shared_key_keeps_every_row_and_unions_columns():
    records_a = [{"hospital": "H1", "beds": 40}]
    records_b = [{"depot": "D9", "buses": 12}]
    conn, res = _combined(records_a, records_b, "3")
    assert conn["requester_join_field"] is None
    rows = res["combined_dataset"]
    assert len(rows) == 2
    cols = set().union(*(r.keys() for r in rows)) - {"_source_org"}
    assert {"hospital", "beds", "depot", "buses"} <= cols

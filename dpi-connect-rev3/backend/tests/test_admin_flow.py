"""Admin review gate: who can decide, what they see, and what each decision does."""
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import ADMIN_KEY, admin_decide, admin_headers

client = TestClient(app)

PEOPLE_A = (
    "citizen_id,full_name,dob,blood_group\n"
    "C1,Ada Lovelace,1815-12-10,O+\nC2,Alan Turing,1912-06-23,A-\nC3,Grace Hopper,1906-12-09,B+\n"
    "C4,Linus Torvalds,1969-12-28,AB+\nC5,Margaret Hamilton,1936-08-17,O-\nC6,Tim Berners-Lee,1955-06-08,A+\n"
)
PEOPLE_B = (
    "owner_id,owner_name,date_of_birth,vehicle_no\n"
    "C2,Alan Turing,23/06/1912,KA01AA1111\nC3,,09/12/1906,KA01BB2222\nC4,Linus Torvalds,28/12/1969,KA01CC3333\n"
    "C5,Margaret Hamilton,17/08/1936,KA01DD4444\nC6,Tim Berners-Lee,08/06/1955,KA01EE5555\nC9,Dennis Ritchie,09/09/1941,KA01FF6666\n"
)


def _org(name):
    return client.post("/api/orgs/register", json={"name": name, "contact_email": "o@x.io"}).json()


def _h(org):
    return {"X-API-Key": org["api_key"]}


def _request(suffix):
    a, b = _org(f"OrgA{suffix}"), _org(f"OrgB{suffix}")
    ds_a = client.post("/api/datasets/import", headers=_h(a), data={"name": "people", "text": PEOPLE_A}).json()
    ds_b = client.post("/api/datasets/import", headers=_h(b), data={"name": "owners", "text": PEOPLE_B}).json()
    conn = client.post("/api/connections", headers=_h(a), json={
        "counterparty_org_id": b["org_id"], "requester_dataset_id": ds_a["id"],
        "counterparty_dataset_id": ds_b["id"], "purpose": "link people and vehicles",
    })
    assert conn.status_code == 200, conn.text
    return a, b, conn.json()


def test_identity_and_permissions():
    org = _org("PermOrg")
    assert client.get("/api/auth/whoami", headers=admin_headers()).json()["role"] == "admin"
    assert client.get("/api/auth/whoami", headers=_h(org)).json() == {"role": "org", "id": org["org_id"], "name": "PermOrg"}
    assert client.get("/api/auth/whoami", headers={"X-API-Key": "nope"}).status_code == 401

    assert client.get("/api/admin/connections", headers=_h(org)).status_code == 403   # orgs are not admins
    assert client.get("/api/admin/connections", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/api/admin/connections").status_code == 422
    assert client.post("/api/connections/x/consent", headers=admin_headers(),
                       json={"agreed": True, "signatory_name": "a", "agreement_text": "b"}).status_code == 401


def test_new_request_waits_for_admin_and_blocks_consent():
    a, b, conn = _request("1")
    assert conn["status"] == "PENDING_ADMIN_REVIEW"
    assert 0 < conn["similarity_score"] < 1
    r = client.post(f"/api/connections/{conn['id']}/consent", headers=_h(b),
                    json={"agreed": True, "signatory_name": "B", "agreement_text": "ok"})
    assert r.status_code == 409 and "admin" in r.json()["detail"]
    assert client.post(f"/api/connections/{conn['id']}/exchange", headers=_h(a)).status_code == 409


def test_admin_sees_both_sides_mappings_and_scores_but_no_raw_data():
    _a, _b, conn = _request("2")
    queue = client.get("/api/admin/connections?status=PENDING_ADMIN_REVIEW", headers=admin_headers()).json()
    row = next(r for r in queue if r["id"] == conn["id"])
    assert row["scoring_summary"]["mean_match_confidence"] is not None

    rev = client.get(f"/api/admin/connections/{conn['id']}", headers=admin_headers())
    assert rev.status_code == 200, rev.text
    body = rev.json()

    cols = {c["output_field"]: c for c in body["reconciliation_report"]}
    assert cols["citizen_id"]["kind"] == "join_key" and cols["citizen_id"]["field_b"] == "owner_id"
    assert cols["full_name"]["field_b"] == "owner_name" and cols["dob"]["field_b"] == "date_of_birth"
    assert cols["blood_group"]["kind"] == "only_a" and cols["vehicle_no"]["kind"] == "only_b"
    assert all(0 < c["confidence"] < 1 for c in cols.values())
    assert all(c["evidence"] for c in cols.values())

    assert body["requester_dataset"]["org_name"] == "OrgA2" and body["counterparty_dataset"]["org_name"] == "OrgB2"
    assert {f["name"] for f in body["requester_dataset"]["fields"]} == {"citizen_id", "full_name", "dob", "blood_group"}
    assert {"Ada Lovelace", "KA01AA1111", "O+"}.isdisjoint(str(body))      # field metadata only - never the rows
    assert [e["event_type"] for e in body["audit"]] == ["connection_requested"]


def test_approve_then_consent_then_exchange_with_lineage():
    a, b, conn = _request("3")
    r = admin_decide(client, conn["id"], approve=True, notes="mappings look right")
    assert r.status_code == 200 and r.json()["status"] == "PENDING_CONSENT"
    assert r.json()["admin_review"]["decision"] == "approved" and r.json()["admin_review"]["notes"] == "mappings look right"
    assert admin_decide(client, conn["id"]).status_code == 409                 # already decided

    c = client.post(f"/api/connections/{conn['id']}/consent", headers=_h(b),
                    json={"agreed": True, "signatory_name": "B DPO", "agreement_text": "ok"})
    assert c.json()["status"] == "ACTIVE"

    res = client.post(f"/api/connections/{conn['id']}/exchange", headers=_h(a)).json()
    assert res["matched_count"] == 5 and res["unmatched_requester_count"] == 1 and res["unmatched_counterparty_count"] == 1
    assert res["combined_record_count"] == 7                                    # 6 + 6 - 5 matched
    lineage = {l["output_field"]: l for l in res["column_lineage"]}
    assert lineage["full_name"]["field_b"] == "owner_name" and lineage["full_name"]["confidence"] < 1

    rows = {r["citizen_id"]: r for r in res["combined_dataset"]}
    assert rows["C3"]["full_name"] == "Grace Hopper"          # B's name was blank, A's value kept
    assert rows["C3"]["vehicle_no"] == "KA01BB2222"
    assert rows["C1"]["blood_group"] == "O+" and rows["C1"].get("vehicle_no") in (None, "")   # A-only row kept
    assert rows["C9"]["vehicle_no"] == "KA01FF6666" and rows["C9"].get("blood_group") in (None, "")  # B-only row kept

    events = [e["event_type"] for e in client.get(f"/api/connections/{conn['id']}/audit", headers=_h(a)).json()]
    assert events == ["connection_requested", "admin_review_approved", "consent_approved",
                      "xroad_secure_handshake", "xroad_secure_exchange"]


def test_reject_closes_the_connection():
    a, b, conn = _request("4")
    r = admin_decide(client, conn["id"], approve=False, notes="confidence too low for the id column")
    assert r.json()["status"] == "REJECTED_BY_ADMIN"
    assert client.post(f"/api/connections/{conn['id']}/consent", headers=_h(b),
                       json={"agreed": True, "signatory_name": "B", "agreement_text": "ok"}).status_code == 409
    assert client.post(f"/api/connections/{conn['id']}/exchange", headers=_h(a)).status_code == 409
    # both parties can see why
    seen = client.get(f"/api/connections/{conn['id']}", headers=_h(a)).json()
    assert seen["admin_review"]["notes"].startswith("confidence too low")


def test_consent_step_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("REQUIRE_COUNTERPARTY_CONSENT", "0")
    a, _b, conn = _request("5")
    assert admin_decide(client, conn["id"]).json()["status"] == "ACTIVE"
    assert client.post(f"/api/connections/{conn['id']}/exchange", headers=_h(a)).status_code == 200


def test_decision_requires_a_reviewer_name():
    _a, _b, conn = _request("6")
    r = client.post(f"/api/admin/connections/{conn['id']}/decision", headers=admin_headers(),
                    json={"decision": "approve", "reviewer_name": "", "notes": ""})
    assert r.status_code == 422

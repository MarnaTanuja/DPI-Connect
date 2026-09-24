"""
End-to-end test of the whole workflow, using FastAPI's TestClient and an
in-memory `mongomock` database (set via env var before importing the
app) — no real MongoDB connection needed to run these.
"""
import os

os.environ["DPI_CONNECT_MONGO_MOCK"] = "1"
os.environ.pop("MISTRAL_API_KEY", None)  # force the fuzzy fallback for deterministic tests

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def _register(name: str) -> dict:
    r = client.post("/api/orgs/register", json={"name": name, "contact_email": f"{name}@example.com"})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(api_key: str) -> dict:
    return {"X-API-Key": api_key}


def test_full_workflow():
    # --- 1. Register two orgs ---
    org_a = _register("LandRegistryA")
    org_b = _register("TaxRegistryB")

    # --- 2. Both upload datasets ---
    ds_a = client.post(
        "/api/datasets",
        headers=_auth(org_a["api_key"]),
        json={"name": "persons", "sample_records": [
            {"first_name": "Ada", "last_name": "Lovelace", "tin": "AB123456"},
        ]},
    )
    assert ds_a.status_code == 200, ds_a.text
    ds_a_id = ds_a.json()["id"]

    ds_b = client.post(
        "/api/datasets",
        headers=_auth(org_b["api_key"]),
        json={"name": "taxpayers", "sample_records": [
            {"full_name": "Ada Lovelace", "national_id": "AB123456", "balance": 500},
            {"full_name": "Alan Turing", "national_id": "CD789012", "balance": 900},
        ]},
    )
    assert ds_b.status_code == 200, ds_b.text
    ds_b_id = ds_b.json()["id"]

    # --- 3. Org A can see B's schema (field names) but not B's raw records ---
    b_datasets = client.get(f"/api/orgs/{org_b['org_id']}/datasets")
    assert b_datasets.status_code == 200
    assert "sample_records" not in b_datasets.text  # never exposed pre-consent

    # --- 4. A requests a connection to B ---
    conn = client.post(
        "/api/connections",
        headers=_auth(org_a["api_key"]),
        json={
            "counterparty_org_id": org_b["org_id"],
            "requester_dataset_id": ds_a_id,
            "counterparty_dataset_id": ds_b_id,
            "purpose": "Enrich resident records with tax balance data",
        },
    )
    assert conn.status_code == 200, conn.text
    conn_data = conn.json()
    assert conn_data["status"] == "PENDING_CONSENT"
    assert conn_data["similarity_score"] is not None
    assert len(conn_data["mappings"]) > 0
    conn_id = conn_data["id"]

    # --- 5. B sees it in their incoming queue ---
    incoming = client.get("/api/connections/incoming", headers=_auth(org_b["api_key"]))
    assert incoming.status_code == 200
    assert any(c["id"] == conn_id for c in incoming.json())

    # A cannot consent on their own request
    bad_consent = client.post(
        f"/api/connections/{conn_id}/consent",
        headers=_auth(org_a["api_key"]),
        json={"agreed": True, "signatory_name": "Ada", "agreement_text": "ok"},
    )
    assert bad_consent.status_code == 403

    # --- 6. B approves with consent ---
    consent = client.post(
        f"/api/connections/{conn_id}/consent",
        headers=_auth(org_b["api_key"]),
        json={"agreed": True, "signatory_name": "Grace Hopper, DPO",
              "agreement_text": "TaxRegistryB agrees to share taxpayer data with LandRegistryA."},
    )
    assert consent.status_code == 200, consent.text
    assert consent.json()["status"] == "ACTIVE"

    # --- 7. A pulls the combined dataset ---
    exch = client.post(f"/api/connections/{conn_id}/exchange", headers=_auth(org_a["api_key"]))
    assert exch.status_code == 200, exch.text
    result = exch.json()
    assert result["combined_record_count"] == 1 + 2  # A's own record + B's 2 records
    assert result["xroad_envelope"]["signature"]
    sources = {r["_source_org"] for r in result["combined_dataset"]}
    assert sources == {"LandRegistryA", "TaxRegistryB"}

    # B (not the requester) cannot pull the exchange
    forbidden = client.post(f"/api/connections/{conn_id}/exchange", headers=_auth(org_b["api_key"]))
    assert forbidden.status_code == 403

    # --- 8. Audit trail has the expected signed events, in order ---
    audit = client.get(f"/api/connections/{conn_id}/audit", headers=_auth(org_a["api_key"]))
    assert audit.status_code == 200
    events = [e["event_type"] for e in audit.json()]
    assert events == [
        "connection_requested",
        "consent_approved",
        "xroad_secure_handshake",
        "xroad_secure_exchange",
    ]
    assert all(e["xroad_signature"] for e in audit.json())


def test_rejected_consent_blocks_exchange():
    org_a = _register("HospitalA")
    org_b = _register("InsurerB")

    ds_a = client.post("/api/datasets", headers=_auth(org_a["api_key"]),
                        json={"name": "patients", "sample_records": [{"name": "Bob"}]}).json()
    ds_b = client.post("/api/datasets", headers=_auth(org_b["api_key"]),
                        json={"name": "claims", "sample_records": [{"name": "Bob", "claim_amount": 200}]}).json()

    conn = client.post("/api/connections", headers=_auth(org_a["api_key"]), json={
        "counterparty_org_id": org_b["org_id"],
        "requester_dataset_id": ds_a["id"],
        "counterparty_dataset_id": ds_b["id"],
        "purpose": "test rejection",
    }).json()

    client.post(f"/api/connections/{conn['id']}/consent", headers=_auth(org_b["api_key"]), json={
        "agreed": False, "signatory_name": "Insurer Officer", "agreement_text": "We decline.",
    })

    exch = client.post(f"/api/connections/{conn['id']}/exchange", headers=_auth(org_a["api_key"]))
    assert exch.status_code == 409  # not ACTIVE, exchange refused


def test_auth_required():
    r = client.get("/api/datasets/mine")
    assert r.status_code in (401, 422)  # missing header

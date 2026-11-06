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
from tests.helpers import admin_decide  # noqa: E402

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
    assert conn_data["status"] == "PENDING_ADMIN_REVIEW"
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

    # --- 5b. B cannot consent until the admin has reviewed the mappings ---
    too_early = client.post(
        f"/api/connections/{conn_id}/consent", headers=_auth(org_b["api_key"]),
        json={"agreed": True, "signatory_name": "Grace", "agreement_text": "ok"},
    )
    assert too_early.status_code == 409
    assert admin_decide(client, conn_id).json()["status"] == "PENDING_CONSENT"

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
        "admin_review_approved",
        "consent_approved",
        "xroad_secure_handshake",
        "xroad_secure_exchange",
    ]
    assert all(e["xroad_signature"] for e in audit.json())


def test_joined_workflow_merges_matching_records_into_nested_output():
    """
    Any two orgs can now request data in 'joined' mode: pick the field each
    side uses as the shared key, describe the desired nested output shape,
    and /exchange returns ONE merged record per matched pair instead of a
    flat list of A's + reshaped B's records.
    """
    land = _register("LandRegistryX")
    tax = _register("TaxRegistryX")

    land_ds = client.post("/api/datasets", headers=_auth(land["api_key"]), json={
        "name": "properties", "sample_records": [
            {"property_id": "LRA-P00001", "citizen_id": "CID-00001", "owner_name": "Aarav Kumar",
             "survey_number": "SV-101-00001", "area_sq_m": 111.1, "land_use": "RESIDENTIAL",
             "district": "Visakhapatnam", "mandal": "Mandal-02", "village": "Village-002",
             "registration_date": "2015-04-08", "document_number": "DOC-70001",
             "assessed_value": 1513753, "ownership_status": "ACTIVE"},
            {"property_id": "LRA-P00002", "citizen_id": "CID-00002", "owner_name": "Priya Rao",
             "survey_number": "SV-101-00002", "area_sq_m": 200.0, "land_use": "COMMERCIAL",
             "district": "Vijayawada", "mandal": "Mandal-01", "village": "Village-005",
             "registration_date": "2018-06-01", "document_number": "DOC-70002",
             "assessed_value": 2000000, "ownership_status": "ACTIVE"},
        ],
    }).json()

    tax_ds = client.post("/api/datasets", headers=_auth(tax["api_key"]), json={
        "name": "tax_records", "sample_records": [
            {"taxpayer_id": "CID-00001", "taxpayer_name": "Aarav Kumar",
             "property_reference": "SV-101-00001", "tax_year": "2025-2026",
             "annual_property_value": 1513753, "tax_amount": 15138, "paid_amount": 15138,
             "balance_amount": 0, "last_payment_date": "2026-01-12", "tax_status": "PAID"},
            # no tax record for SV-101-00002 on purpose -> exercises the unmatched path
        ],
    }).json()

    conn = client.post("/api/connections", headers=_auth(land["api_key"]), json={
        "counterparty_org_id": tax["org_id"],
        "requester_dataset_id": land_ds["id"],
        "counterparty_dataset_id": tax_ds["id"],
        "purpose": "Combine land + tax records per property",
        "requester_join_field": "survey_number",
        "counterparty_join_field": "property_reference",
        "output_fields": [
            "property.property_id", "property.survey_number", "property.property_reference",
            "property.area_sq_m", "property.land_use",
            "property.location.district", "property.location.mandal", "property.location.village",
            "property.registration.registration_date", "property.registration.document_number",
            "property.ownership_status",
            "ownership_and_taxation.citizen_id", "ownership_and_taxation.owner_name",
            "ownership_and_taxation.taxpayer_id", "ownership_and_taxation.taxpayer_name",
            "financials.assessed_value", "financials.annual_property_value", "financials.tax_year",
            "financials.tax_amount", "financials.paid_amount", "financials.balance_amount",
            "financials.last_payment_date", "financials.tax_status",
        ],
    })
    assert conn.status_code == 200, conn.text
    conn_data = conn.json()
    assert conn_data["mode"] == "joined"
    conn_id = conn_data["id"]

    admin_decide(client, conn_id)
    client.post(f"/api/connections/{conn_id}/consent", headers=_auth(tax["api_key"]), json={
        "agreed": True, "signatory_name": "Tax Officer", "agreement_text": "agreed",
    })

    exch = client.post(f"/api/connections/{conn_id}/exchange", headers=_auth(land["api_key"]))
    assert exch.status_code == 200, exch.text
    result = exch.json()

    assert result["combined_record_count"] == 2
    assert result["matched_count"] == 1
    assert result["unmatched_requester_count"] == 1

    by_survey = {r["property"]["survey_number"]: r for r in result["combined_dataset"]}
    matched = by_survey["SV-101-00001"]
    assert matched["property"]["property_id"] == "LRA-P00001"
    assert matched["property"]["location"]["district"] == "Visakhapatnam"
    assert matched["property"]["registration"]["document_number"] == "DOC-70001"
    assert matched["ownership_and_taxation"]["taxpayer_name"] == "Aarav Kumar"
    assert matched["financials"]["tax_amount"] == 15138
    assert matched["financials"]["tax_status"] == "PAID"

    # unmatched property still comes back (with only the land-side fields filled in)
    unmatched = by_survey["SV-101-00002"]
    assert unmatched["property"]["property_id"] == "LRA-P00002"
    assert unmatched["financials"].get("tax_amount") is None


def test_auto_reconcile_removes_redundant_fields():
    """
    Omitting output_fields (but giving both join fields) switches to
    auto-reconcile: redundant fields collapse into one (detected by value
    agreement across joined records, or a declared alias), while anything
    unique to either side is still kept — a full outer join of columns.
    """
    land = _register("LandRegistryY")
    tax = _register("TaxRegistryY")

    land_ds = client.post("/api/datasets", headers=_auth(land["api_key"]), json={
        "name": "properties", "sample_records": [
            {"property_id": "LRA-P00001", "citizen_id": "CID-00001", "owner_name": "Aarav Kumar",
             "name": "Aarav Kumar", "gender": "MALE",
             "survey_number": "SV-101-00001", "area_sq_m": 111.1, "land_use": "RESIDENTIAL",
             "assessed_value": 1513753, "ownership_status": "ACTIVE"},
            {"property_id": "LRA-P00002", "citizen_id": "CID-00002", "owner_name": "Priya Rao",
             "name": "Priya Rao", "gender": "FEMALE",
             "survey_number": "SV-101-00002", "area_sq_m": 200.0, "land_use": "COMMERCIAL",
             "assessed_value": 2000000, "ownership_status": "ACTIVE"},
        ],
    }).json()

    tax_ds = client.post("/api/datasets", headers=_auth(tax["api_key"]), json={
        "name": "tax_records", "sample_records": [
            {"taxpayer_id": "CID-00001", "taxpayer_name": "Aarav Kumar", "person_name": "Aarav Kumar",
             "property_reference": "SV-101-00001", "tax_amount": 15138, "tax_status": "PAID"},
            {"taxpayer_id": "CID-00002", "taxpayer_name": "Priya Rao", "person_name": "Priya Rao",
             "property_reference": "SV-101-00002", "tax_amount": 20000, "tax_status": "PAID"},
        ],
    }).json()

    conn = client.post("/api/connections", headers=_auth(land["api_key"]), json={
        "counterparty_org_id": tax["org_id"],
        "requester_dataset_id": land_ds["id"],
        "counterparty_dataset_id": tax_ds["id"],
        "purpose": "Auto-reconciled combined view",
        "requester_join_field": "survey_number",
        "counterparty_join_field": "property_reference",
        "field_aliases": {"name": "person_name"},
    })
    assert conn.status_code == 200, conn.text
    conn_data = conn.json()
    assert conn_data["mode"] == "joined"
    output_fields = conn_data["output_fields"]

    # taxpayer_id/citizen_id and owner_name/taxpayer_name agree on every
    # joined record -> auto-detected as redundant -> collapsed to ONE field.
    assert output_fields.count("citizen_id") + output_fields.count("taxpayer_id") == 1
    assert output_fields.count("owner_name") + output_fields.count("taxpayer_name") == 1
    # name/person_name declared as an alias -> also collapsed to one field.
    assert output_fields.count("name") + output_fields.count("person_name") == 1
    # survey_number/property_reference is the join key -> one field.
    assert output_fields.count("survey_number") + output_fields.count("property_reference") == 1
    # gender only exists on the land side -> kept as its own field.
    assert "gender" in output_fields
    # tax_amount/tax_status only exist on the tax side -> kept as their own fields.
    assert "tax_amount" in output_fields and "tax_status" in output_fields
    # unrelated fields never get merged just because they're both "status"-ish.
    assert output_fields.count("ownership_status") == 1 and output_fields.count("tax_status") == 1

    admin_decide(client, conn_data["id"])
    consent = client.post(f"/api/connections/{conn_data['id']}/consent",
                           headers=_auth(tax["api_key"]),
                           json={"agreed": True, "signatory_name": "Tax Officer", "agreement_text": "agreed"})
    assert consent.status_code == 200

    exch = client.post(f"/api/connections/{conn_data['id']}/exchange", headers=_auth(land["api_key"]))
    assert exch.status_code == 200, exch.text
    result = exch.json()
    assert result["matched_count"] == 2

    rec = next(r for r in result["combined_dataset"] if r.get("citizen_id") == "CID-00001")
    assert rec["gender"] == "MALE"              # land-only field present
    assert rec["tax_amount"] == 15138            # tax-only field present
    assert rec["owner_name"] == "Aarav Kumar"    # merged field, single key
    assert "taxpayer_name" not in rec            # redundant duplicate gone
    assert "taxpayer_id" not in rec
    assert rec["name"] == "Aarav Kumar"
    assert "person_name" not in rec


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

    admin_decide(client, conn["id"])
    client.post(f"/api/connections/{conn['id']}/consent", headers=_auth(org_b["api_key"]), json={
        "agreed": False, "signatory_name": "Insurer Officer", "agreement_text": "We decline.",
    })

    exch = client.post(f"/api/connections/{conn['id']}/exchange", headers=_auth(org_a["api_key"]))
    assert exch.status_code == 409  # not ACTIVE, exchange refused


def test_auth_required():
    r = client.get("/api/datasets/mine")
    assert r.status_code in (401, 422)  # missing header

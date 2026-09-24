"""
DPI Connect — API for cross-organization data connection requests with
schema-similarity scoring, a consent/agreement workflow, and a simulated
X-Road secure exchange layer.

Run: uvicorn app.main:app --reload --port 8000
Frontend: http://localhost:8000/app/
Docs:     http://localhost:8000/docs
"""

from __future__ import annotations

import secrets

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import models_db, schemas_api, xroad_adapter
from app.auth import get_current_org
from app.database import get_db, init_db, utcnow
from app.mapping_engine.conflict_resolver import CONFIDENCE_AUTO_APPROVE_THRESHOLD
from app.mapping_engine.llm_mapper import generate_mappings
from app.mapping_engine.schema_discovery import discover_schema
from app.mapping_engine.schema_models import MappingSet, Schema
from app.mapping_engine.transform import transform_record

app = FastAPI(title="DPI Connect", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

EXCHANGE_MIN_CONFIDENCE = 0.5  # mappings below this are excluded from the combined dataset

init_db()  # create Mongo indexes on import — simple and sufficient for this app's single-process scale


@app.get("/")
def root():
    return RedirectResponse("/app/")


# =========================================================================
# Organizations
# =========================================================================

@app.post("/api/orgs/register", response_model=schemas_api.OrgRegisterResponse)
def register_org(req: schemas_api.OrgRegisterRequest, db=Depends(get_db)):
    if db[models_db.ORGANIZATIONS].find_one({"name": req.name}):
        raise HTTPException(409, f"organization '{req.name}' already registered")
    org = models_db.new_organization(
        name=req.name,
        contact_email=req.contact_email,
        api_key=secrets.token_hex(24),
    )
    db[models_db.ORGANIZATIONS].insert_one(org)
    return schemas_api.OrgRegisterResponse(org_id=org["_id"], name=org["name"], api_key=org["api_key"])


@app.get("/api/orgs", response_model=list[schemas_api.OrgPublic])
def list_orgs(db=Depends(get_db)):
    return [
        schemas_api.OrgPublic(id=o["_id"], name=o["name"])
        for o in db[models_db.ORGANIZATIONS].find()
    ]


@app.get("/api/orgs/me", response_model=schemas_api.OrgPublic)
def whoami(org: dict = Depends(get_current_org)):
    return schemas_api.OrgPublic(id=org["_id"], name=org["name"])


# =========================================================================
# Datasets
# =========================================================================

def _dataset_summary(ds: dict) -> schemas_api.DatasetSummary:
    schema = Schema.model_validate(ds["schema_json"])
    return schemas_api.DatasetSummary(
        id=ds["_id"], name=ds["name"],
        field_names=[f.path for f in schema.fields],
        field_count=len(schema.fields),
        record_count=len(ds["sample_records"]),
        created_at=ds["created_at"],
    )


@app.post("/api/datasets", response_model=schemas_api.DatasetDetail)
def upload_dataset(
    req: schemas_api.DatasetUploadRequest,
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    schema = discover_schema(org["_id"], req.name, req.sample_records)
    ds = models_db.new_dataset(
        org_id=org["_id"],
        name=req.name,
        sample_records=req.sample_records,
        schema_json=schema.model_dump(mode="json"),
    )
    db[models_db.DATASETS].insert_one(ds)
    return schemas_api.DatasetDetail(**_dataset_summary(ds).model_dump(), dataset_schema=ds["schema_json"])


@app.get("/api/datasets/mine", response_model=list[schemas_api.DatasetSummary])
def my_datasets(
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    rows = db[models_db.DATASETS].find({"org_id": org["_id"]})
    return [_dataset_summary(d) for d in rows]


@app.get("/api/orgs/{org_id}/datasets", response_model=list[schemas_api.DatasetSummary])
def org_datasets(org_id: str, db=Depends(get_db)):
    """
    Schema-level metadata only (field names/counts) — never the actual
    sample records. Lets a prospective requester see *what shape* of data
    an org has, without seeing any real data before consent is granted.
    """
    rows = db[models_db.DATASETS].find({"org_id": org_id})
    return [_dataset_summary(d) for d in rows]


# =========================================================================
# Connections
# =========================================================================

def _connection_summary(c: dict, db) -> schemas_api.ConnectionSummary:
    requester = db[models_db.ORGANIZATIONS].find_one({"_id": c["requester_org_id"]})
    counterparty = db[models_db.ORGANIZATIONS].find_one({"_id": c["counterparty_org_id"]})
    return schemas_api.ConnectionSummary(
        id=c["_id"],
        requester_org_id=c["requester_org_id"], requester_org_name=requester["name"],
        counterparty_org_id=c["counterparty_org_id"], counterparty_org_name=counterparty["name"],
        purpose=c["purpose"], status=c["status"], similarity_score=c["similarity_score"],
        created_at=c["created_at"], updated_at=c["updated_at"],
    )


def _log_audit(db, connection_id: str | None, event_type: str, detail: dict) -> dict:
    entry = models_db.new_audit_entry(
        connection_id=connection_id,
        event_type=event_type,
        detail=detail,
        xroad_signature=detail.get("signature") or xroad_adapter.sign_payload(detail),
    )
    db[models_db.AUDIT_LOG].insert_one(entry)
    return entry


@app.post("/api/connections", response_model=schemas_api.ConnectionDetail)
def create_connection(
    req: schemas_api.ConnectionCreateRequest,
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    if req.counterparty_org_id == org["_id"]:
        raise HTTPException(400, "cannot request a connection with your own organization")

    counterparty = db[models_db.ORGANIZATIONS].find_one({"_id": req.counterparty_org_id})
    if not counterparty:
        raise HTTPException(404, "counterparty organization not found")

    requester_ds = db[models_db.DATASETS].find_one({"_id": req.requester_dataset_id})
    counterparty_ds = db[models_db.DATASETS].find_one({"_id": req.counterparty_dataset_id})
    if not requester_ds or requester_ds["org_id"] != org["_id"]:
        raise HTTPException(404, "requester_dataset_id not found under your organization")
    if not counterparty_ds or counterparty_ds["org_id"] != counterparty["_id"]:
        raise HTTPException(404, "counterparty_dataset_id not found under that organization")

    # Similarity: can B's data (source) be mapped into A's shape (target)?
    source_schema = Schema.model_validate(counterparty_ds["schema_json"])
    target_schema = Schema.model_validate(requester_ds["schema_json"])
    mapping_set = generate_mappings(source_schema, target_schema)
    confidences = [m.confidence for m in mapping_set.mappings]
    similarity_score = round(sum(confidences) / len(confidences), 3) if confidences else 0.0

    conn = models_db.new_connection_request(
        requester_org_id=org["_id"],
        counterparty_org_id=counterparty["_id"],
        requester_dataset_id=requester_ds["_id"],
        counterparty_dataset_id=counterparty_ds["_id"],
        purpose=req.purpose,
        similarity_score=similarity_score,
        mapping_set_json=mapping_set.model_dump(mode="json"),
    )
    db[models_db.CONNECTION_REQUESTS].insert_one(conn)

    _log_audit(db, conn["_id"], "connection_requested", {
        "requester_org": org["name"], "counterparty_org": counterparty["name"],
        "similarity_score": similarity_score,
    })
    return _connection_detail(conn, db)


def _connection_detail(c: dict, db) -> schemas_api.ConnectionDetail:
    summary = _connection_summary(c, db)
    mapping_set = MappingSet.model_validate(c["mapping_set_json"]) if c.get("mapping_set_json") else None
    mappings = []
    low_conf = []
    if mapping_set:
        for m in mapping_set.mappings:
            mappings.append(schemas_api.MappingSummary(
                target_field=m.target_path, source_fields=m.source_paths,
                transform_type=m.transform_type.value, confidence=m.confidence,
                rationale=m.rationale,
            ))
        low_conf = [m.target_path for m in mapping_set.low_confidence_mappings(CONFIDENCE_AUTO_APPROVE_THRESHOLD)]
    return schemas_api.ConnectionDetail(**summary.model_dump(), mappings=mappings, low_confidence_fields=low_conf)


def _get_connection_for_party(conn_id: str, org: dict, db) -> dict:
    conn = db[models_db.CONNECTION_REQUESTS].find_one({"_id": conn_id})
    if not conn:
        raise HTTPException(404, "connection not found")
    if org["_id"] not in (conn["requester_org_id"], conn["counterparty_org_id"]):
        raise HTTPException(403, "you are not a party to this connection")
    return conn


def _update_connection_status(db, conn: dict, status: str) -> None:
    """Mutates `conn` in place and persists the same change to Mongo."""
    now = utcnow()
    conn["status"] = status
    conn["updated_at"] = now
    db[models_db.CONNECTION_REQUESTS].update_one(
        {"_id": conn["_id"]}, {"$set": {"status": status, "updated_at": now}}
    )


@app.get("/api/connections/mine", response_model=list[schemas_api.ConnectionSummary])
def my_outgoing_connections(
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    rows = db[models_db.CONNECTION_REQUESTS].find({"requester_org_id": org["_id"]})
    return [_connection_summary(c, db) for c in rows]


@app.get("/api/connections/incoming", response_model=list[schemas_api.ConnectionSummary])
def incoming_connections(
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    rows = db[models_db.CONNECTION_REQUESTS].find({"counterparty_org_id": org["_id"]})
    return [_connection_summary(c, db) for c in rows]


@app.get("/api/connections/{conn_id}", response_model=schemas_api.ConnectionDetail)
def get_connection(
    conn_id: str,
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    conn = _get_connection_for_party(conn_id, org, db)
    return _connection_detail(conn, db)


@app.post("/api/connections/{conn_id}/consent", response_model=schemas_api.ConnectionDetail)
def submit_consent(
    conn_id: str,
    req: schemas_api.ConsentRequest,
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    conn = _get_connection_for_party(conn_id, org, db)
    if org["_id"] != conn["counterparty_org_id"]:
        raise HTTPException(403, "only the counterparty can submit consent for this connection")
    if conn["status"] != "PENDING_CONSENT":
        raise HTTPException(409, f"connection is not awaiting consent (status={conn['status']})")

    decision = "approved" if req.agreed else "rejected"
    consent = models_db.new_consent(
        connection_id=conn["_id"],
        decision=decision,
        signatory_name=req.signatory_name,
        agreement_text=req.agreement_text,
    )
    db[models_db.CONSENTS].insert_one(consent)

    if req.agreed:
        _update_connection_status(db, conn, "APPROVED")
        _log_audit(db, conn["_id"], "consent_approved", {
            "signatory_name": req.signatory_name, "counterparty_org": org["name"],
        })
        handshake = xroad_adapter.secure_handshake(conn["requester_org_id"], conn["counterparty_org_id"], conn["_id"])
        _log_audit(db, conn["_id"], "xroad_secure_handshake", handshake)
        _update_connection_status(db, conn, "ACTIVE")
    else:
        _update_connection_status(db, conn, "REJECTED")
        _log_audit(db, conn["_id"], "consent_rejected", {
            "signatory_name": req.signatory_name, "counterparty_org": org["name"],
        })

    return _connection_detail(conn, db)


@app.post("/api/connections/{conn_id}/exchange", response_model=schemas_api.ExchangeResult)
def exchange(
    conn_id: str,
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    conn = _get_connection_for_party(conn_id, org, db)
    if org["_id"] != conn["requester_org_id"]:
        raise HTTPException(403, "only the requesting organization can pull the combined dataset")
    if conn["status"] != "ACTIVE":
        raise HTTPException(409, f"connection is not active yet (status={conn['status']})")

    requester_ds = db[models_db.DATASETS].find_one({"_id": conn["requester_dataset_id"]})
    counterparty_ds = db[models_db.DATASETS].find_one({"_id": conn["counterparty_dataset_id"]})
    requester_org = db[models_db.ORGANIZATIONS].find_one({"_id": conn["requester_org_id"]})
    counterparty_org = db[models_db.ORGANIZATIONS].find_one({"_id": conn["counterparty_org_id"]})

    full_mapping_set = MappingSet.model_validate(conn["mapping_set_json"])
    filtered = full_mapping_set.model_copy(update={
        "mappings": [m for m in full_mapping_set.mappings if m.confidence >= EXCHANGE_MIN_CONFIDENCE]
    })

    combined: list[dict] = [
        {"_source_org": requester_org["name"], **rec} for rec in requester_ds["sample_records"]
    ]
    transform_errors: list[dict[str, str]] = []
    for rec in counterparty_ds["sample_records"]:
        out, errors = transform_record(rec, filtered)
        combined.append({"_source_org": counterparty_org["name"], **out})
        transform_errors.extend({"target_field": e.mapping.target_path, "reason": e.reason} for e in errors)

    envelope = xroad_adapter.secure_exchange_envelope(conn["_id"], {"records": combined})
    _log_audit(db, conn["_id"], "xroad_secure_exchange", envelope)

    return schemas_api.ExchangeResult(
        connection_id=conn["_id"],
        combined_record_count=len(combined),
        combined_dataset=combined,
        transform_errors=transform_errors,
        xroad_envelope=envelope,
    )


@app.get("/api/connections/{conn_id}/audit", response_model=list[schemas_api.AuditEntry])
def connection_audit(
    conn_id: str,
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    conn = _get_connection_for_party(conn_id, org, db)
    rows = (
        db[models_db.AUDIT_LOG]
        .find({"connection_id": conn["_id"]})
        .sort("created_at", 1)
    )
    return [
        schemas_api.AuditEntry(
            id=r["_id"], event_type=r["event_type"], detail=r["detail_json"],
            xroad_signature=r["xroad_signature"], created_at=r["created_at"],
        )
        for r in rows
    ]


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Frontend (vanilla HTML/JS) served at /app/
import os
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/app", StaticFiles(directory=_frontend_dir, html=True), name="frontend")

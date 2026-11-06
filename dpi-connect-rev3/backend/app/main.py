"""
DPI Connect — API for cross-organization data connection requests with
evidence-scored field mappings, platform-admin review, a consent/agreement
workflow, and a simulated X-Road secure exchange layer.

Run: uvicorn app.main:app --reload --port 8000
Frontend: http://localhost:8000/app/
Docs:     http://localhost:8000/docs
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import ingest, models_db, schemas_api, xroad_adapter
from app.auth import get_current_admin, get_current_org, is_admin_key
from app.database import get_db, init_db, utcnow
from app.mapping_engine.field_matcher import CONFIDENCE_HIGH, profile_schema, reconcile
from app.mapping_engine.join_planner import detect_join_fields
from app.mapping_engine.llm_mapper import generate_mappings
from app.mapping_engine.schema_discovery import discover_schema
from app.mapping_engine.schema_merge import mapping_sets_from_reconciliation, report_from_mapping_sets
from app.mapping_engine.schema_models import FieldSchema, MappingSet, Schema
from app.mapping_engine.transform import (
    get_path, join_key as _join_key, merge_by_confidence, transform_record, transform_record_with_confidence,
)

from fastapi import Header

app = FastAPI(title="DPI Connect", version="0.2.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# Only applies to mappings produced by the LLM/fuzzy mapper (explicit output_fields mode).
# Evidence-scored mappings are never dropped: a column unique to one side must still flow through.
EXCHANGE_MIN_CONFIDENCE = 0.5
SCHEMA_SAMPLE_ROWS = 3000          # schema discovery looks at a spread sample, not all 50k rows
MAX_STORED_BYTES = 15 * 1024 * 1024  # MongoDB's hard document limit is 16 MB

init_db()  # create Mongo indexes on import — simple and sufficient for this app's single-process scale


def _require_counterparty_consent() -> bool:
    return os.environ.get("REQUIRE_COUNTERPARTY_CONSENT", "1").lower() not in ("0", "false", "no")


@app.get("/")
def root():
    return RedirectResponse("/app/")


# =========================================================================
# Organizations + identity
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


@app.get("/api/auth/whoami", response_model=schemas_api.WhoAmI)
def auth_whoami(x_api_key: str = Header(..., alias="X-API-Key"), db=Depends(get_db)):
    """Tells the frontend which console to show for a key: platform admin or an organization."""
    if is_admin_key(x_api_key):
        return schemas_api.WhoAmI(role="admin", id="admin", name="Platform admin")
    org = db[models_db.ORGANIZATIONS].find_one({"api_key": x_api_key})
    if not org:
        raise HTTPException(401, "invalid API key")
    return schemas_api.WhoAmI(role="org", id=org["_id"], name=org["name"])


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


def _spread(records: list[dict], cap: int) -> list[dict]:
    if len(records) <= cap:
        return records
    step = max(1, len(records) // cap)
    return records[::step][:cap]


def _columns_for(schema: Schema, records: list[dict]) -> list[schemas_api.DatasetColumn]:
    profiles = profile_schema(schema, records)
    return [
        schemas_api.DatasetColumn(
            name=f.path, type=f.type.value, value_kind=profiles[f.path].kind,
            non_null=profiles[f.path].non_null, distinct=len(profiles[f.path].distinct),
        )
        for f in schema.fields
    ]


def _store_dataset(db, org: dict, name: str, records: list[dict]) -> schemas_api.DatasetDetail:
    if len(json.dumps(records, default=str)) > MAX_STORED_BYTES:
        raise HTTPException(413, "dataset is too large to store (limit ~15 MB once parsed); upload fewer rows")
    schema = discover_schema(org["_id"], name, _spread(records, SCHEMA_SAMPLE_ROWS))
    ds = models_db.new_dataset(
        org_id=org["_id"], name=name, sample_records=records,
        schema_json=schema.model_dump(mode="json"),
    )
    db[models_db.DATASETS].insert_one(ds)
    return schemas_api.DatasetDetail(**_dataset_summary(ds).model_dump(), dataset_schema=ds["schema_json"])


def _parse_input(file: Optional[UploadFile], text: Optional[str], fmt: str, sheet: Optional[str]) -> dict[str, Any]:
    try:
        if file is not None and file.filename:
            data = file.file.read(ingest.MAX_BYTES + 1)
            return ingest.parse_dataset(filename=file.filename, data=data, fmt=fmt, sheet=sheet or None)
        return ingest.parse_dataset(text=text, fmt=fmt, sheet=sheet or None)
    except ingest.IngestError as e:
        raise HTTPException(400, str(e))


@app.post("/api/datasets", response_model=schemas_api.DatasetDetail)
def upload_dataset(
    req: schemas_api.DatasetUploadRequest,
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    """JSON body variant (API clients). The UI uses /preview + /import below."""
    return _store_dataset(db, org, req.name, req.sample_records)


@app.post("/api/datasets/preview", response_model=schemas_api.DatasetPreview)
def preview_dataset(
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    fmt: str = Form("auto"),
    sheet: Optional[str] = Form(None),
    org: dict = Depends(get_current_org),
):
    """Parse an uploaded file OR pasted text and show what would be imported — nothing is saved."""
    parsed = _parse_input(file, text, fmt, sheet)
    records = parsed["records"]
    schema = discover_schema(org["_id"], "preview", _spread(records, SCHEMA_SAMPLE_ROWS))
    return schemas_api.DatasetPreview(
        format=parsed["format"], record_count=len(records),
        columns=_columns_for(schema, records),
        preview=json.loads(json.dumps(records[:ingest.PREVIEW_ROWS], default=str)),
        warnings=parsed["warnings"],
    )


@app.post("/api/datasets/import", response_model=schemas_api.DatasetDetail)
def import_dataset(
    name: str = Form(...),
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    fmt: str = Form("auto"),
    sheet: Optional[str] = Form(None),
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    """Save an uploaded file OR pasted text as a dataset."""
    if not name.strip():
        raise HTTPException(400, "give the dataset a name")
    parsed = _parse_input(file, text, fmt, sheet)
    return _store_dataset(db, org, name.strip(), parsed["records"])


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

    requester_schema = Schema.model_validate(requester_ds["schema_json"])
    counterparty_schema = Schema.model_validate(counterparty_ds["schema_json"])

    for field, schema, label in [
        (req.requester_join_field, requester_schema, "requester"),
        (req.counterparty_join_field, counterparty_schema, "counterparty"),
    ]:
        if field is not None and schema.field_by_path(field) is None:
            raise HTTPException(400, f"{label}_join_field '{field}' is not a field of that dataset")

    # Join key: explicit if both fields were given, otherwise detected from the data.
    join_r, join_c = req.requester_join_field, req.counterparty_join_field
    join_how = "user-specified"
    join_detection: dict[str, Any] | None = None
    if not (join_r and join_c):
        join_how = "auto-detected"
        detected = detect_join_fields(
            requester_schema, counterparty_schema,
            requester_ds["sample_records"], counterparty_ds["sample_records"],
        )
        if detected:
            join_r, join_c, detect_score, detect_reason = detected
            join_detection = {"requester_field": join_r, "counterparty_field": join_c,
                              "score": detect_score, "reason": detect_reason}

    if req.output_fields:
        # Explicit shape: the caller names the output fields; each side is mapped
        # into it by the LLM / fuzzy engine (their confidences are used as-is).
        output_schema = Schema(
            subsystem_id="combined_output",
            service_name=f"{org['name']}__{counterparty['name']}_combined",
            fields=[FieldSchema(name=p.split(".")[-1], path=p) for p in req.output_fields],
        )
        mapping_set_requester = generate_mappings(requester_schema, output_schema)
        mapping_set_counterparty = generate_mappings(counterparty_schema, output_schema)
        reconciliation_report, scoring_summary = report_from_mapping_sets(
            mapping_set_requester, mapping_set_counterparty, output_schema,
            len(requester_schema.fields), len(counterparty_schema.fields),
        )
    else:
        # Auto-reconcile: a FULL OUTER JOIN of columns. Every column gets an
        # evidence-based confidence (names, meaning, value shape, agreement on
        # joined rows) instead of a blanket 100%.
        try:
            rec = reconcile(
                requester_schema, counterparty_schema,
                requester_ds["sample_records"], counterparty_ds["sample_records"],
                join_r if join_c else None, join_c if join_r else None,
                field_aliases=req.field_aliases, join_how=join_how,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        output_schema = rec.output_schema
        mapping_set_requester, mapping_set_counterparty = mapping_sets_from_reconciliation(
            rec, requester_schema, counterparty_schema,
        )
        reconciliation_report, scoring_summary = rec.columns, rec.summary

    similarity_score = scoring_summary["schema_overlap"]

    conn = models_db.new_connection_request(
        requester_org_id=org["_id"],
        counterparty_org_id=counterparty["_id"],
        requester_dataset_id=requester_ds["_id"],
        counterparty_dataset_id=counterparty_ds["_id"],
        purpose=req.purpose,
        similarity_score=similarity_score,
        mapping_set_json=mapping_set_requester.model_dump(mode="json"),
        mode="joined",
        requester_join_field=join_r if join_c else None,
        counterparty_join_field=join_c if join_r else None,
        output_schema_json=output_schema.model_dump(mode="json"),
        mapping_set_counterparty_json=mapping_set_counterparty.model_dump(mode="json"),
        reconciliation_report=reconciliation_report,
        scoring_summary=scoring_summary,
    )
    db[models_db.CONNECTION_REQUESTS].insert_one(conn)

    _log_audit(db, conn["_id"], "connection_requested", {
        "requester_org": org["name"], "counterparty_org": counterparty["name"],
        "similarity_score": similarity_score, "mode": conn["mode"],
        "mean_match_confidence": scoring_summary.get("mean_match_confidence"),
        "needs_review": scoring_summary.get("needs_review"),
        "join_detection": join_detection,
    })
    return _connection_detail(conn, db)


def _connection_detail(c: dict, db) -> schemas_api.ConnectionDetail:
    summary = _connection_summary(c, db)
    mode = c.get("mode", "concat")

    mapping_sets = []
    if c.get("mapping_set_json"):
        mapping_sets.append(MappingSet.model_validate(c["mapping_set_json"]))
    if mode == "joined" and c.get("mapping_set_counterparty_json"):
        mapping_sets.append(MappingSet.model_validate(c["mapping_set_counterparty_json"]))

    mappings = []
    for mapping_set in mapping_sets:
        for m in mapping_set.mappings:
            mappings.append(schemas_api.MappingSummary(
                target_field=m.target_path, source_fields=m.source_paths,
                transform_type=m.transform_type.value, confidence=m.confidence,
                rationale=m.rationale,
            ))

    report = c.get("reconciliation_report") or []
    if report:
        low_conf = [r["output_field"] for r in report if r.get("needs_review")]
    else:  # records created before evidence scoring existed
        low_conf = sorted({m.target_field for m in mappings if m.confidence < CONFIDENCE_HIGH})

    return schemas_api.ConnectionDetail(
        **summary.model_dump(), mappings=mappings, low_confidence_fields=low_conf,
        scoring_summary=c.get("scoring_summary") or {},
        admin_review=c.get("admin_review"),
        mode=mode,
        output_fields=[f["path"] for f in c["output_schema_json"]["fields"]] if c.get("output_schema_json") else None,
        requester_join_field=c.get("requester_join_field"),
        counterparty_join_field=c.get("counterparty_join_field"),
        reconciliation_report=report,
    )


def _get_connection_for_party(conn_id: str, org: dict, db) -> dict:
    conn = db[models_db.CONNECTION_REQUESTS].find_one({"_id": conn_id})
    if not conn:
        raise HTTPException(404, "connection not found")
    if org["_id"] not in (conn["requester_org_id"], conn["counterparty_org_id"]):
        raise HTTPException(403, "you are not a party to this connection")
    return conn


def _update_connection_status(db, conn: dict, status: str, **extra: Any) -> None:
    """Mutates `conn` in place and persists the same change to Mongo."""
    now = utcnow()
    conn["status"] = status
    conn["updated_at"] = now
    conn.update(extra)
    db[models_db.CONNECTION_REQUESTS].update_one(
        {"_id": conn["_id"]}, {"$set": {"status": status, "updated_at": now, **extra}}
    )


def _activate(db, conn: dict) -> None:
    """APPROVED -> secure handshake -> ACTIVE."""
    handshake = xroad_adapter.secure_handshake(conn["requester_org_id"], conn["counterparty_org_id"], conn["_id"])
    _log_audit(db, conn["_id"], "xroad_secure_handshake", handshake)
    _update_connection_status(db, conn, "ACTIVE")


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
    if conn["status"] == "PENDING_ADMIN_REVIEW":
        raise HTTPException(409, "the platform admin has not reviewed this connection yet")
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
        _activate(db, conn)
    else:
        _update_connection_status(db, conn, "REJECTED")
        _log_audit(db, conn["_id"], "consent_rejected", {
            "signatory_name": req.signatory_name, "counterparty_org": org["name"],
        })

    return _connection_detail(conn, db)


# =========================================================================
# Platform admin: review mappings + confidence, decide whether to connect
# =========================================================================

def _audit_entries(conn_id: str, db) -> list[schemas_api.AuditEntry]:
    rows = db[models_db.AUDIT_LOG].find({"connection_id": conn_id}).sort("created_at", 1)
    return [
        schemas_api.AuditEntry(
            id=r["_id"], event_type=r["event_type"], detail=r["detail_json"],
            xroad_signature=r["xroad_signature"], created_at=r["created_at"],
        )
        for r in rows
    ]


def _admin_dataset_view(ds: dict, org: dict) -> schemas_api.AdminDatasetView:
    """Field-level metadata only — the admin reviews mappings, not raw records."""
    schema = Schema.model_validate(ds["schema_json"])
    return schemas_api.AdminDatasetView(
        org_name=org["name"], dataset_name=ds["name"], record_count=len(ds["sample_records"]),
        fields=_columns_for(schema, ds["sample_records"]),
    )


@app.get("/api/admin/connections", response_model=list[schemas_api.AdminConnectionRow])
def admin_list_connections(
    status: Optional[str] = None,
    admin: dict = Depends(get_current_admin),
    db=Depends(get_db),
):
    query = {"status": status} if status else {}
    rows = sorted(db[models_db.CONNECTION_REQUESTS].find(query), key=lambda c: c["created_at"], reverse=True)
    return [
        schemas_api.AdminConnectionRow(
            **_connection_summary(c, db).model_dump(),
            scoring_summary=c.get("scoring_summary") or {}, admin_review=c.get("admin_review"),
        )
        for c in rows
    ]


@app.get("/api/admin/connections/{conn_id}", response_model=schemas_api.AdminConnectionReview)
def admin_get_connection(
    conn_id: str,
    admin: dict = Depends(get_current_admin),
    db=Depends(get_db),
):
    conn = db[models_db.CONNECTION_REQUESTS].find_one({"_id": conn_id})
    if not conn:
        raise HTTPException(404, "connection not found")
    requester_ds = db[models_db.DATASETS].find_one({"_id": conn["requester_dataset_id"]})
    counterparty_ds = db[models_db.DATASETS].find_one({"_id": conn["counterparty_dataset_id"]})
    requester_org = db[models_db.ORGANIZATIONS].find_one({"_id": conn["requester_org_id"]})
    counterparty_org = db[models_db.ORGANIZATIONS].find_one({"_id": conn["counterparty_org_id"]})
    return schemas_api.AdminConnectionReview(
        **_connection_detail(conn, db).model_dump(),
        requester_dataset=_admin_dataset_view(requester_ds, requester_org),
        counterparty_dataset=_admin_dataset_view(counterparty_ds, counterparty_org),
        audit=_audit_entries(conn_id, db),
    )


@app.post("/api/admin/connections/{conn_id}/decision", response_model=schemas_api.ConnectionDetail)
def admin_decide(
    conn_id: str,
    req: schemas_api.AdminDecisionRequest,
    admin: dict = Depends(get_current_admin),
    db=Depends(get_db),
):
    conn = db[models_db.CONNECTION_REQUESTS].find_one({"_id": conn_id})
    if not conn:
        raise HTTPException(404, "connection not found")
    if conn["status"] != "PENDING_ADMIN_REVIEW":
        raise HTTPException(409, f"connection is not awaiting admin review (status={conn['status']})")

    summary = conn.get("scoring_summary") or {}
    review = {
        "decision": "approved" if req.decision == "approve" else "rejected",
        "reviewer_name": req.reviewer_name.strip(),
        "notes": req.notes.strip(),
        "decided_at": utcnow(),
        "flagged_columns_at_decision": summary.get("needs_review"),
        "mean_match_confidence_at_decision": summary.get("mean_match_confidence"),
    }
    audit_detail = {k: v for k, v in review.items() if k != "decided_at"}

    if req.decision == "approve":
        _log_audit(db, conn_id, "admin_review_approved", audit_detail)
        if _require_counterparty_consent():
            _update_connection_status(db, conn, "PENDING_CONSENT", admin_review=review)
        else:
            _update_connection_status(db, conn, "APPROVED", admin_review=review)
            _activate(db, conn)
    else:
        _log_audit(db, conn_id, "admin_review_rejected", audit_detail)
        _update_connection_status(db, conn, "REJECTED_BY_ADMIN", admin_review=review)

    return _connection_detail(conn, db)


# =========================================================================
# Exchange
# =========================================================================

def _exchange_concat(conn: dict, requester_ds: dict, counterparty_ds: dict,
                      requester_org: dict, counterparty_org: dict) -> tuple[list[dict], list[dict]]:
    """Legacy behaviour (records created before joined mode): B reshaped into A's schema, appended."""
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
    return combined, transform_errors


def _exchange_joined(conn: dict, requester_ds: dict, counterparty_ds: dict,
                     requester_org: dict, counterparty_org: dict) -> tuple[list[dict], list[dict], dict]:
    """
    FULL OUTER JOIN of A's and B's records into one combined dataset:

      * matched rows      -> A's and B's fields merged into a single record
      * A-only rows       -> kept, B's columns empty
      * B-only rows       -> kept, A's columns empty

    Row counts never multiply: each source record is consumed at most once,
    so the combined dataset can never exceed len(A) + len(B) records (repeated
    keys are paired positionally). Single-pass hash join: B is indexed by key
    once (O(len(B))), then A is streamed against that index (O(len(A))).
    """
    def _filtered(key: str) -> MappingSet:
        ms = MappingSet.model_validate(conn[key])
        # Evidence-scored mappings carry "how sure are we this column is shared", not
        # "is this data safe to send" — so they are never dropped here.
        return ms.model_copy(update={
            "mappings": [m for m in ms.mappings
                         if m.generated_by == "evidence_scoring" or m.confidence >= EXCHANGE_MIN_CONFIDENCE]
        })

    mapping_set_requester = _filtered("mapping_set_json")
    mapping_set_counterparty = _filtered("mapping_set_counterparty_json")

    join_r = conn.get("requester_join_field")
    join_c = conn.get("counterparty_join_field")

    records_a = requester_ds["sample_records"]
    records_b = counterparty_ds["sample_records"]
    name_a = requester_org["name"]
    name_b = counterparty_org["name"]

    combined: list[dict] = []
    transform_errors: list[dict[str, str]] = []

    def _shape(rec: dict, mapping_set: MappingSet):
        out, conf, errors = transform_record_with_confidence(rec, mapping_set)
        transform_errors.extend({"target_field": e.mapping.target_path, "reason": e.reason} for e in errors)
        return out, conf

    # ---- keyless case: no shared key exists, so no rows can be matched.
    # Still a full outer result: every row from both sides, unioned columns.
    if not (join_r and join_c):
        for rec in records_a:
            out, _ = _shape(rec, mapping_set_requester)
            combined.append({"_source_org": name_a, **out})
        for rec in records_b:
            out, _ = _shape(rec, mapping_set_counterparty)
            combined.append({"_source_org": name_b, **out})
        return combined, transform_errors, {
            "matched_count": 0,
            "unmatched_requester_count": len(records_a),
            "unmatched_counterparty_count": len(records_b),
        }

    b_index: dict[Any, list[int]] = {}
    for i, rec in enumerate(records_b):
        key = get_path(rec, join_c)
        if key is None:
            continue
        b_index.setdefault(_join_key(key), []).append(i)

    b_consumed = bytearray(len(records_b))
    b_cursor: dict[Any, int] = {}
    matched_count = 0
    unmatched_a = 0

    for rec_a in records_a:
        out_a, conf_a = _shape(rec_a, mapping_set_requester)
        key = get_path(rec_a, join_r)
        norm = _join_key(key) if key is not None else None

        partner_idx = None
        if norm is not None and norm in b_index:
            bucket = b_index[norm]
            pos = b_cursor.get(norm, 0)
            if pos < len(bucket):
                partner_idx = bucket[pos]
                b_cursor[norm] = pos + 1

        if partner_idx is not None:
            b_consumed[partner_idx] = 1
            out_b, conf_b = _shape(records_b[partner_idx], mapping_set_counterparty)
            merged = merge_by_confidence(out_a, conf_a, out_b, conf_b)
            combined.append({"_source_org": f"{name_a} + {name_b}", **merged})
            matched_count += 1
        else:
            combined.append({"_source_org": name_a, **out_a})
            unmatched_a += 1

    unmatched_b = 0
    for i, rec_b in enumerate(records_b):
        if b_consumed[i]:
            continue
        out_b, _ = _shape(rec_b, mapping_set_counterparty)
        combined.append({"_source_org": name_b, **out_b})
        unmatched_b += 1

    stats = {
        "matched_count": matched_count,
        "unmatched_requester_count": unmatched_a,
        "unmatched_counterparty_count": unmatched_b,
        "join_type": "full_outer",
        "requester_record_count": len(records_a),
        "counterparty_record_count": len(records_b),
    }
    return combined, transform_errors, stats


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

    if conn.get("mode") == "joined":
        combined, transform_errors, stats = _exchange_joined(
            conn, requester_ds, counterparty_ds, requester_org, counterparty_org
        )
    else:
        combined, transform_errors = _exchange_concat(
            conn, requester_ds, counterparty_ds, requester_org, counterparty_org
        )
        stats = {"matched_count": len(combined)}

    envelope = xroad_adapter.secure_exchange_envelope(conn["_id"], {"records": combined}, conn["requester_org_id"], conn["counterparty_org_id"])
    _log_audit(db, conn["_id"], "xroad_secure_exchange", {**envelope, **stats})

    lineage = [
        {k: c.get(k) for k in ("output_field", "kind", "field_a", "field_b", "confidence", "level")}
        for c in (conn.get("reconciliation_report") or [])
    ]
    return schemas_api.ExchangeResult(
        connection_id=conn["_id"],
        combined_record_count=len(combined),
        combined_dataset=combined,
        transform_errors=transform_errors,
        xroad_envelope=envelope,
        matched_count=stats.get("matched_count", 0),
        unmatched_requester_count=stats.get("unmatched_requester_count", 0),
        unmatched_counterparty_count=stats.get("unmatched_counterparty_count", 0),
        column_lineage=lineage,
    )


@app.get("/api/connections/{conn_id}/audit", response_model=list[schemas_api.AuditEntry])
def connection_audit(
    conn_id: str,
    org: dict = Depends(get_current_org),
    db=Depends(get_db),
):
    conn = _get_connection_for_party(conn_id, org, db)
    return _audit_entries(conn["_id"], db)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Frontend (vanilla HTML/JS) served at /app/
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/app", StaticFiles(directory=_frontend_dir, html=True), name="frontend")

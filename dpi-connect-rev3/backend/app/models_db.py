"""
Mongo document shapes.

MongoDB is schemaless, so these aren't ORM classes anymore — they're
small factory functions that build the dict each collection stores.
Every document uses `_id` as its primary key (a uuid4 string — the same
IDs the API has always handed out), so `_id` *is* `id` as far as the API
is concerned.

Core workflow this schema supports (unchanged from the original SQLite
version):

  1. An Organization registers and uploads a Dataset (sample records ->
     we auto-discover its Schema).
  2. Org A (the "requester") creates a ConnectionRequest against Org B
     (the "counterparty"), pointing at A's own dataset (what A already
     has / the shape A wants back) and B's dataset (the enrichment data
     A is asking for). We immediately compute a similarity score by
     running the AI mapping engine between the two schemas.
  3. B reviews the request (they can see the schema-level similarity
     report, never A's or their own raw records, before deciding) and
     submits a Consent — approve or reject, with a signed agreement.
  4. On approval, the connection goes ACTIVE via the X-Road adapter's
     simulated secure handshake, logged to AuditLog.

  NEW: between steps 2 and 3 the platform ADMIN reviews both organizations'
  field mappings and their confidence scores and decides whether the
  connection may be established at all:

      PENDING_ADMIN_REVIEW -> (admin approves) -> PENDING_CONSENT -> APPROVED -> ACTIVE
                           -> (admin rejects)  -> REJECTED_BY_ADMIN
                                                  PENDING_CONSENT -> REJECTED (B declines)
  5. A can then call the exchange endpoint, which pulls B's records,
     transforms them into A's schema using the approved mapping, and
     merges them with A's own records into one combined dataset — every
     exchange is signed and appended to AuditLog.

Collections:
  organizations         name, contact_email, api_key
  datasets               org_id, name, sample_records, schema_json
  connection_requests    requester/counterparty org+dataset ids, status,
                          similarity_score, mapping_set_json
  consents                connection_id (unique), decision, signatory_name,
                          agreement_text
  audit_log              connection_id, event_type, detail_json,
                          xroad_signature
"""

from __future__ import annotations

from typing import Any, Optional

from app.database import new_id, utcnow

ORGANIZATIONS = "organizations"
DATASETS = "datasets"
CONNECTION_REQUESTS = "connection_requests"
CONSENTS = "consents"
AUDIT_LOG = "audit_log"


def new_organization(name: str, contact_email: str, api_key: str) -> dict[str, Any]:
    return {
        "_id": new_id(),
        "name": name,
        "contact_email": contact_email,
        "api_key": api_key,
        "created_at": utcnow(),
    }


def new_dataset(
    org_id: str, name: str, sample_records: list[dict], schema_json: dict
) -> dict[str, Any]:
    return {
        "_id": new_id(),
        "org_id": org_id,
        "name": name,
        "sample_records": sample_records,
        "schema_json": schema_json,
        "created_at": utcnow(),
    }


def new_connection_request(
    requester_org_id: str,
    counterparty_org_id: str,
    requester_dataset_id: str,
    counterparty_dataset_id: str,
    purpose: str,
    similarity_score: Optional[float],
    mapping_set_json: Optional[dict],
    mode: str = "concat",
    requester_join_field: Optional[str] = None,
    counterparty_join_field: Optional[str] = None,
    output_schema_json: Optional[dict] = None,
    mapping_set_counterparty_json: Optional[dict] = None,
    reconciliation_report: Optional[list[dict]] = None,
    scoring_summary: Optional[dict] = None,
) -> dict[str, Any]:
    now = utcnow()
    return {
        "_id": new_id(),
        "requester_org_id": requester_org_id,
        "counterparty_org_id": counterparty_org_id,
        "requester_dataset_id": requester_dataset_id,
        "counterparty_dataset_id": counterparty_dataset_id,
        "purpose": purpose,
        # PENDING_ADMIN_REVIEW -> PENDING_CONSENT -> APPROVED -> ACTIVE   (happy path)
        #                      -> REJECTED_BY_ADMIN                       (admin declines)
        #                                         -> REJECTED             (B declines)
        "status": "PENDING_ADMIN_REVIEW",
        "similarity_score": similarity_score,
        # "concat": mapping_set_json maps counterparty schema -> requester schema;
        #           exchange() appends reshaped B records after A's own records.
        # "joined": mapping_set_json maps requester schema -> output_schema_json,
        #           mapping_set_counterparty_json maps counterparty schema ->
        #           the same output_schema_json; exchange() joins A/B records on
        #           requester_join_field/counterparty_join_field and deep-merges
        #           both mapped records into one, per the shared output shape.
        "mode": mode,
        "mapping_set_json": mapping_set_json,
        "requester_join_field": requester_join_field,
        "counterparty_join_field": counterparty_join_field,
        "output_schema_json": output_schema_json,
        "mapping_set_counterparty_json": mapping_set_counterparty_json,
        # One entry per output column: which A/B fields feed it, its confidence,
        # the evidence behind that confidence, and whether a human should look.
        "reconciliation_report": reconciliation_report or [],
        "scoring_summary": scoring_summary or {},
        "admin_review": None,   # {decision, reviewer_name, notes, decided_at}
        "created_at": now,
        "updated_at": now,
    }


def new_consent(
    connection_id: str, decision: str, signatory_name: str, agreement_text: str
) -> dict[str, Any]:
    return {
        "_id": new_id(),
        "connection_id": connection_id,
        "decision": decision,  # "approved" | "rejected"
        "signatory_name": signatory_name,
        "agreement_text": agreement_text,
        "agreement_filename": None,
        "decided_at": utcnow(),
    }


def new_audit_entry(
    connection_id: Optional[str], event_type: str, detail: dict, xroad_signature: str
) -> dict[str, Any]:
    """
    Every state-changing / data-touching event, signed the way a real
    X-Road Security Server would sign inter-organization messages. See
    app/xroad_adapter.py — that module owns the signing; this factory
    just shapes what it produced for storage.
    """
    return {
        "_id": new_id(),
        "connection_id": connection_id,
        "event_type": event_type,
        "detail_json": detail,
        "xroad_signature": xroad_signature,
        "created_at": utcnow(),
    }

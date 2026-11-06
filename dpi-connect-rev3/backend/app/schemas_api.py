from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# --- Organizations ----------------------------------------------------

class OrgRegisterRequest(BaseModel):
    name: str
    contact_email: str


class OrgRegisterResponse(BaseModel):
    org_id: str
    name: str
    api_key: str = Field(description="Shown once — store it, it's your auth token.")


class OrgPublic(BaseModel):
    id: str
    name: str


# --- Datasets -----------------------------------------------------------

class DatasetUploadRequest(BaseModel):
    name: str
    sample_records: list[dict[str, Any]] = Field(min_length=1)


class DatasetSummary(BaseModel):
    id: str
    name: str
    field_names: list[str]
    field_count: int
    record_count: int
    created_at: datetime


class DatasetDetail(DatasetSummary):
    dataset_schema: dict[str, Any]


class DatasetColumn(BaseModel):
    name: str
    type: str
    value_kind: str
    non_null: int
    distinct: int


class DatasetPreview(BaseModel):
    format: str
    record_count: int
    columns: list[DatasetColumn]
    preview: list[dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)


# --- Connections ----------------------------------------------------

class ConnectionCreateRequest(BaseModel):
    counterparty_org_id: str
    requester_dataset_id: str
    counterparty_dataset_id: str
    purpose: str

    # --- Optional "joined output" mode -----------------------------------
    # Default behaviour (all three omitted) is unchanged: B's records get
    # reshaped into A's own dataset schema and appended to A's records.
    #
    # Provide all three to instead JOIN matching records from both sides on
    # a shared key and MERGE them into one record per `output_fields` shape.
    # `output_fields` are dot-paths, so the result can be nested, e.g.
    # ["property.property_id", "property.location.district",
    #  "financials.tax_amount"] produces {"property": {...}, "financials": {...}}.
    requester_join_field: Optional[str] = Field(
        default=None, description="Field path in the requester's own dataset used to match records, e.g. 'survey_number'."
    )
    counterparty_join_field: Optional[str] = Field(
        default=None, description="Field path in the counterparty's dataset that corresponds to the same key, e.g. 'property_reference'."
    )
    output_fields: Optional[list[str]] = Field(
        default=None, description="Dot-paths defining the desired combined/nested output shape. "
                                   "Omit this to auto-reconcile: redundant fields (same value across "
                                   "joined records, or a declared alias) collapse into one; everything "
                                   "else is kept as its own field (full outer join of columns)."
    )
    field_aliases: Optional[dict[str, str]] = Field(
        default=None, description="Only used in auto-reconcile mode (output_fields omitted). Declares "
                                   "field-name pairs that mean the same thing but can't be inferred "
                                   "automatically, e.g. {'name': 'person_name'}."
    )


class MappingSummary(BaseModel):
    target_field: str
    source_fields: list[str]
    transform_type: str
    confidence: float
    rationale: Optional[str] = None


class ConnectionSummary(BaseModel):
    id: str
    requester_org_id: str
    requester_org_name: str
    counterparty_org_id: str
    counterparty_org_name: str
    purpose: str
    status: str
    similarity_score: Optional[float]
    created_at: datetime
    updated_at: datetime


class ConnectionDetail(ConnectionSummary):
    scoring_summary: dict[str, Any] = Field(default_factory=dict)
    admin_review: Optional[dict[str, Any]] = None
    mappings: list[MappingSummary]
    low_confidence_fields: list[str]
    mode: str = "concat"  # "concat" | "joined"
    output_fields: Optional[list[str]] = None
    requester_join_field: Optional[str] = None
    counterparty_join_field: Optional[str] = None
    reconciliation_report: list[dict[str, Any]] = Field(
        default_factory=list,
        description="One entry per output column: the A/B fields behind it, its confidence, "
                    "the score components and the evidence, and whether it needs human review.",
    )


class ConsentRequest(BaseModel):
    agreed: bool
    signatory_name: str
    agreement_text: str = Field(
        description="Typed agreement / terms the counterparty is signing off on."
    )


class AuditEntry(BaseModel):
    id: str
    event_type: str
    detail: dict[str, Any]
    xroad_signature: str
    created_at: datetime


class WhoAmI(BaseModel):
    role: Literal["admin", "org"]
    id: str
    name: str


class AdminDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer_name: str = Field(min_length=1)
    notes: str = ""


class AdminConnectionRow(ConnectionSummary):
    scoring_summary: dict[str, Any] = Field(default_factory=dict)
    admin_review: Optional[dict[str, Any]] = None


class AdminDatasetView(BaseModel):
    org_name: str
    dataset_name: str
    record_count: int
    fields: list[DatasetColumn]


class AdminConnectionReview(ConnectionDetail):
    requester_dataset: AdminDatasetView
    counterparty_dataset: AdminDatasetView
    audit: list[AuditEntry]


class ExchangeResult(BaseModel):
    connection_id: str
    combined_record_count: int
    combined_dataset: list[dict[str, Any]]
    transform_errors: list[dict[str, str]]
    xroad_envelope: dict[str, Any]
    # Only meaningful in "joined" mode; zero/record_count in "concat" mode.
    matched_count: int = 0
    unmatched_requester_count: int = 0
    unmatched_counterparty_count: int = 0
    # Which requester/counterparty fields fed each output column, and how sure we were.
    column_lineage: list[dict[str, Any]] = Field(default_factory=list)

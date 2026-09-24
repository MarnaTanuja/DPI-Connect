from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

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


# --- Connections ----------------------------------------------------

class ConnectionCreateRequest(BaseModel):
    counterparty_org_id: str
    requester_dataset_id: str
    counterparty_dataset_id: str
    purpose: str


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
    mappings: list[MappingSummary]
    low_confidence_fields: list[str]


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


class ExchangeResult(BaseModel):
    connection_id: str
    combined_record_count: int
    combined_dataset: list[dict[str, Any]]
    transform_errors: list[dict[str, str]]
    xroad_envelope: dict[str, Any]

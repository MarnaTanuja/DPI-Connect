"""
Core data models for the AI Schema Mapping Engine.

These model the two things every mapping run revolves around:
  - a Schema (source or target), made of FieldSchema entries
  - a Mapping, the AI-generated (or human-approved) correspondence
    between one target field and one-or-more source fields.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Schema representation
# --------------------------------------------------------------------------

class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    OBJECT = "object"
    ARRAY = "array"
    UNKNOWN = "unknown"


class FieldSchema(BaseModel):
    """One field in a registry/service schema."""
    name: str
    path: str = Field(
        description="Dot-path within the record, e.g. 'address.postal_code'"
    )
    type: FieldType = FieldType.UNKNOWN
    description: Optional[str] = None
    sample_values: list[Any] = Field(default_factory=list)
    nullable: bool = True


class Schema(BaseModel):
    """A full schema for a DPI subsystem/service, at a point in time."""
    subsystem_id: str
    service_name: str
    version: int = 1
    fields: list[FieldSchema]
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def field_by_path(self, path: str) -> Optional[FieldSchema]:
        return next((f for f in self.fields if f.path == path), None)


# --------------------------------------------------------------------------
# Mapping representation
# --------------------------------------------------------------------------

class TransformType(str, Enum):
    DIRECT = "direct"                # copy value as-is
    TYPE_COERCION = "type_coercion"  # cast/reformat a single value
    FIELD_SPLIT = "field_split"      # one source field -> many target fields
    FIELD_MERGE = "field_merge"      # many source fields -> one target field
    NESTED_MAP = "nested_map"        # remap a nested object/array structure
    CONSTANT = "constant"            # target gets a fixed/default value
    UNMAPPED = "unmapped"            # no confident mapping found


class ConflictStrategy(str, Enum):
    LATEST_WINS = "latest_wins"
    SOURCE_PRIORITY = "source_priority"
    MANUAL_REVIEW = "manual_review"


class FieldMapping(BaseModel):
    """
    One AI-generated correspondence: how to produce `target_path`
    from the given `source_paths`, plus a confidence score.
    """
    source_paths: list[str]
    target_path: str
    transform_type: TransformType
    # transform_params holds transform-specific config, e.g.:
    #   TYPE_COERCION -> {"target_type": "date", "format": "%Y-%m-%d"}
    #   FIELD_SPLIT   -> {"separator": " ", "target_order": ["first_name","last_name"]}
    #   FIELD_MERGE   -> {"joiner": " "}
    #   NESTED_MAP    -> {"child_mappings": [...]}  (recursive)
    #   CONSTANT      -> {"value": "..."}
    transform_params: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: Optional[str] = None
    generated_by: str = "llm"  # "llm" | "human" | "fuzzy_fallback"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MappingSet(BaseModel):
    """The full set of mappings produced for one source->target schema pair."""
    id: str
    source_subsystem: str
    target_subsystem: str
    source_schema_version: int
    target_schema_version: int
    mappings: list[FieldMapping]
    status: str = "pending_review"  # pending_review | approved | rejected | superseded
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def low_confidence_mappings(self, threshold: float = 0.75) -> list[FieldMapping]:
        return [m for m in self.mappings if m.confidence < threshold]


class SchemaDiff(BaseModel):
    """Result of comparing two versions of the same schema (drift detection)."""
    subsystem_id: str
    old_version: int
    new_version: int
    added_fields: list[str]
    removed_fields: list[str]
    changed_type_fields: list[str]
    affected_mapping_ids: list[str] = Field(default_factory=list)

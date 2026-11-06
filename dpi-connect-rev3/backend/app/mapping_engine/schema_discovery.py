"""
Schema discovery: infer a Schema from sample JSON records coming off a
subsystem's X-Road service response. Intern 15's X-Road adapter is the
intended caller — it fetches sample responses, hands them here, and gets
back a catalogued Schema to feed into the mapping engine.
"""

from __future__ import annotations

from typing import Any

from app.mapping_engine.schema_models import FieldSchema, FieldType, Schema


def _infer_type(value: Any) -> FieldType:
    if value is None:
        return FieldType.UNKNOWN
    if isinstance(value, bool):
        return FieldType.BOOLEAN
    if isinstance(value, int):
        return FieldType.INTEGER
    if isinstance(value, float):
        return FieldType.NUMBER
    if isinstance(value, dict):
        return FieldType.OBJECT
    if isinstance(value, list):
        return FieldType.ARRAY
    if isinstance(value, str):
        return FieldType.STRING
    return FieldType.UNKNOWN


def _walk(record: dict[str, Any], prefix: str = "") -> dict[str, FieldSchema]:
    fields: dict[str, FieldSchema] = {}
    for key, value in record.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            fields.update(_walk(value, path))
            continue
        ftype = _infer_type(value)
        fields[path] = FieldSchema(
            name=key,
            path=path,
            type=ftype,
            sample_values=[value] if value is not None else [],
            nullable=value is None,
        )
    return fields


def discover_schema(
    subsystem_id: str,
    service_name: str,
    sample_records: list[dict[str, Any]],
    version: int = 1,
) -> Schema:
    """
    Build a Schema by merging field info across several sample records
    (so sample_values and nullability reflect real variation, not just
    one lucky/unlucky record).
    """
    merged: dict[str, FieldSchema] = {}
    for record in sample_records:
        for path, field in _walk(record).items():
            if path not in merged:
                merged[path] = field
            else:
                existing = merged[path]
                existing.sample_values = list(
                    dict.fromkeys(existing.sample_values + field.sample_values)
                )[:5]
                if field.nullable:
                    existing.nullable = True
                if existing.type == FieldType.UNKNOWN:
                    existing.type = field.type

    return Schema(
        subsystem_id=subsystem_id,
        service_name=service_name,
        version=version,
        fields=list(merged.values()),
    )

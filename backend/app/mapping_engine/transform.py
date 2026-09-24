"""
Transformation runtime.

Takes a MappingSet (source_path(s) -> target_path with a transform_type +
transform_params) and applies it to an actual data record, producing the
target-shaped record. This is what runs on every real data-exchange call
once a mapping has been approved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.mapping_engine.schema_models import FieldMapping, MappingSet, TransformType


class TransformError(Exception):
    """Raised when a single field mapping cannot be applied to a record."""
    def __init__(self, mapping: FieldMapping, reason: str):
        self.mapping = mapping
        self.reason = reason
        super().__init__(f"{mapping.target_path}: {reason}")


# --------------------------------------------------------------------------
# Dot-path get/set helpers (support nested objects, e.g. "address.postal_code")
# --------------------------------------------------------------------------

def _get_path(record: dict[str, Any], path: str) -> Any:
    node: Any = record
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _set_path(record: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = record
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


# --------------------------------------------------------------------------
# Per-transform-type handlers
# --------------------------------------------------------------------------

def _coerce(value: Any, target_type: str, fmt: str | None = None) -> Any:
    if value is None:
        return None
    if target_type == "integer":
        return int(value)
    if target_type == "number":
        return float(value)
    if target_type == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "y")
        return bool(value)
    if target_type == "string":
        return str(value)
    if target_type in ("date", "datetime"):
        if isinstance(value, (datetime,)):
            dt = value
        else:
            # try the given format first, then a couple of common fallbacks
            candidates = [fmt] if fmt else []
            candidates += ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"]
            dt = None
            for candidate in candidates:
                if not candidate:
                    continue
                try:
                    dt = datetime.strptime(str(value), candidate)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise ValueError(f"could not parse date value '{value}'")
        return dt.date().isoformat() if target_type == "date" else dt.isoformat()
    return value


def apply_mapping(record: dict[str, Any], mapping: FieldMapping) -> Any:
    """Compute the value for mapping.target_path given a source record."""
    tt = mapping.transform_type
    params = mapping.transform_params

    if tt == TransformType.UNMAPPED:
        raise TransformError(mapping, "no source mapping available")

    if tt == TransformType.CONSTANT:
        return params.get("value")

    if tt == TransformType.DIRECT:
        if not mapping.source_paths:
            raise TransformError(mapping, "direct mapping has no source_paths")
        return _get_path(record, mapping.source_paths[0])

    if tt == TransformType.TYPE_COERCION:
        raw = _get_path(record, mapping.source_paths[0])
        try:
            return _coerce(raw, params.get("target_type", "string"), params.get("format"))
        except (ValueError, TypeError) as e:
            raise TransformError(mapping, f"coercion failed: {e}")

    if tt == TransformType.FIELD_SPLIT:
        raw = _get_path(record, mapping.source_paths[0])
        if raw is None:
            return None
        separator = params.get("separator", " ")
        target_order = params.get("target_order", [])
        parts = str(raw).split(separator)
        # target_path names the *specific* sub-field this mapping is producing;
        # the LLM emits one FieldMapping per resulting target field, each
        # carrying the same source + separator but a different "part_index".
        idx = params.get("part_index")
        if idx is None:
            # not told which part -> best effort: whole split result as a list
            return parts
        return parts[idx] if idx < len(parts) else None

    if tt == TransformType.FIELD_MERGE:
        joiner = params.get("joiner", " ")
        values = [str(_get_path(record, p) or "") for p in mapping.source_paths]
        return joiner.join(v for v in values if v)

    if tt == TransformType.NESTED_MAP:
        child_mappings = [FieldMapping(**cm) if isinstance(cm, dict) else cm
                           for cm in params.get("child_mappings", [])]
        nested_result: dict[str, Any] = {}
        for cm in child_mappings:
            try:
                value = apply_mapping(record, cm)
            except TransformError:
                value = None
            _set_path(nested_result, cm.target_path, value)
        return nested_result

    raise TransformError(mapping, f"unsupported transform_type '{tt}'")


def transform_record(
    record: dict[str, Any],
    mapping_set: MappingSet,
    skip_unmapped: bool = True,
    min_confidence: float = 0.0,
) -> tuple[dict[str, Any], list[TransformError]]:
    """
    Apply every mapping in `mapping_set` to `record`.

    Returns (transformed_record, errors). Mappings below `min_confidence`
    or that raise a TransformError are skipped (target field left absent)
    rather than aborting the whole record, so partial data still flows.
    """
    output: dict[str, Any] = {}
    errors: list[TransformError] = []

    for mapping in mapping_set.mappings:
        if mapping.confidence < min_confidence:
            errors.append(TransformError(mapping, "below min_confidence threshold"))
            continue
        try:
            value = apply_mapping(record, mapping)
            _set_path(output, mapping.target_path, value)
        except TransformError as e:
            if skip_unmapped and mapping.transform_type == TransformType.UNMAPPED:
                errors.append(e)
                continue
            errors.append(e)

    return output, errors

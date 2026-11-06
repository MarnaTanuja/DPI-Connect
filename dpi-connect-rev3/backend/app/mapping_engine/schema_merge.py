"""
Glue between the field matcher and the rest of the app.

`field_matcher.reconcile()` decides WHICH fields of org A and org B are the
same concept and how sure it is. This module turns that decision into the
two MappingSets the transform runtime executes (A -> output, B -> output),
and builds a comparable report for the explicit `output_fields` mode, so the
admin review screen can treat both modes the same way.

Every FieldMapping carries the confidence of the *column it feeds*; nothing
here hard-codes 1.0 any more.
"""

from __future__ import annotations

import uuid

from app.mapping_engine.field_matcher import CONFIDENCE_HIGH, Reconciliation, level_for, summarise
from app.mapping_engine.schema_models import FieldMapping, MappingSet, Schema, TransformType


def _mapping_set(
    path_map: dict[str, str],
    confidence_by_output: dict[str, float],
    rationale_by_output: dict[str, str],
    source_subsystem: str, target_subsystem: str,
    source_version: int, target_version: int,
) -> MappingSet:
    mappings = [
        FieldMapping(
            source_paths=[src],
            target_path=tgt,
            transform_type=TransformType.DIRECT,
            confidence=confidence_by_output.get(tgt, 0.0),
            rationale=rationale_by_output.get(tgt),
            generated_by="evidence_scoring",
        )
        for src, tgt in path_map.items()
    ]
    return MappingSet(
        id=str(uuid.uuid4()),
        source_subsystem=source_subsystem, target_subsystem=target_subsystem,
        source_schema_version=source_version, target_schema_version=target_version,
        mappings=mappings,
    )


def mapping_sets_from_reconciliation(
    rec: Reconciliation, schema_a: Schema, schema_b: Schema,
) -> tuple[MappingSet, MappingSet]:
    conf = rec.confidence_by_output()
    why = {c["output_field"]: c["reason"] for c in rec.columns}
    out = rec.output_schema
    return (
        _mapping_set(rec.path_map_a, conf, why, schema_a.subsystem_id, out.subsystem_id, schema_a.version, out.version),
        _mapping_set(rec.path_map_b, conf, why, schema_b.subsystem_id, out.subsystem_id, schema_b.version, out.version),
    )


def report_from_mapping_sets(
    ms_a: MappingSet, ms_b: MappingSet, output_schema: Schema,
    n_fields_a: int, n_fields_b: int,
) -> tuple[list[dict], dict]:
    """Explicit `output_fields` mode: LLM/fuzzy mappings -> the same column report shape."""
    by_a = {m.target_path: m for m in ms_a.mappings if m.source_paths}
    by_b = {m.target_path: m for m in ms_b.mappings if m.source_paths}
    columns: list[dict] = []
    for f in output_schema.fields:
        ma, mb = by_a.get(f.path), by_b.get(f.path)
        confs = [m.confidence for m in (ma, mb) if m is not None]
        conf = round(sum(confs) / len(confs), 3) if confs else 0.0
        kind = "matched" if ma and mb else "only_a" if ma else "only_b" if mb else "unmapped"
        evidence = [m.rationale for m in (ma, mb) if m is not None and m.rationale] or ["no source field mapped"]
        columns.append({
            "output_field": f.path, "canonical": f.path, "kind": kind,
            "field_a": ma.source_paths[0] if ma else None,
            "field_b": mb.source_paths[0] if mb else None,
            "confidence": conf, "level": level_for(conf), "needs_review": conf < CONFIDENCE_HIGH,
            "components": {}, "evidence": evidence, "nearest": None, "declared_alias": False,
            "reason": "; ".join(evidence[:2]),
        })
    return columns, summarise(columns, n_fields_a, n_fields_b, keyed=False)

"""
LLM-based schema mapping engine.

Given a source Schema and a target Schema (plus optional few-shot examples
from previously-approved mappings), this asks Mistral to propose a
FieldMapping for every target field, with a transform type, transform
params, a confidence score, and a short rationale.

If no MISTRAL_API_KEY is set, falls back to a deterministic fuzzy-match
mapper (difflib) so the rest of the pipeline (transform runtime, conflict
resolution, API) can be developed and tested without hitting the network.
"""

from __future__ import annotations

import json
import os
import uuid
from difflib import SequenceMatcher
from typing import Optional

import requests

from app.mapping_engine.schema_models import FieldMapping, FieldSchema, MappingSet, Schema, TransformType

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
# "mistral-large-latest" is the strongest reasoning model, best for this task.
# Swap to "mistral-small-latest" for a cheaper/faster option once prompts are tuned.
MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")

SYSTEM_PROMPT = """You are a data-integration expert mapping fields between two \
schemas from different government/enterprise registries (a DPI interoperability \
scenario, similar to X-Road cross-subsystem data exchange).

For EVERY field in the TARGET schema, propose the best mapping from the SOURCE \
schema fields. Consider field names, descriptions, types, and sample values. \
Handle these transform types:
- "direct": source and target are semantically identical, same shape.
- "type_coercion": same concept, different type/format (e.g. string date -> ISO date).
- "field_split": one source field must be split into this target field \
(e.g. "full_name" -> "first_name"). List ALL source paths that participate.
- "field_merge": multiple source fields combine into this one target field.
- "nested_map": target is a nested object/array requiring structural remapping.
- "constant": no reasonable source exists; propose a sensible default or null.
- "unmapped": you cannot confidently map this field at all.

Return ONLY a JSON object (no markdown fences, no prose) of the shape:
{
  "mappings": [
    {
      "source_paths": ["<source field path>", ...],
      "target_path": "<target field path>",
      "transform_type": "<one of the types above>",
      "transform_params": { ... },
      "confidence": <float 0.0-1.0>,
      "rationale": "<one sentence>"
    },
    ...
  ]
}

Confidence guidance: 0.9-1.0 exact/obvious matches; 0.6-0.89 plausible but \
worth a quick human glance; below 0.6 genuinely uncertain, route to manual review.
Be conservative — an overconfident wrong mapping is worse than a low-confidence \
flag for manual review.
"""


def _schema_to_prompt_block(schema: Schema, label: str) -> str:
    lines = [f"{label} SCHEMA (subsystem={schema.subsystem_id}, "
              f"service={schema.service_name}, version={schema.version}):"]
    for f in schema.fields:
        lines.append(
            f'- path="{f.path}" type={f.type.value} '
            f'nullable={f.nullable} description="{f.description or ""}" '
            f'samples={f.sample_values[:3]}'
        )
    return "\n".join(lines)


def _few_shot_block(examples: list[FieldMapping]) -> str:
    if not examples:
        return ""
    rendered = [m.model_dump(mode="json", include={
        "source_paths", "target_path", "transform_type", "transform_params"
    }) for m in examples[:8]]
    return (
        "\nPreviously human-approved mappings for this subsystem pair "
        "(use as style/precedent, not gospel):\n" + json.dumps(rendered, indent=2)
    )


def _call_mistral(prompt: str) -> str:
    """Thin wrapper around the Mistral chat completions API. Raises on any
    HTTP/network/auth failure so the caller can fall back to fuzzy matching."""
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY is not set")

    resp = requests.post(
        MISTRAL_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            # Forces the response body to be a single JSON value — Mistral
            # still needs "JSON" mentioned in the prompt (it is, in
            # SYSTEM_PROMPT), but this stops it from wrapping in prose/fences.
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 4000,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _parse_llm_json(raw: str) -> list[dict]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
    parsed = json.loads(cleaned)
    # Mistral's JSON mode requires a top-level object, so we asked for
    # {"mappings": [...]}; unwrap it. Tolerate a bare array too, in case
    # a future model/prompt tweak returns one directly.
    if isinstance(parsed, dict):
        return parsed.get("mappings", [])
    return parsed


# --------------------------------------------------------------------------
# Fallback: deterministic fuzzy matcher (no network / no API key needed)
# --------------------------------------------------------------------------

def _fuzzy_fallback(source: Schema, target: Schema) -> list[FieldMapping]:
    mappings = []
    for tf in target.fields:
        best: Optional[FieldSchema] = None
        best_score = 0.0
        for sf in source.fields:
            score = SequenceMatcher(None, tf.name.lower(), sf.name.lower()).ratio()
            if score > best_score:
                best, best_score = sf, score

        if best is None or best_score < 0.35:
            mappings.append(FieldMapping(
                source_paths=[],
                target_path=tf.path,
                transform_type=TransformType.UNMAPPED,
                confidence=0.0,
                rationale="No source field name is similar enough (fuzzy fallback).",
                generated_by="fuzzy_fallback",
            ))
            continue

        transform_type = TransformType.DIRECT
        params = {}
        if best.type != tf.type:
            transform_type = TransformType.TYPE_COERCION
            params = {"target_type": tf.type.value}

        mappings.append(FieldMapping(
            source_paths=[best.path],
            target_path=tf.path,
            transform_type=transform_type,
            transform_params=params,
            confidence=round(min(best_score, 0.85), 2),  # cap: fuzzy is never "sure"
            rationale=f"Name similarity {best_score:.2f} to '{best.path}' "
                      f"(fuzzy fallback, no LLM available).",
            generated_by="fuzzy_fallback",
        ))
    return mappings


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def generate_mappings(
    source: Schema,
    target: Schema,
    few_shot_examples: Optional[list[FieldMapping]] = None,
    force_fallback: bool = False,
) -> MappingSet:
    """
    Generate a MappingSet from `source` schema fields to `target` schema fields.

    Tries the LLM first; falls back to fuzzy name matching if no API key is
    configured, the network is unavailable, or `force_fallback=True`.
    """
    mappings: list[FieldMapping]

    if force_fallback or not os.environ.get("MISTRAL_API_KEY"):
        mappings = _fuzzy_fallback(source, target)
    else:
        try:
            prompt = (
                _schema_to_prompt_block(source, "SOURCE") + "\n\n" +
                _schema_to_prompt_block(target, "TARGET") +
                _few_shot_block(few_shot_examples or []) +
                "\n\nReturn the JSON object now."
            )
            raw = _call_mistral(prompt)
            parsed = _parse_llm_json(raw)
            mappings = [FieldMapping(**item, generated_by="llm") for item in parsed]
        except Exception:
            # Network/API failure shouldn't crash the pipeline — degrade gracefully.
            mappings = _fuzzy_fallback(source, target)

    return MappingSet(
        id=str(uuid.uuid4()),
        source_subsystem=source.subsystem_id,
        target_subsystem=target.subsystem_id,
        source_schema_version=source.version,
        target_schema_version=target.version,
        mappings=mappings,
    )

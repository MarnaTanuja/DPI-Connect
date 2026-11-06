"""
Automatic join-key detection.

The app used to require the requester to explicitly name a join field on
each side; without them it silently fell back to "append B's rows after A's"
(which reads like a LEFT JOIN with B's extra rows reshaped into A's shape).

This module picks the join key pair automatically from the actual data, so
a FULL OUTER JOIN is the default with no manual configuration:

  score = |shared values| / min(distinct_a, distinct_b)   (value overlap)
        * uniqueness bonus (keys should identify a record, not a category)
        + small name-similarity nudge as a tiebreak

Everything is hash-based and sampled (no O(n*m) scanning), so it stays fast
on large datasets.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from app.mapping_engine import semantic
from app.mapping_engine.schema_models import Schema
from app.mapping_engine.transform import get_path

MAX_RECORDS_TO_SAMPLE = 5000     # per side; keys are detected from a sample
MIN_OVERLAP = 0.10               # at least 10% of the smaller key space must match
MIN_UNIQUENESS = 0.60            # distinct/non-null ratio — filters out status-like columns
MIN_DISTINCT_VALUES = 2


def _norm_name(name: str) -> str:
    return name.strip().lower().replace("_", "").replace(" ", "").replace("-", "")


def _norm_value(v: Any) -> str | None:
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    s = str(v).strip().lower()
    return s or None


def _value_profile(records: list[dict], path: str) -> tuple[set[str], int]:
    """(distinct normalized values, non-null count) over the sampled records."""
    values: set[str] = set()
    non_null = 0
    for rec in records:
        v = _norm_value(get_path(rec, path))
        if v is None:
            continue
        non_null += 1
        values.add(v)
    return values, non_null


def _spread_sample(records: list[dict]) -> list[dict]:
    """
    Sample evenly across the whole dataset, not just the first N records.
    Taking the head would miss the overlap entirely when the two sides are
    sorted differently (e.g. A holds ids 1..50k and B holds 25k..75k).
    """
    if len(records) <= MAX_RECORDS_TO_SAMPLE:
        return records
    step = len(records) // MAX_RECORDS_TO_SAMPLE
    return records[::step][:MAX_RECORDS_TO_SAMPLE]


def detect_join_fields(
    schema_a: Schema,
    schema_b: Schema,
    records_a: list[dict],
    records_b: list[dict],
) -> tuple[str, str, float, str] | None:
    """
    Returns (path_a, path_b, score, reason) for the best join key pair, or
    None when no field pair shares enough values to join on.
    """
    sample_a = _spread_sample(records_a)
    sample_b = _spread_sample(records_b)
    if not sample_a or not sample_b:
        return None

    profiles_a = {f.path: _value_profile(sample_a, f.path) for f in schema_a.fields}
    profiles_b = {f.path: _value_profile(sample_b, f.path) for f in schema_b.fields}

    best: tuple[str, str, float, str] | None = None

    for fa in schema_a.fields:
        vals_a, non_null_a = profiles_a[fa.path]
        if len(vals_a) < MIN_DISTINCT_VALUES or non_null_a == 0:
            continue
        uniq_a = len(vals_a) / non_null_a
        if uniq_a < MIN_UNIQUENESS:
            continue

        for fb in schema_b.fields:
            vals_b, non_null_b = profiles_b[fb.path]
            if len(vals_b) < MIN_DISTINCT_VALUES or non_null_b == 0:
                continue
            uniq_b = len(vals_b) / non_null_b
            if uniq_b < MIN_UNIQUENESS:
                continue

            shared = len(vals_a & vals_b)
            if shared == 0:
                continue
            overlap = shared / min(len(vals_a), len(vals_b))
            if overlap < MIN_OVERLAP:
                continue

            name_sim = max(
                SequenceMatcher(None, _norm_name(fa.name), _norm_name(fb.name)).ratio(),
                semantic.semantic_similarity(fa.path, fb.path)[0],
            )
            # Prefer real identifiers (id / number / code / reference) over merely
            # unique-looking columns such as names, which are a poor join key.
            ident_bonus = 0.10 if (
                "ident" in semantic.concept_tokens(fa.path) and "ident" in semantic.concept_tokens(fb.path)
            ) else 0.0
            score = overlap * ((uniq_a + uniq_b) / 2) + 0.15 * name_sim + ident_bonus

            if best is None or score > best[2]:
                best = (
                    fa.path, fb.path, round(score, 4),
                    f"auto-detected join key: '{fa.path}' and '{fb.path}' share "
                    f"{shared} values ({overlap:.0%} of the smaller key space), "
                    f"both near-unique per record",
                )

    return best

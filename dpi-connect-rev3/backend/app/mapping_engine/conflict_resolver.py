"""
Conflict resolution + schema versioning.

Two kinds of "conflict" show up in an interoperability engine like this:

1. Mapping conflicts: more than one candidate FieldMapping exists for the
   same target_path (e.g. the LLM re-ran after a schema tweak, or two
   source subsystems both plausibly supply the same target field).
2. Schema drift: a source or target schema changes version, which can
   silently invalidate previously-approved mappings.

Both are handled here so the admin dashboard (Intern 17) has one place
to pull "things that need a human decision" from.
"""

from __future__ import annotations

from typing import Optional

from app.mapping_engine.schema_models import (
    ConflictStrategy,
    FieldMapping,
    Schema,
    SchemaDiff,
)

CONFIDENCE_AUTO_APPROVE_THRESHOLD = 0.9


def resolve_conflict(
    candidates: list[FieldMapping],
    strategy: ConflictStrategy,
    source_priority: Optional[list[str]] = None,
) -> tuple[Optional[FieldMapping], bool]:
    """
    Pick a winning mapping among `candidates` (all mapping the same
    target_path) according to `strategy`.

    Returns (winner_or_None, needs_manual_review).
    """
    if not candidates:
        return None, False

    # MANUAL_REVIEW always routes to a human, even with just one candidate
    # and even at high confidence — it's an explicit "don't auto-apply" policy,
    # not a tiebreaker for actual conflicts.
    if strategy == ConflictStrategy.MANUAL_REVIEW:
        return None, True

    if len(candidates) == 1:
        only = candidates[0]
        return only, only.confidence < CONFIDENCE_AUTO_APPROVE_THRESHOLD

    if strategy == ConflictStrategy.LATEST_WINS:
        winner = max(candidates, key=lambda m: m.created_at)
        return winner, winner.confidence < CONFIDENCE_AUTO_APPROVE_THRESHOLD

    if strategy == ConflictStrategy.SOURCE_PRIORITY:
        priority = source_priority or []

        def rank(m: FieldMapping) -> int:
            # lower index = higher priority; unlisted sources sort last
            for path in m.source_paths:
                subsystem = path.split(".")[0]
                if subsystem in priority:
                    return priority.index(subsystem)
            return len(priority)

        winner = min(candidates, key=rank)
        return winner, winner.confidence < CONFIDENCE_AUTO_APPROVE_THRESHOLD

    if strategy == ConflictStrategy.MANUAL_REVIEW:
        # Never auto-pick; surface all candidates to a human.
        return None, True

    raise ValueError(f"unknown conflict strategy: {strategy}")


def group_by_target(mappings: list[FieldMapping]) -> dict[str, list[FieldMapping]]:
    groups: dict[str, list[FieldMapping]] = {}
    for m in mappings:
        groups.setdefault(m.target_path, []).append(m)
    return groups


def resolve_all(
    mappings: list[FieldMapping],
    strategy: ConflictStrategy,
    source_priority: Optional[list[str]] = None,
) -> tuple[list[FieldMapping], list[str]]:
    """
    Resolve every target-field group in `mappings`.

    Returns (approved_mappings, target_paths_needing_manual_review).
    """
    approved: list[FieldMapping] = []
    needs_review: list[str] = []

    for target_path, group in group_by_target(mappings).items():
        winner, review = resolve_conflict(group, strategy, source_priority)
        if winner and not review:
            approved.append(winner)
        else:
            needs_review.append(target_path)

    return approved, needs_review


# --------------------------------------------------------------------------
# Schema versioning / drift detection
# --------------------------------------------------------------------------

def diff_schemas(old: Schema, new: Schema) -> SchemaDiff:
    """Compare two versions of the same subsystem's schema."""
    old_by_path = {f.path: f for f in old.fields}
    new_by_path = {f.path: f for f in new.fields}

    added = [p for p in new_by_path if p not in old_by_path]
    removed = [p for p in old_by_path if p not in new_by_path]
    changed_type = [
        p for p in old_by_path
        if p in new_by_path and old_by_path[p].type != new_by_path[p].type
    ]

    return SchemaDiff(
        subsystem_id=old.subsystem_id,
        old_version=old.version,
        new_version=new.version,
        added_fields=added,
        removed_fields=removed,
        changed_type_fields=changed_type,
    )


def mappings_affected_by_diff(
    mappings: list[FieldMapping], diff: SchemaDiff
) -> list[FieldMapping]:
    """
    Which existing mappings reference a field that was removed or
    retyped in the new schema version -> these need re-mapping/review.
    """
    stale_paths = set(diff.removed_fields) | set(diff.changed_type_fields)
    return [
        m for m in mappings
        if any(sp in stale_paths for sp in m.source_paths)
        or m.target_path in stale_paths
    ]

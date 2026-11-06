"""
Evidence-based field matching + confidence scoring for the FULL OUTER JOIN.

Why this exists
---------------
Reconciliation used to mark every field "100% confident" because it simply
declared whatever it merged to be correct. This module replaces that with a
score per field pair built from independent signals, so a confidence number
means something an admin can act on.

Signals for a pair (field a from org A, field b from org B)
-----------------------------------------------------------
  lexical      spelling similarity of the names
  semantic     meaning similarity (concept lexicon, qualifier conflicts,
               optional embeddings) - see semantic.py
  structural   do the VALUES look alike?  detected value kind (date / email /
               integer / code ...) and character-shape distribution
  agreement    on rows that join (same entity), do the two values agree?
               Measured as (agree+1)/(n+2) so small samples can't give 100%.
  overlap      share of distinct values the two columns have in common
               (positive evidence only - disjoint populations are normal)

How they combine
----------------
  linguistic   = 0.35*lexical + 0.65*semantic
  schema_score = linguistic * (0.6 + 0.4*structural)
  trust        = how much the data evidence deserves to count: n/(n+2) for n
                 comparable joined rows, scaled by how informative the values
                 are (id-like columns count fully, constants not at all)
  score        = (1-trust)*schema_score + trust*agreement      (joined rows)
               = schema_score pulled up towards overlap          (otherwise)

Guard rails
  * contradiction veto: >=3 joined rows and <50% agreement -> never merged
  * no data evidence -> capped at 0.90; the cap rises to 0.985 with evidence,
    so an inferred mapping is never reported as 100%
  * ambiguity penalty (up to -0.10) when another candidate scores nearly as high

Columns that exist on one side only get a confidence too: how sure we are
that NO counterpart exists (1 - best leftover candidate score, max 0.98).
A near-miss (best candidate >= 0.45, even one already matched elsewhere) is
flagged for human review, as is a same-named pair whose values contradict.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.mapping_engine import semantic
from app.mapping_engine.schema_models import FieldSchema, FieldType, Schema
from app.mapping_engine.transform import get_path, join_key

# ---- tunables (documented in README) -------------------------------------
MERGE_THRESHOLD = 0.70          # pair must score at least this to share a column
CONFIDENCE_HIGH = 0.85          # >= : no review needed
CONFIDENCE_LOW = 0.60           # <  : flagged "low"
REVIEW_BAND_LOW = 0.45          # unmatched field whose nearest candidate is >= this -> near-miss
AMBIGUITY_MARGIN = 0.10
MAX_AMBIGUITY_PENALTY = 0.10
NO_EVIDENCE_CAP = 0.90
MAX_CONFIDENCE = 0.985
MAX_UNIQUE_CONFIDENCE = 0.98
DECLARED_ALIAS_FLOOR = 0.95
MAX_PROFILE_ROWS = 5000
MAX_JOINED_PAIRS = 500
VETO_MIN_ROWS = 3
VETO_AGREEMENT = 0.50

# --------------------------------------------------------------------------
# Value normalisation + profiling
# --------------------------------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")
_DATE_LIKE = re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[\d\s().-]{7,20}$")
_BOOL = {"true", "false", "yes", "no", "y", "n"}


def canon(v: Any) -> Optional[str]:
    """Canonical comparable form of a value, or None when it carries no information."""
    if v is None or isinstance(v, (dict, list)):
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip().casefold()
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)
    if _DATE_LIKE.match(s):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
    return s


def _alnum(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s)


def values_agree(a: str, b: str) -> bool:
    if a == b:
        return True
    xa, xb = _alnum(a), _alnum(b)
    return bool(xa) and xa == xb


def _shape(s: str) -> str:
    out = []
    for ch in s:
        c = "A" if ch.isalpha() else "9" if ch.isdigit() else ch
        if not out or out[-1] != c:
            out.append(c)
    return "".join(out)


def _detect_kind(raw_values: list[Any]) -> str:
    vals = [v for v in raw_values if v is not None and not isinstance(v, (dict, list))]
    if not vals:
        return "unknown"
    n = len(vals)

    def frac(pred) -> float:
        return sum(1 for v in vals if pred(v)) / n

    def is_int(v):
        return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, str) and re.fullmatch(r"[+-]?\d+", v.strip() or "x") is not None)

    def is_num(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return True
        try:
            float(str(v).strip())
            return True
        except ValueError:
            return False

    s_vals = [str(v).strip() for v in vals]
    if frac(lambda v: isinstance(v, bool) or str(v).strip().lower() in _BOOL) >= 0.8:
        return "boolean"
    if frac(is_int) >= 0.8:
        return "integer"
    if frac(is_num) >= 0.8:
        return "number"
    if sum(1 for s in s_vals if _DATE_LIKE.match(s) and any(_try_date(s, f) for f in _DATE_FORMATS)) / n >= 0.8:
        return "date"
    if sum(1 for s in s_vals if _EMAIL.match(s)) / n >= 0.8:
        return "email"
    if sum(1 for s in s_vals if _PHONE.match(s) and sum(c.isdigit() for c in s) >= 7 and re.search(r"[+\s().-]", s)) / n >= 0.8:
        return "phone"
    if sum(1 for s in s_vals if " " in s) / n >= 0.5:
        return "text"
    if sum(1 for s in s_vals if re.search(r"\d", s) and re.search(r"[A-Za-z]", s)) / n >= 0.5:
        return "code"
    return "word"


def _try_date(s: str, fmt: str) -> bool:
    try:
        datetime.strptime(s, fmt)
        return True
    except ValueError:
        return False


@dataclass
class FieldProfile:
    path: str
    raw_count: int = 0
    distinct: set[str] = field(default_factory=set)
    non_null: int = 0
    kind: str = "unknown"
    shapes: dict[str, float] = field(default_factory=dict)

    @property
    def uniqueness(self) -> float:
        return len(self.distinct) / self.non_null if self.non_null else 0.0


def _spread(records: list[dict], cap: int) -> list[dict]:
    if len(records) <= cap:
        return records
    step = max(1, len(records) // cap)
    return records[::step][:cap]


def profile_schema(schema: Schema, records: list[dict]) -> dict[str, FieldProfile]:
    sample = _spread(records, MAX_PROFILE_ROWS)
    profiles: dict[str, FieldProfile] = {}
    for f in schema.fields:
        raws: list[Any] = []
        p = FieldProfile(path=f.path)
        shapes: Counter[str] = Counter()
        for rec in sample:
            raw = get_path(rec, f.path)
            c = canon(raw)
            if c is None:
                continue
            raws.append(raw)
            p.non_null += 1
            p.distinct.add(c)
            if len(raws) <= 300:
                shapes[_shape(c)] += 1
        total = sum(shapes.values())
        p.shapes = {k: v / total for k, v in shapes.items()} if total else {}
        p.kind = _detect_kind(raws[:300]) if raws else "unknown"
        profiles[f.path] = p
    return profiles


# --------------------------------------------------------------------------
# Pair scoring
# --------------------------------------------------------------------------

_KIND_COMPAT = {
    frozenset({"integer", "number"}): 0.85,
    frozenset({"integer", "phone"}): 0.70,
    frozenset({"integer", "code"}): 0.60,
    frozenset({"code", "word"}): 0.55,
    frozenset({"word", "text"}): 0.80,
    frozenset({"code", "text"}): 0.45,
    frozenset({"date", "text"}): 0.35,
    frozenset({"date", "word"}): 0.35,
    frozenset({"date", "code"}): 0.35,
}


def kind_compat(ka: str, kb: str) -> float:
    if ka == kb:
        return 1.0 if ka != "unknown" else 0.6
    if "unknown" in (ka, kb):
        return 0.6
    return _KIND_COMPAT.get(frozenset({ka, kb}), 0.2)


def shape_similarity(pa: FieldProfile, pb: FieldProfile) -> Optional[float]:
    if not pa.shapes or not pb.shapes:
        return None
    return sum(min(w, pb.shapes.get(s, 0.0)) for s, w in pa.shapes.items())


def _schema_type_compat(a: FieldType, b: FieldType) -> float:
    if a == b and a != FieldType.UNKNOWN:
        return 1.0
    if {a, b} <= {FieldType.INTEGER, FieldType.NUMBER}:
        return 0.85
    if FieldType.UNKNOWN in (a, b):
        return 0.6
    return 0.4


def informativeness(vals: list[str]) -> float:
    """
    How much can agreement on these values tell us? 1.0 when every value is
    different (id-like: matching them cannot be a coincidence), 0.0 when they
    are all the same (a constant agrees with everything), in between for
    repetitive columns such as status flags. Based on the chance that two
    random rows share a value, corrected for sample size.
    """
    m = len(vals)
    if m < 2:
        return 0.0
    simpson = sum((c / m) ** 2 for c in Counter(vals).values())
    return max(0.0, min(1.0, 1 - (simpson - 1 / m) / (1 - 1 / m)))


@dataclass
class PairScore:
    path_a: str
    path_b: str
    lexical: float = 0.0
    semantic: float = 0.0
    sem_method: str = "lexicon"
    linguistic: float = 0.0
    structural: float = 0.0
    schema_score: float = 0.0
    compared: int = 0
    agree: int = 0
    agreement: Optional[float] = None
    shared: int = 0
    overlap: Optional[float] = None
    trust: float = 0.0
    score: float = 0.0
    vetoed: bool = False
    evidence: list[str] = field(default_factory=list)

    def components(self) -> dict[str, Any]:
        r = lambda x: None if x is None else round(x, 3)  # noqa: E731
        return {
            "lexical": r(self.lexical), "semantic": r(self.semantic), "semantic_method": self.sem_method,
            "linguistic": r(self.linguistic), "structural": r(self.structural),
            "schema_score": r(self.schema_score),
            "value_agreement": r(self.agreement), "joined_compared": self.compared,
            "value_overlap": r(self.overlap), "shared_values": self.shared,
            "evidence_trust": r(self.trust),
        }


def _schema_part(fa: FieldSchema, fb: FieldSchema, pa: FieldProfile, pb: FieldProfile, ps: PairScore) -> None:
    ps.lexical = semantic.lexical_similarity(fa.path, fb.path)
    ps.semantic, ps.sem_method = semantic.semantic_similarity(fa.path, fb.path)
    ps.linguistic = 0.35 * ps.lexical + 0.65 * ps.semantic

    if pa.kind != "unknown" and pb.kind != "unknown":
        kc = kind_compat(pa.kind, pb.kind)
        sh = shape_similarity(pa, pb)
        ps.structural = kc if sh is None else 0.6 * kc + 0.4 * sh
        ps.evidence.append(f"values: {pa.kind} vs {pb.kind}" + (f", shape similarity {sh:.2f}" if sh is not None else ""))
    else:
        ps.structural = _schema_type_compat(fa.type, fb.type)
    ps.schema_score = ps.linguistic * (0.6 + 0.4 * ps.structural)
    ps.evidence.insert(0, f"names: lexical {ps.lexical:.2f}, semantic {ps.semantic:.2f} ({ps.sem_method})")


def score_pair(
    fa: FieldSchema, fb: FieldSchema, pa: FieldProfile, pb: FieldProfile,
    joined_a: list[Optional[str]], joined_b: list[Optional[str]],
) -> PairScore:
    ps = PairScore(path_a=fa.path, path_b=fb.path)
    _schema_part(fa, fb, pa, pb, ps)

    # ---- agreement on joined rows (same entity on both sides)
    vals_a: list[str] = []
    vals_b: list[str] = []
    for ca, cb in zip(joined_a, joined_b):
        if ca is None or cb is None:
            continue
        ps.compared += 1
        vals_a.append(ca)
        vals_b.append(cb)
        if values_agree(ca, cb):
            ps.agree += 1

    trust_j = 0.0
    if ps.compared:
        ps.agreement = (ps.agree + 1) / (ps.compared + 2)
        informative = min(informativeness(vals_a), informativeness(vals_b))
        trust_j = ps.compared / (ps.compared + 2) * informative
        ps.evidence.append(f"values agree on {ps.agree}/{ps.compared} joined records")

    # ---- overlap of distinct values (positive evidence only)
    d = min(len(pa.distinct), len(pb.distinct))
    trust_c = 0.0
    if d:
        ps.shared = len(pa.distinct & pb.distinct)
        ps.overlap = ps.shared / d
        if ps.shared:
            trust_c = 0.5 * ps.shared / (ps.shared + 3) * min(1.0, (d - 1) / 4)
            ps.evidence.append(f"{ps.shared} of {d} distinct values appear in both columns")

    score = ps.schema_score
    if trust_j > 0:
        ps.trust = trust_j
        score = (1 - trust_j) * ps.schema_score + trust_j * ps.agreement
    elif trust_c > 0 and ps.overlap is not None and ps.overlap > score:
        ps.trust = trust_c
        score = score + trust_c * (ps.overlap - score)

    if ps.compared >= VETO_MIN_ROWS and ps.agree / ps.compared < VETO_AGREEMENT:
        ps.vetoed = True
        score = min(score, 0.45 + 0.1 * (ps.agree / ps.compared))
        ps.evidence.append("values contradict each other on the same records -> kept as separate columns")

    cap = NO_EVIDENCE_CAP + (MAX_CONFIDENCE - NO_EVIDENCE_CAP) * max(trust_j, trust_c)
    if score > cap:
        score = cap
    ps.score = max(0.0, min(score, MAX_CONFIDENCE))
    return ps


def score_join_key(
    fa: FieldSchema, fb: FieldSchema, pa: FieldProfile, pb: FieldProfile, how: str,
) -> PairScore:
    ps = PairScore(path_a=fa.path, path_b=fb.path)
    _schema_part(fa, fb, pa, pb, ps)
    ps.shared = len(pa.distinct & pb.distinct)
    d = min(len(pa.distinct), len(pb.distinct))
    ps.overlap = ps.shared / d if d else 0.0
    uniq = (pa.uniqueness + pb.uniqueness) / 2
    ps.evidence.append(f"join key ({how}): {ps.shared} shared values, {ps.overlap:.0%} of the smaller key space, uniqueness {uniq:.0%}")

    if ps.shared == 0:
        ps.score = min(ps.schema_score, 0.40)
        ps.evidence.append("no shared key values - this key would not join any rows")
        return ps
    support = uniq * (0.6 + 0.4 * min(1.0, ps.overlap / 0.5))
    ps.trust = ps.shared / (ps.shared + 3)
    score = (1 - ps.trust) * ps.schema_score + ps.trust * support
    cap = NO_EVIDENCE_CAP + (MAX_CONFIDENCE - NO_EVIDENCE_CAP) * ps.trust
    ps.score = max(0.0, min(score, cap, MAX_CONFIDENCE))
    return ps


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def level_for(conf: float) -> str:
    return "high" if conf >= CONFIDENCE_HIGH else "review" if conf >= CONFIDENCE_LOW else "low"


@dataclass
class Reconciliation:
    output_schema: Schema
    path_map_a: dict[str, str]
    path_map_b: dict[str, str]
    columns: list[dict[str, Any]]
    summary: dict[str, Any]

    def confidence_by_output(self) -> dict[str, float]:
        return {c["output_field"]: c["confidence"] for c in self.columns}


def reconcile(
    schema_a: Schema, schema_b: Schema,
    records_a: list[dict], records_b: list[dict],
    join_a: Optional[str], join_b: Optional[str],
    field_aliases: Optional[dict[str, str]] = None,
    join_how: str = "auto-detected",
) -> Reconciliation:
    """Match columns of A and B, give every output column an evidence-based confidence."""
    join_fa = schema_a.field_by_path(join_a) if join_a else None
    join_fb = schema_b.field_by_path(join_b) if join_b else None
    if join_a and join_fa is None:
        raise ValueError(f"requester_join_field '{join_a}' not found in requester schema")
    if join_b and join_fb is None:
        raise ValueError(f"counterparty_join_field '{join_b}' not found in counterparty schema")
    keyed = join_fa is not None and join_fb is not None

    prof_a = profile_schema(schema_a, records_a)
    prof_b = profile_schema(schema_b, records_b)
    semantic.prefetch_embeddings([f.path for f in schema_a.fields] + [f.path for f in schema_b.fields])

    joined: list[tuple[dict, dict]] = []
    if keyed:
        b_by_key: dict[Any, dict] = {}
        for rec in records_b:
            k = get_path(rec, join_fb.path)
            if k is not None:
                b_by_key.setdefault(join_key(k), rec)
        for rec in records_a:
            k = get_path(rec, join_fa.path)
            m = b_by_key.get(join_key(k)) if k is not None else None
            if m is not None:
                joined.append((rec, m))
                if len(joined) >= MAX_JOINED_PAIRS:
                    break

    fields_a = [f for f in schema_a.fields if not (keyed and f.path == join_fa.path)]
    fields_b = [f for f in schema_b.fields if not (keyed and f.path == join_fb.path)]

    jvals_a = {f.path: [canon(get_path(ra, f.path)) for ra, _ in joined] for f in fields_a}
    jvals_b = {f.path: [canon(get_path(rb, f.path)) for _, rb in joined] for f in fields_b}

    matrix: dict[tuple[str, str], PairScore] = {}
    by_a: dict[str, list[tuple[float, str]]] = {f.path: [] for f in fields_a}
    by_b: dict[str, list[tuple[float, str]]] = {f.path: [] for f in fields_b}
    for fa in fields_a:
        for fb in fields_b:
            ps = score_pair(fa, fb, prof_a[fa.path], prof_b[fb.path], jvals_a[fa.path], jvals_b[fb.path])
            matrix[(fa.path, fb.path)] = ps
            by_a[fa.path].append((ps.score, fb.path))
            by_b[fb.path].append((ps.score, fa.path))

    alias_pairs: set[tuple[str, str]] = set()
    for k, v in (field_aliases or {}).items():
        alias_pairs.add((_norm_name(k), _norm_name(v)))
        alias_pairs.add((_norm_name(v), _norm_name(k)))

    chosen: dict[str, tuple[str, float, list[str], bool]] = {}   # path_a -> (path_b, conf, evidence, declared)
    used_b: set[str] = set()

    # 1) declared aliases are human assertions: honour them first
    for fa in fields_a:
        for fb in fields_b:
            if fb.path in used_b or fa.path in chosen:
                continue
            if (_norm_name(fa.name), _norm_name(fb.name)) in alias_pairs:
                ps = matrix[(fa.path, fb.path)]
                conf = max(ps.score, DECLARED_ALIAS_FLOOR)
                chosen[fa.path] = (fb.path, min(conf, MAX_CONFIDENCE),
                                   [f"declared alias ('{fa.name}' = '{fb.name}')"] + ps.evidence, True)
                used_b.add(fb.path)

    # 2) greedy one-to-one assignment by descending score
    for (pa_path, pb_path), ps in sorted(matrix.items(), key=lambda kv: -kv[1].score):
        if ps.score < MERGE_THRESHOLD:
            break
        if pa_path in chosen or pb_path in used_b:
            continue
        # ambiguity: how close is the runner-up for either field?
        rivals = [(sc, o) for sc, o in by_a[pa_path] if o != pb_path] + \
                 [(sc, o) for sc, o in by_b[pb_path] if o != pa_path]
        rival = max(rivals, default=(0.0, ""))
        margin = ps.score - rival[0]
        conf = ps.score
        evidence = list(ps.evidence)
        if margin < AMBIGUITY_MARGIN:
            penalty = MAX_AMBIGUITY_PENALTY * (1 - max(0.0, margin) / AMBIGUITY_MARGIN)
            conf = max(0.0, conf - penalty)
            evidence.append(f"ambiguous: another candidate '{rival[1]}' scores {rival[0]:.2f} (-{penalty:.2f})")
        chosen[pa_path] = (pb_path, conf, evidence, False)
        used_b.add(pb_path)

    # ---- assemble output schema + column reports
    output_fields: list[FieldSchema] = []
    path_map_a: dict[str, str] = {}
    path_map_b: dict[str, str] = {}
    columns: list[dict[str, Any]] = []

    def add_output_field(name: str, ftype: FieldType, description: Optional[str]) -> str:
        path, suffix, existing = name, 2, {f.path for f in output_fields}
        while path in existing:
            path = f"{name}_{suffix}"
            suffix += 1
        output_fields.append(FieldSchema(name=path, path=path, type=ftype, description=description))
        return path

    def column(kind: str, out: str, fa: Optional[str], fb: Optional[str], conf: float,
               comps: dict, evidence: list[str], needs_review: bool, nearest: Optional[dict] = None,
               declared: bool = False) -> None:
        columns.append({
            "output_field": out, "canonical": out, "kind": kind,
            "field_a": fa, "field_b": fb,
            "confidence": round(conf, 3), "level": level_for(conf),
            "needs_review": needs_review, "components": comps, "evidence": evidence,
            "nearest": nearest, "declared_alias": declared,
            "reason": "; ".join(evidence[:3]),
        })

    if keyed:
        ps = score_join_key(join_fa, join_fb, prof_a[join_fa.path], prof_b[join_fb.path], join_how)
        out = add_output_field(join_fa.name, join_fa.type, f"join key ({join_fa.path} = {join_fb.path})")
        path_map_a[join_fa.path] = out
        path_map_b[join_fb.path] = out
        column("join_key", out, join_fa.path, join_fb.path, ps.score, ps.components(), ps.evidence,
               ps.score < CONFIDENCE_HIGH)

    for fa in fields_a:
        out = add_output_field(fa.name, fa.type, fa.description)
        path_map_a[fa.path] = out
        if fa.path in chosen:
            pb_path, conf, evidence, declared = chosen[fa.path]
            path_map_b[pb_path] = out
            comps = matrix[(fa.path, pb_path)].components()
            column("matched", out, fa.path, pb_path, conf, comps, evidence,
                   conf < CONFIDENCE_HIGH or any(e.startswith("ambiguous") for e in evidence), declared=declared)
        else:
            taken_b = {pb: pa for pa, (pb, *_rest) in chosen.items()}
            column_unique(columns, "only_a", out, fa.path, None, matrix, fields_b, taken_b, side="a")

    for fb in fields_b:
        if fb.path in used_b:
            continue
        out = add_output_field(fb.name, fb.type, fb.description)
        path_map_b[fb.path] = out
        taken_a = {pa: pb for pa, (pb, *_rest) in chosen.items()}
        column_unique(columns, "only_b", out, None, fb.path, matrix, fields_a, taken_a, side="b")

    output_schema = Schema(
        subsystem_id="combined_output",
        service_name=f"{schema_a.subsystem_id}__{schema_b.subsystem_id}_merged",
        fields=output_fields,
    )
    return Reconciliation(output_schema, path_map_a, path_map_b, columns,
                          summarise(columns, len(schema_a.fields), len(schema_b.fields), keyed))


def column_unique(columns, kind, out, fa, fb, matrix, other_fields, taken, side) -> None:
    """
    Report a column that exists on one side only (passthrough).

    Confidence = how sure we are that NO counterpart exists = 1 - the best
    candidate score over ALL fields on the other side, including ones already
    matched to something else (a second field with the same meaning is a
    duplicate worth a human look, not a clean "unique").
    """
    mine = fa if side == "a" else fb
    other_org = "B" if side == "a" else "A"
    best_score, best_other, contradicts = 0.0, None, None
    for of in other_fields:
        key = (mine, of.path) if side == "a" else (of.path, mine)
        ps = matrix.get(key)
        if ps is None:
            continue
        if ps.score > best_score:
            best_score, best_other = ps.score, of.path
        if ps.vetoed and ps.linguistic >= 0.7 and (contradicts is None or ps.linguistic > contradicts[1]):
            contradicts = (of.path, ps.linguistic, ps.compared, ps.agree)

    conf = min(MAX_UNIQUE_CONFIDENCE, 1.0 - best_score)
    ev = [f"no counterpart in org {other_org}"
          + (f"; closest candidate '{best_other}' scores {best_score:.2f}" if best_other else "")]
    near = best_score >= REVIEW_BAND_LOW
    if contradicts:
        near = True
        ev.append(f"name resembles '{contradicts[0]}' but values disagree on "
                  f"{contradicts[2] - contradicts[3]}/{contradicts[2]} joined records - kept separate")
    elif best_other and best_other in taken and near:
        ev.append(f"'{best_other}' is already matched with '{taken[best_other]}' - this may be a duplicate of the same concept")
    elif near:
        ev.append("near-miss: close to the merge threshold - confirm these really are different fields")
    columns.append({
        "output_field": out, "canonical": out, "kind": kind,
        "field_a": fa, "field_b": fb,
        "confidence": round(conf, 3), "level": "review" if near else "high",
        "needs_review": near, "components": {}, "evidence": ev,
        "nearest": {"field": best_other, "score": round(best_score, 3)} if best_other else None,
        "declared_alias": False, "reason": "; ".join(ev),
    })


def summarise(columns: list[dict], n_fields_a: int, n_fields_b: int, keyed: bool) -> dict[str, Any]:
    paired = [c for c in columns if c["kind"] in ("matched", "join_key")]
    total_fields = n_fields_a + n_fields_b
    mean_conf = round(sum(c["confidence"] for c in paired) / len(paired), 3) if paired else None
    return {
        "matched_columns": len(paired),
        "only_requester_columns": sum(1 for c in columns if c["kind"] == "only_a"),
        "only_counterparty_columns": sum(1 for c in columns if c["kind"] == "only_b"),
        "needs_review": sum(1 for c in columns if c["needs_review"]),
        "low_confidence": sum(1 for c in paired if c["level"] == "low"),
        "mean_match_confidence": mean_conf,
        "min_match_confidence": round(min(c["confidence"] for c in paired), 3) if paired else None,
        # soft Dice coefficient: confidence-weighted share of fields that have a counterpart
        "schema_overlap": round(2 * sum(c["confidence"] for c in paired) / total_fields, 3) if total_fields else 0.0,
        "join_key": next(({"requester_field": c["field_a"], "counterparty_field": c["field_b"],
                           "confidence": c["confidence"]} for c in columns if c["kind"] == "join_key"), None),
        "keyed": keyed,
    }

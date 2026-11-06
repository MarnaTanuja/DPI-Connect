"""
Confidence scoring: scores must reflect evidence, never a blanket 100%.
Tests call the reconciler directly (no API) so each behaviour is isolated.
"""
from app.mapping_engine import semantic
from app.mapping_engine.field_matcher import MAX_CONFIDENCE, MERGE_THRESHOLD, reconcile
from app.mapping_engine.schema_discovery import discover_schema

NAMES = ["Ada Lovelace", "Alan Turing", "Grace Hopper", "Linus Torvalds", "Margaret Hamilton",
         "Tim Berners-Lee", "Edsger Dijkstra", "Barbara Liskov"]


def _reconcile(a, b, ja, jb, **kw):
    sa, sb = discover_schema("a", "a", a), discover_schema("b", "b", b)
    return reconcile(sa, sb, a, b, ja, jb, **kw)


def _col(rec, **match):
    for c in rec.columns:
        if all(c.get(k) == v for k, v in match.items()):
            return c
    raise AssertionError(f"no column matching {match}: {[(c['kind'], c['field_a'], c['field_b']) for c in rec.columns]}")


def _people(n=8):
    a = [{"citizen_id": f"C{i}", "full_name": NAMES[i % 8], "dob": f"19{60 + i}-0{1 + i % 9}-1{i % 9}",
          "blood_group": ["O+", "A-", "B+", "AB+"][i % 4], "phone": f"98765{i:05d}"} for i in range(n)]
    b = [{"owner_id": f"C{i}", "owner_name": NAMES[i % 8],
          "date_of_birth": f"1{i % 9}/0{1 + i % 9}/19{60 + i}",       # dd/mm/yyyy, different format on purpose
          "vehicle_no": f"KA01AA{i:04d}", "mobile_no": f"98765{i:05d}"} for i in range(n)]
    return a, b


def test_scores_come_from_evidence_not_a_blanket_100():
    a, b = _people()
    rec = _reconcile(a, b, "citizen_id", "owner_id")

    confs = [c["confidence"] for c in rec.columns]
    assert max(confs) <= MAX_CONFIDENCE < 1.0          # never a flat 100%
    assert len(set(confs)) > 2                         # and not one repeated number either

    # same concept, different spelling / format -> matched, with a real (<1) score
    for fa, fb in [("full_name", "owner_name"), ("dob", "date_of_birth"), ("phone", "mobile_no")]:
        c = _col(rec, field_a=fa, field_b=fb)
        assert c["kind"] == "matched" and MERGE_THRESHOLD <= c["confidence"] < 1.0, c
        assert c["components"]["value_agreement"] is not None
    assert _col(rec, kind="join_key")["confidence"] < 1.0

    # genuinely one-sided columns stay separate, still in the output (full outer join of columns)
    assert _col(rec, field_a="blood_group")["kind"] == "only_a"
    assert _col(rec, field_b="vehicle_no")["kind"] == "only_b"
    out = {f.path for f in rec.output_schema.fields}
    assert {"citizen_id", "full_name", "dob", "phone", "blood_group", "vehicle_no"} <= out
    assert len(out) == 6                                # each concept exactly once


def test_more_joined_rows_means_more_confidence():
    small = _reconcile(*_people(3), "citizen_id", "owner_id")
    large = _reconcile(*_people(8), "citizen_id", "owner_id")
    assert _col(large, field_a="phone")["confidence"] > _col(small, field_a="phone")["confidence"]


def test_matching_names_beat_value_only_matches():
    a = [{"pid": f"P{i}", "full_name": NAMES[i], "code": f"X{i}"} for i in range(8)]
    b = [{"pid": f"P{i}", "full_name": NAMES[i], "zzz": f"X{i}"} for i in range(8)]
    rec = _reconcile(a, b, "pid", "pid")
    same_name = _col(rec, field_a="full_name", field_b="full_name")["confidence"]
    value_only = _col(rec, field_a="code", field_b="zzz")
    assert value_only["kind"] == "matched"             # identical values are enough to merge...
    assert same_name > value_only["confidence"]        # ...but name + meaning + values is stronger


def test_contradicting_values_are_not_merged_and_are_flagged():
    a = [{"pid": f"P{i}", "status": s} for i, s in enumerate(["ACTIVE", "INACTIVE", "ACTIVE", "PENDING", "ACTIVE", "INACTIVE"])]
    b = [{"pid": f"P{i}", "status": s} for i, s in enumerate(["PAID", "UNPAID", "PAID", "OVERDUE", "UNPAID", "PAID"])]
    rec = _reconcile(a, b, "pid", "pid")
    assert {f.path for f in rec.output_schema.fields} == {"pid", "status", "status_2"}
    c = _col(rec, field_a="status")
    assert c["kind"] == "only_a" and c["needs_review"] is True


def test_opposing_name_parts_do_not_match():
    assert semantic.semantic_similarity("first_name", "last_name")[0] < 0.3
    assert semantic.semantic_similarity("valid_from", "valid_to")[0] < 0.3
    assert semantic.semantic_similarity("first_name", "given_name")[0] > 0.9
    assert semantic.semantic_similarity("issue_date", "date_of_issue")[0] > 0.9
    assert semantic.semantic_similarity("zip", "postal_code")[0] > 0.9
    assert semantic.semantic_similarity("beds", "buses")[0] < 0.1


def test_declared_alias_is_honoured_but_still_not_100():
    a = [{"pid": f"P{i}", "name": NAMES[i]} for i in range(6)]
    b = [{"pid": f"P{i}", "label_x": NAMES[(i + 1) % 6]} for i in range(6)]   # values even disagree
    rec = _reconcile(a, b, "pid", "pid", field_aliases={"name": "label_x"})
    c = _col(rec, field_a="name", field_b="label_x")
    assert c["declared_alias"] and 0.95 <= c["confidence"] <= MAX_CONFIDENCE


def test_duplicate_concept_is_flagged_not_called_unique():
    a = [{"pid": f"P{i}", "first_name": NAMES[i].split()[0]} for i in range(8)]
    b = [{"pid": f"P{i}", "given_name": NAMES[i].split()[0], "forename": NAMES[i].split()[0]} for i in range(8)]
    rec = _reconcile(a, b, "pid", "pid")
    matched = _col(rec, field_a="first_name")
    assert matched["kind"] == "matched" and matched["needs_review"]          # ambiguity penalty applied
    assert any("ambiguous" in e for e in matched["evidence"])
    extra = _col(rec, field_b="forename")
    assert extra["kind"] == "only_b" and extra["needs_review"]
    assert extra["confidence"] < 0.5                                         # NOT "98% sure it is unique"


def test_keyless_same_name_disjoint_populations_still_merge_but_capped():
    a = [{"customer_id": f"A{i}", "city": c} for i, c in enumerate(["Pune", "Delhi", "Goa", "Agra"])]
    b = [{"customer_id": f"B{i}", "city": c} for i, c in enumerate(["Surat", "Delhi", "Kochi", "Patna"])]
    rec = _reconcile(a, b, None, None)
    c = _col(rec, field_a="customer_id", field_b="customer_id")
    assert c["kind"] == "matched"                       # different people, same column - not a contradiction
    assert c["confidence"] <= 0.91                      # but no joined rows to verify against -> capped
    assert rec.summary["keyed"] is False


def test_bad_explicit_join_key_gets_low_confidence():
    a = [{"k": f"A{i}", "v": i} for i in range(6)]
    b = [{"k": f"B{i}", "w": i} for i in range(6)]       # keys never overlap
    rec = _reconcile(a, b, "k", "k")
    assert _col(rec, kind="join_key")["confidence"] <= 0.40


def test_summary_numbers_are_consistent():
    rec = _reconcile(*_people(), "citizen_id", "owner_id")
    s = rec.summary
    assert s["matched_columns"] == 4                    # join key + name + dob + phone
    assert s["only_requester_columns"] == 1 and s["only_counterparty_columns"] == 1
    assert 0 < s["schema_overlap"] < 1
    assert s["min_match_confidence"] <= s["mean_match_confidence"] <= MAX_CONFIDENCE


def test_embeddings_blend_in_and_failure_falls_back(monkeypatch):
    semantic._embed_cache.clear()
    vecs = {"vehicle no": [1.0, 0.0], "registration plate": [0.98, 0.2], "phone": [0.0, 1.0]}
    assert semantic.prefetch_embeddings(list(vecs), embed_fn=lambda texts: [vecs[t] for t in texts])
    s, method = semantic.semantic_similarity("vehicle_no", "registration_plate")
    assert method == "lexicon+embedding" and s > semantic.lexicon_similarity("vehicle_no", "registration_plate")
    assert semantic.semantic_similarity("vehicle_no", "phone")[0] < 0.3

    semantic._embed_cache.clear()
    def boom(_):
        raise RuntimeError("network down")
    assert semantic.prefetch_embeddings(["a_b"], embed_fn=boom) is False
    assert semantic.semantic_similarity("vehicle_no", "registration_plate")[1] == "lexicon"


def test_join_key_prefers_identifier_over_unique_names():
    from app.mapping_engine.join_planner import detect_join_fields
    a, b = _people()
    sa, sb = discover_schema("a", "a", a), discover_schema("b", "b", b)
    ka, kb, *_ = detect_join_fields(sa, sb, a, b)
    assert (ka, kb) == ("citizen_id", "owner_id")

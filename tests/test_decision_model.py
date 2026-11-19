import random
import sqlite3

import pytest

from control_plane.services import decision_model as dm, embeddings, knowledge_index as ki

E = embeddings.HashingEmbedder()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE mapping_suggestions (suggestion_id TEXT PRIMARY KEY, run_id TEXT, migration_id TEXT, entity_name TEXT, source_field TEXT, canonical_field TEXT, "
              "proposed_rule TEXT, confidence_score REAL, status TEXT, reviewer TEXT, review_comments TEXT, created_utc TEXT, reviewed_utc TEXT)")
    return c


def _add(conn, n, mig="M1", informative=True, seed=3):
    """Decisions where the reviewers' choice follows the canonical field's name, and the agent's confidence says nothing."""
    rng = random.Random(seed)
    for i in range(n):
        good = i % 2 == 0
        canonical = f"{'GOOD' if good else 'BAD'}_FIELD_{i}" if informative else f"FIELD_{i}"
        approved = good if informative else rng.random() < 0.5
        conn.execute("INSERT INTO mapping_suggestions VALUES (?, 'R', ?, 'Account', ?, ?, 'DIRECT_COPY', ?, ?, 'Ann', NULL, '2026-11-19T10:00:00', '2026-11-19T11:00:00')",
                     (f"S{mig}{i}", mig, f"SourceCol{i}", canonical, rng.random(), "APPROVED" if approved else "REJECTED"))


def test_it_refuses_to_learn_from_too_few_decisions_and_says_what_is_missing(conn):
    _add(conn, 20)
    r = dm.readiness(conn, "M1")
    assert r["decisions"] == 20 and r["ready"] is False and r["min_decisions"] == dm.MIN_DECISIONS
    with pytest.raises(ValueError, match="Not enough decisions"):
        dm.train(conn, "M1", "Ann", E)
    assert dm.list_models(conn, "M1") == [] and dm.likelihood(conn, "M1", E, [{"entity_name": "Account", "source_field": "X", "canonical_field": "GOOD_X", "confidence_score": 0.5}]) == {}


def test_a_model_that_beats_the_agents_confidence_on_held_out_decisions_is_used(conn):
    _add(conn, 120)
    r = dm.readiness(conn, "M1")
    assert r["ready"] and r["approved"] == 60 and r["rejected"] == 60
    res = dm.train(conn, "M1", "Ann", E)
    assert res["status"] == "ACTIVE" and res["tuned_acc"] >= 0.9 and res["base_acc"] <= 0.7 and res["tuned_acc"] >= res["base_acc"] + res["min_gain"]
    pending = [{"entity_name": "Account", "source_field": "NewCol1", "canonical_field": "GOOD_FIELD_X", "confidence_score": 0.5},
               {"entity_name": "Account", "source_field": "NewCol2", "canonical_field": "BAD_FIELD_Y", "confidence_score": 0.5}]
    lk = dm.likelihood(conn, "M1", E, pending)
    assert lk["Account|NEWCOL1"] > 0.5 > lk["Account|NEWCOL2"]  # keyed like the other advisory hints
    assert dm.likelihood(conn, "M2", E, pending) == {}  # another migration has no model of its own
    assert dm.likelihood(conn, "M1", embeddings.HashingEmbedder(64), pending) == {}  # and a model is only used with the embedder it was trained with


def test_decisions_that_follow_no_pattern_do_not_earn_a_model(conn):
    _add(conn, 120, informative=False)
    res = dm.train(conn, "M1", "Ann", E)
    assert res["status"] == "REJECTED" and res["tuned_acc"] < res["base_acc"] + res["min_gain"]
    assert [m["status"] for m in dm.list_models(conn, "M1")] == ["REJECTED"]
    assert dm.likelihood(conn, "M1", E, [{"entity_name": "Account", "source_field": "A", "canonical_field": "B", "confidence_score": 0.5}]) == {}


def test_only_the_latest_decision_per_suggestion_counts_and_a_model_can_be_retired(conn):
    _add(conn, 120)
    conn.execute("INSERT INTO mapping_suggestions VALUES ('Sdup', 'R', 'M1', 'Account', 'SourceCol0', 'GOOD_FIELD_0', 'DIRECT_COPY', 0.5, 'REJECTED', 'Bo', NULL, '2026-11-19T10:00:00', '2026-11-20T09:00:00')")
    assert dm.readiness(conn, "M1")["decisions"] == 120 and dm.readiness(conn, "M1")["rejected"] == 61  # the later rejection replaced the earlier approval
    first = dm.train(conn, "M1", "Ann", E)
    second = dm.train(conn, "M1", "Bo", E)
    states = {m["model_id"]: m["status"] for m in dm.list_models(conn, "M1")}
    assert states[first["model_id"]] == "RETIRED" and states[second["model_id"]] == "ACTIVE" and first["data_hash"] == second["data_hash"]
    with pytest.raises(ValueError, match="not the active"):
        dm.retire(conn, "M2", second["model_id"])
    dm.retire(conn, "M1", second["model_id"])
    assert dm.likelihood(conn, "M1", E, [{"entity_name": "Account", "source_field": "A", "canonical_field": "GOOD_A", "confidence_score": 0.5}]) == {}


def test_purging_a_migration_removes_its_decision_models_too(conn):
    _add(conn, 120)
    dm.train(conn, "M1", "Ann", E)
    ki.ensure_tables(conn)
    assert ki.purge_migration(conn, "M1")["models"] == 1 and dm.list_models(conn, "M1") == []

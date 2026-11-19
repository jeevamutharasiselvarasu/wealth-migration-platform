from urllib.parse import unquote_plus

import numpy as np

from control_plane.services import embeddings, knowledge_chunks as kc, knowledge_index as ki, retrieval_tuning as rt
from tests.test_knowledge_ingestion import _loc, app_env  # noqa: F401  (the fixture and a helper)
from tests.test_retrieval import Rotated

DICTIONARY = (b"# Accounts\nThe Account field AccountNumber (canonical ACCT_NUM) is the custodian's identifier for one custodial account.\n\n"
              b"# Households\nA Household groups related clients; HouseholdId (canonical HH_ID) names the group.\n")


def _upload(env, name=b"dictionary.md", content=DICTIONARY):
    env.call("analyst", "POST", "/api/knowledge/file-upload", {"category": "data_dictionary"}, {"file": (name.decode(), content, "text/markdown")})
    return env.store.firm_knowledge_docs[-1]


def _db(env):
    return env.app.get_db_connection()


def test_uploaded_knowledge_is_embedded_at_once_and_found_by_a_search_with_its_citation(app_env):
    doc = _upload(app_env)
    c = _db(app_env)
    try:
        n = c.execute("SELECT COUNT(*) FROM knowledge_vectors AS v JOIN knowledge_chunks AS k ON k.chunk_id = v.chunk_id WHERE k.asset_id = ?", (f"{app_env.mid}:{doc['id']}",)).fetchone()[0]
    finally:
        c.close()
    assert n == 2  # both chunks have a vector before anyone searches
    page = app_env.call("analyst", "GET", "/firm-knowledge?q=which+field+is+the+custodian+account+number").text
    assert 'data-testid="search-hit"' in page and "dictionary.md" in page and "Section: Accounts" in page and embeddings.get_embedder().name in page
    assert "/api/knowledge/feedback" in page and "Helpful" in page
    nothing = app_env.call("analyst", "GET", "/firm-knowledge?q=zzzz+qqqq+xxxx").text
    assert 'data-testid="search-empty"' in nothing
    viewer = app_env.call("viewer", "GET", "/firm-knowledge?q=account+number").text
    assert 'data-testid="search-hit"' in viewer and "/api/knowledge/feedback" not in viewer  # anyone can read evidence; only reviewers mark it


def test_feedback_is_recorded_for_reviewers_only_and_only_for_this_migrations_evidence(app_env):
    _upload(app_env)
    c = _db(app_env)
    try:
        hit = ki.retrieve(c, app_env.mid, ["account number"], embeddings.get_embedder())[0][0]
        c.commit()
    finally:
        c.close()
    form = {"subject": "Account.AccountNumber", "query": "account number", "chunk_id": hit["chunk_id"], "verdict": "helpful", "return_to": "/discovery"}
    r = app_env.call("analyst", "POST", "/api/knowledge/feedback", form)
    assert r.headers["location"].startswith("/discovery?notice=") and "helpful" in _loc(r)
    c = _db(app_env)
    try:
        assert ki.feedback_counts(c, app_env.mid) == {"helpful": 1, "not_helpful": 0}
    finally:
        c.close()
    assert "may+not+perform" in app_env.call("viewer", "POST", "/api/knowledge/feedback", form).headers["location"]
    bad = app_env.call("analyst", "POST", "/api/knowledge/feedback", {**form, "chunk_id": "KC-NOT-MINE"})
    assert "error=" in bad.headers["location"] and "not part of this migration" in _loc(bad)
    odd = app_env.call("analyst", "POST", "/api/knowledge/feedback", {**form, "return_to": "https://evil.example/x"})
    assert odd.headers["location"].startswith("/firm-knowledge?")  # only our own pages are redirect targets
    panel = app_env.call("analyst", "GET", "/firm-knowledge").text
    assert "1 helpful" in panel and 'data-testid="tune-retrieval"' in panel and "disabled" in panel.split('data-testid="tune-retrieval"')[0].rsplit("<button", 1)[1]


def test_evidence_for_a_mapping_suggestion_comes_from_the_migrations_knowledge_and_never_breaks_the_page(app_env, monkeypatch):
    _upload(app_env)
    suggestions = [{"entity_name": "Account", "source_field": "AccountNumber", "canonical_field": "ACCT_NUM"},
                   {"entity_name": "Household", "source_field": "HouseholdId", "canonical_field": "HH_ID"}]
    ev = app_env.app._evidence_for(suggestions)
    acct = ev["Account|ACCOUNTNUMBER"]
    assert acct["subject"] == "Account.AccountNumber" and acct["hits"][0]["citation"] == "Section: Accounts" and "AccountNumber" in acct["hits"][0]["snippet"]
    assert ev["Household|HOUSEHOLDID"]["hits"][0]["citation"] == "Section: Households"
    assert app_env.app._evidence_for([]) == {}
    monkeypatch.setattr(ki, "retrieve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("index unavailable")))
    assert app_env.app._evidence_for(suggestions) == {}  # advisory: no evidence rather than an error


def _seed_rotated(env, n=60):
    """Knowledge and feedback for an embedder with a hidden vocabulary gap, written straight to the app database."""
    emb = Rotated(n)
    c = _db(env)
    try:
        kc.index_asset(c, migration_id=env.mid, scope="firm", asset_id=f"{env.mid}:SEED", asset_name="seed.md", source="file", source_ref="s", version="v",
                       chunks=[{"text": f"c{i}", "citation": f"Part {i}"} for i in range(n)])
        ids = {r["text"]: r["chunk_id"] for r in c.execute("SELECT chunk_id, text FROM knowledge_chunks WHERE migration_id = ?", (env.mid,))}
        for i in range(n):
            ki.record_feedback(c, env.mid, f"Field {i}", f"q{i}", ids[f"c{i}"], 1, "Ann")
        c.commit()
    finally:
        c.close()
    return emb


def test_tuning_is_refused_without_enough_feedback_then_used_only_when_it_beats_untuned_retrieval(app_env, monkeypatch):
    emb = Rotated()
    monkeypatch.setattr(embeddings, "get_embedder", lambda refresh=False: emb)
    few = app_env.call("analyst", "POST", "/api/knowledge/retrieval/tune")
    assert "error=" in few.headers["location"] and "Not enough feedback" in _loc(few) and few.headers["location"].endswith("#retrieval")
    _seed_rotated(app_env)
    page = app_env.call("analyst", "GET", "/firm-knowledge").text
    assert "60 helpful" in page and 'data-testid="tune-retrieval"' in page and 'disabled data-testid="tune-retrieval"' not in page
    assert "may+not+perform" in app_env.call("viewer", "POST", "/api/knowledge/retrieval/tune").headers["location"]
    done = app_env.call("analyst", "POST", "/api/knowledge/retrieval/tune")
    msg = _loc(done)
    assert "notice=" in done.headers["location"] and "now used" in msg and "held-out queries" in msg
    c = _db(app_env)
    try:
        models = rt.list_models(c, app_env.mid)
        assert [m["status"] for m in models] == ["ACTIVE"] and rt.active_adapter(c, app_env.mid, emb.name) is not None
    finally:
        c.close()
    shown = app_env.call("analyst", "GET", "/firm-knowledge").text
    assert 'data-testid="retrieval-model"' in shown and models[0]["model_id"] in shown and "Go back to untuned" in shown
    gone = app_env.call("analyst", "POST", "/api/knowledge/retrieval/retire", {"model_id": models[0]["model_id"]})
    assert "untuned" in _loc(gone)
    c = _db(app_env)
    try:
        assert rt.active_adapter(c, app_env.mid, emb.name) is None
    finally:
        c.close()
    again = app_env.call("analyst", "POST", "/api/knowledge/retrieval/retire", {"model_id": models[0]["model_id"]})
    assert "error=" in again.headers["location"] and "not the active" in _loc(again)


def test_the_search_panel_and_evidence_do_not_leak_another_migrations_knowledge(app_env):
    _upload(app_env)
    c = _db(app_env)
    try:
        kc.index_asset(c, migration_id="MIG-OTHER-FIRM", scope="firm", asset_id="MIG-OTHER-FIRM:DOC-001", asset_name="secret-firm.md", source="file", source_ref="x", version="v",
                       chunks=[{"text": "Account number layout of a competing firm", "citation": "Part 1"}])
        c.commit()
    finally:
        c.close()
    page = app_env.call("analyst", "GET", "/firm-knowledge?q=account+number+layout").text
    assert "secret-firm.md" not in page and "competing firm" not in page
    assert np.isclose(np.linalg.norm(embeddings.get_embedder().embed(["account"])[0]), 1.0)

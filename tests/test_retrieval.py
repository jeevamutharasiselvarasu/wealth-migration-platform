import sqlite3

import numpy as np
import pytest

from control_plane.services import embeddings, knowledge_chunks as kc, knowledge_index as ki, retrieval_tuning as rt


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def _asset(conn, mig, asset, texts, scope="firm", citations=None, entities=None):
    chunks = [{"text": t, "citation": (citations or [f"Part {i + 1}" for i in range(len(texts))])[i]} for i, t in enumerate(texts)]
    return kc.index_asset(conn, migration_id=mig, scope=scope, asset_id=asset, asset_name=asset + ".md", source="file", source_ref=asset + ".md", version="v1",
                          chunks=chunks, entity_names=entities or [])


# ---- embedders --------------------------------------------------------------------------------------------------------------------------------------
def test_the_hashing_embedder_is_stable_unit_length_and_reads_column_names_as_words():
    e = embeddings.HashingEmbedder()
    assert e.name == "hashing-v1-384" and e.dim == 384
    v = e.embed(["The account number identifies one account.", "ACCT_NUM", "AccountNumber", ""])
    assert v.shape == (4, 384) and v.dtype == np.float32
    assert np.allclose(np.linalg.norm(v[:3], axis=1), 1.0, atol=1e-5) and not v[3].any()  # an empty text stays the zero vector
    assert np.array_equal(v, e.embed(["The account number identifies one account.", "ACCT_NUM", "AccountNumber", ""]))  # the same text, the same vector, on every run
    assert embeddings._tokens("ACCT_NUM AccountNumber HH_ID2") == ["acct", "num", "account", "number", "hh", "id", "2"]
    near, far = e.embed(["account number field"])[0], e.embed(["custodian name list"])[0]
    assert float(v[2] @ near) > float(v[2] @ far)  # shared vocabulary is closer than none


def test_the_embedder_is_chosen_from_the_environment_and_never_downloads_by_itself(monkeypatch):
    monkeypatch.delenv("XODUS_EMBEDDER", raising=False)
    monkeypatch.delenv("XODUS_EMBEDDING_MODEL", raising=False)
    assert embeddings.get_embedder(refresh=True).name == "hashing-v1-384"
    monkeypatch.setenv("XODUS_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")  # auto: a named model that cannot be loaded falls back to hashing
    monkeypatch.setattr(embeddings, "FastEmbedder", lambda model: (_ for _ in ()).throw(embeddings.EmbedderUnavailable("no fastembed")))
    assert embeddings.get_embedder(refresh=True).name == "hashing-v1-384"
    monkeypatch.setenv("XODUS_EMBEDDER", "fastembed")  # asked for explicitly: the failure is shown, not hidden
    with pytest.raises(embeddings.EmbedderUnavailable):
        embeddings.get_embedder(refresh=True)
    monkeypatch.setenv("XODUS_EMBEDDER", "hashing")
    assert embeddings.get_embedder(refresh=True).name == "hashing-v1-384"
    embeddings._cached = None


def test_fastembed_is_reported_missing_when_the_package_is_not_installed():
    import importlib.util
    if importlib.util.find_spec("fastembed"):
        pytest.skip("fastembed is installed here")
    with pytest.raises(embeddings.EmbedderUnavailable, match="not installed"):
        embeddings.FastEmbedder("BAAI/bge-small-en-v1.5")


# ---- vectors and retrieval --------------------------------------------------------------------------------------------------------------------------
E = embeddings.HashingEmbedder()


def test_chunks_are_embedded_once_and_again_when_the_embedder_changes(conn):
    _asset(conn, "M1", "a", ["Account numbers identify custodial accounts.", "Households group related clients."])
    assert ki.embed_pending(conn, E) == 2 and ki.embed_pending(conn, E) == 0  # nothing to do the second time

    class Other(embeddings.HashingEmbedder):
        def __init__(self):
            super().__init__(64)
    assert ki.embed_pending(conn, Other()) == 2  # a different embedder re-embeds everything instead of mixing vectors
    assert {r["model"] for r in conn.execute("SELECT model FROM knowledge_vectors")} == {"hashing-v1-64"}
    kc.retire_asset(conn, "a")
    assert ki.embed_pending(conn, E) == 0  # retired chunks are not embedded


def test_retrieval_returns_the_closest_chunks_with_citations_and_only_this_migrations_and_the_platforms_knowledge(conn):
    _asset(conn, "M1", "dict", ["Account number: the custodian's identifier for one account.", "Household: a group of related clients sharing one advisor.",
                                "Advisor rep code identifies the advisor who services a household."], citations=["Sheet Accounts, rows 2-9", "Section: Households", "Page 3"],
           entities=["Account", "Household"])
    _asset(conn, "M2", "other", ["Account number rules of a different firm."])
    _asset(conn, "-", "platform", ["Custodian codes map SCHWAB to one."], scope="platform")
    [[top, *rest]] = ki.retrieve(conn, "M1", ["which field is the account number"], E, k=3)
    assert top["asset_name"] == "dict.md" and top["citation"] == "Sheet Accounts, rows 2-9" and "Account number" in top["snippet"] and 0 < top["score"] <= 1
    assert all(h["asset_name"] != "other.md" for h in rest)  # another migration's knowledge is never returned
    [[plat]] = ki.retrieve(conn, "M1", ["custodian codes SCHWAB"], E, k=1)
    assert plat["scope"] == "platform" and plat["citation"] == "Part 1"
    assert all(h["scope"] == "platform" for h in ki.retrieve(conn, "M3", ["account number"], E, k=3)[0])  # a migration with no knowledge of its own sees only the platform's
    assert ki.retrieve(conn, "M1", ["zzzz qqqq"], E, k=3, min_score=0.3) == [[]]  # nothing close enough: no evidence rather than weak evidence
    assert ki.retrieve(conn, "M1", [], E) == []
    kc.retire_asset(conn, "dict")
    assert all(h["asset_name"] != "dict.md" for h in ki.retrieve(conn, "M1", ["account number"], E)[0])


def test_an_entity_filter_keeps_the_entitys_chunks_and_the_general_ones(conn):
    _asset(conn, "M1", "a", ["Account numbers are unique.", "Households have a name.", "Every record needs an identifier."], entities=["Account", "Household"])
    got = {h["snippet"] for h in ki.retrieve(conn, "M1", ["identifier name numbers"], E, k=5, entity="Household", min_score=0)[0]}
    assert "Households have a name." in got and "Every record needs an identifier." in got and "Account numbers are unique." not in got


def test_feedback_is_one_verdict_per_person_query_and_chunk_and_only_about_visible_evidence(conn):
    _asset(conn, "M1", "a", ["Account numbers are unique."])
    _asset(conn, "M2", "b", ["Another firm's note."])
    cid = ki.retrieve(conn, "M1", ["account numbers"], E)[0][0]["chunk_id"]
    other = conn.execute("SELECT chunk_id FROM knowledge_chunks WHERE migration_id = 'M2'").fetchone()["chunk_id"]
    ki.record_feedback(conn, "M1", "Account.ACCT_NUM", "account numbers", cid, 1, "Ann")
    ki.record_feedback(conn, "M1", "Account.ACCT_NUM", "account  numbers ", cid, -1, "Ann")  # the same person changes their mind (whitespace is normalised)
    assert ki.feedback_counts(conn, "M1") == {"helpful": 0, "not_helpful": 1}
    ki.record_feedback(conn, "M1", "Account.ACCT_NUM", "account numbers", cid, 1, "Bo")
    assert ki.feedback_counts(conn, "M1") == {"helpful": 1, "not_helpful": 1}
    for args, why in (((cid, 2), "helpful or not"), ((other, 1), "not part of this migration"), (("KC-NOPE", 1), "not part of this migration")):
        with pytest.raises(ValueError, match=why):
            ki.record_feedback(conn, "M1", "s", "account numbers", args[0], args[1], "Ann")
    with pytest.raises(ValueError, match="nothing to give feedback"):
        ki.record_feedback(conn, "M1", "", "q", cid, 1, "Ann")
    kc.retire_asset(conn, "a")
    assert ki.feedback_counts(conn, "M1") == {"helpful": 0, "not_helpful": 0}  # feedback about retired evidence no longer counts
    with pytest.raises(ValueError, match="not part of this migration"):
        ki.record_feedback(conn, "M1", "s", "account numbers", cid, 1, "Ann")


# ---- tuning -----------------------------------------------------------------------------------------------------------------------------------------
class Rotated:
    """An embedder with a hidden vocabulary gap: a query "q7" is the chunk "c7" seen through a fixed rotation, so untuned retrieval cannot find it
    and a learned linear map can."""
    dim, name = 16, "fake-rotated-16"

    def __init__(self, n=60, seed=7):
        rng = np.random.default_rng(seed)
        c = rng.normal(size=(n, self.dim))
        self.C = (c / np.linalg.norm(c, axis=1, keepdims=True)).astype(np.float32)
        self.R, _ = np.linalg.qr(rng.normal(size=(self.dim, self.dim)))
        noise = rng.normal(size=(n, self.dim)) * 0.03
        q = self.C @ self.R + noise
        self.Q = (q / np.linalg.norm(q, axis=1, keepdims=True)).astype(np.float32)

    def embed(self, texts):
        return np.vstack([self.C[int(t[1:])] if t[0] == "c" else self.Q[int(t[1:])] for t in texts])


def _seed_feedback(conn, emb, n, mig="M1", bad_pairs=False):
    chunks = [{"text": f"c{i}", "citation": f"Part {i}"} for i in range(n)]
    kc.index_asset(conn, migration_id=mig, scope="firm", asset_id=f"{mig}:A", asset_name="a.md", source="file", source_ref="a", version="v", chunks=chunks)
    ids = {r["text"]: r["chunk_id"] for r in conn.execute("SELECT chunk_id, text FROM knowledge_chunks WHERE migration_id = ?", (mig,))}
    for i in range(n):
        target = (i + 17) % n if bad_pairs else i  # wrong answers: the "helpful" chunk has nothing to do with the query
        ki.record_feedback(conn, mig, f"Field {i}", f"q{i}", ids[f"c{target}"], 1, "Ann")
    return ids


def test_tuning_needs_enough_feedback_and_says_how_much_is_missing(conn):
    emb = Rotated()
    _seed_feedback(conn, emb, 12)
    r = rt.readiness(conn, "M1", emb)
    assert r["ready"] is False and r["positives"] == 12 and r["min_positive"] == rt.MIN_POSITIVE
    with pytest.raises(ValueError, match="Not enough feedback to tune"):
        rt.tune(conn, "M1", "Ann", emb)
    assert rt.list_models(conn, "M1") == [] and rt.active_adapter(conn, "M1", emb.name) is None


def test_a_tuned_adapter_is_activated_only_when_it_beats_untuned_retrieval_on_held_out_queries(conn):
    emb = Rotated()
    _seed_feedback(conn, emb, 60)
    r = rt.readiness(conn, "M1", emb)
    assert r["ready"] and r["positives"] == 60 and r["eval_positives"] >= rt.MIN_EVAL and r["train_positives"] + r["eval_positives"] == 60
    res = rt.tune(conn, "M1", "Ann", emb)
    assert res["status"] == "ACTIVE" and res["tuned_hit"] >= res["base_hit"] + rt.MIN_GAIN and res["tuned_hit"] >= 0.8 and res["base_hit"] <= 0.4
    assert res["n_train"] + res["n_eval"] == 60 and len(res["data_hash"]) == 16
    W = rt.active_adapter(conn, "M1", emb.name)
    assert W is not None and W.shape == (16, 16)
    # the adapter is used by retrieval: a held-out query now finds its chunk, and untuned retrieval of the same query does not
    held_out = next(i for i in range(60) if rt._is_eval(f"q{i}"))
    [[hit]] = ki.retrieve(conn, "M1", [f"q{held_out}"], emb, k=1, min_score=-1)
    assert hit["snippet"] == f"c{held_out}"
    conn.execute("UPDATE retrieval_models SET status = 'RETIRED'")
    [[plain]] = ki.retrieve(conn, "M1", [f"q{held_out}"], emb, k=1, min_score=-1)
    assert plain["snippet"] != f"c{held_out}"


def test_feedback_that_does_not_generalise_is_rejected_and_nothing_changes(conn):
    emb = Rotated()
    _seed_feedback(conn, emb, 60, bad_pairs=True)  # consistent only by accident: nothing learned on the training queries carries to the held-out ones
    res = rt.tune(conn, "M1", "Ann", emb)
    assert res["tuned_hit"] < res["base_hit"] + res["min_gain"] and res["status"] == "REJECTED" and res["min_gain"] >= 2 / res["n_eval"]
    assert rt.active_adapter(conn, "M1", emb.name) is None
    assert [m["status"] for m in rt.list_models(conn, "M1")] == ["REJECTED"]


def test_a_better_candidate_retires_the_active_one_and_the_active_one_can_be_retired_by_hand(conn):
    emb = Rotated()
    _seed_feedback(conn, emb, 60)
    first = rt.tune(conn, "M1", "Ann", emb)
    second = rt.tune(conn, "M1", "Bo", emb)
    assert first["status"] == second["status"] == "ACTIVE" and first["data_hash"] == second["data_hash"]  # the same feedback gives the same snapshot hash
    statuses = {m["model_id"]: m["status"] for m in rt.list_models(conn, "M1")}
    assert statuses[first["model_id"]] == "RETIRED" and statuses[second["model_id"]] == "ACTIVE"
    with pytest.raises(ValueError, match="not the active"):
        rt.retire(conn, "M1", first["model_id"])
    with pytest.raises(ValueError, match="not the active"):
        rt.retire(conn, "M2", second["model_id"])  # another migration cannot retire it
    rt.retire(conn, "M1", second["model_id"])
    assert rt.active_adapter(conn, "M1", emb.name) is None


def test_an_adapter_is_only_used_with_the_embedder_it_was_trained_on(conn):
    emb = Rotated()
    _seed_feedback(conn, emb, 60)
    rt.tune(conn, "M1", "Ann", emb)
    assert rt.active_adapter(conn, "M1", emb.name) is not None and rt.active_adapter(conn, "M1", "hashing-v1-384") is None
    assert rt.active_adapter(conn, "M2", emb.name) is None  # and only for the migration whose feedback trained it

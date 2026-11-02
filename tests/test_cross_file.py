from control_plane.services import cross_file as cf


def _files():
    acc = [{"ACCT": f"A{i}", "HH": f"H{i % 3}", "REG": "IRA"} for i in range(6)]
    reg = [{"ACCT": f"A{i}", "REG": "IRA" if i < 4 else "TAX"} for i in range(6)]
    hh = [{"HH": "H0", "NAME": "x"}, {"HH": "H1", "NAME": "y"}, {"HH": "H2", "NAME": "z"}]
    return [{"name": "acc.csv", "entity": "Account", "rows": acc}, {"name": "reg.csv", "entity": "Account", "rows": reg},
            {"name": "hh.csv", "entity": "Household", "rows": hh}, {"name": "hh_copy.csv", "entity": "Household", "rows": list(hh)}]


def test_cross_file_finds_relationships_conflicts_overlaps_and_duplicate_files_without_values():
    out = cf.analyze(_files())
    assert out["files_analyzed"] == 4
    assert any(r["child"] == "acc.csv.HH" and r["parent"] == "hh.csv.HH" and r["kind"] == "cross-entity link" for r in out["relationships"])
    assert any(c["files"] == ["acc.csv", "reg.csv"] and c["column"] == "REG" and (c["compared_rows"], c["differing_rows"]) == (6, 2) for c in out["conflicts"])
    assert out["duplicate_files"] == [{"files": ["hh.csv", "hh_copy.csv"], "detail": "Identical content (same columns and rows)."}]
    assert any(e["a"] == "acc.csv.ACCT" and e["b"] == "reg.csv.ACCT" for e in out["equivalent_columns"])
    blob = repr(out)
    assert "'IRA'" not in blob and "'TAX'" not in blob  # counts and column names only


def test_cross_file_is_quiet_for_unrelated_files_and_handles_empty_input():
    out = cf.analyze([{"name": "a.csv", "entity": "Account", "rows": [{"X": "1"}, {"X": "2"}]},
                      {"name": "b.csv", "entity": "Client", "rows": [{"Y": "q"}, {"Y": "r"}]}])
    assert not (out["equivalent_columns"] or out["relationships"] or out["conflicts"] or out["duplicate_files"])
    assert cf.analyze([{"name": "e.csv", "entity": "Account", "rows": []}])["files_analyzed"] == 1


def test_discovery_page_shows_cross_file_analysis_for_two_or_more_files(tmp_path, monkeypatch):
    import asyncio
    import httpx
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid", {"email": "t@example.invalid", "name": "Ann", "role": "Migration Analyst"})
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("ACCT,REG\nA1,IRA\nA2,IRA\nA3,TAX\n")
    b.write_text("ACCT,REG\nA1,IRA\nA2,TAX\nA3,TAX\n")
    store.entity_source_files["Account"] = {
        "F1": {"file_id": "F1", "filename": "a.csv", "stored_path": str(a), "inference_approval_status": "APPROVED"},
        "F2": {"file_id": "F2", "filename": "b.csv", "stored_path": str(b), "inference_approval_status": "PENDING"}}

    async def get(path):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.get(path)

    page = asyncio.run(get("/entity-sample-files")).text
    assert "Cross-file analysis" in page and "Conflicting values across files" in page and "1 of 3" in page
    assert "IRA" not in page.split("Cross-file analysis")[1].split("Approve all files")[0]


def test_relationship_decisions_are_validated_persisted_and_attached(tmp_path):
    import sqlite3
    import pytest
    conn = sqlite3.connect(tmp_path / "r.db")
    conn.row_factory = sqlite3.Row
    with pytest.raises(ValueError, match="reason"):
        cf.review_relationship(conn, "M", "a.csv.ID", "b.csv.ID", "REJECTED", " ", "Ann")
    with pytest.raises(ValueError, match="Decision"):
        cf.review_relationship(conn, "M", "a.csv.ID", "b.csv.ID", "MAYBE", "x", "Ann")
    with pytest.raises(ValueError, match="file.column"):
        cf.review_relationship(conn, "M", "bad", "b.csv.ID", "CONFIRMED", "", "Ann")
    cf.review_relationship(conn, "M", "a.csv.ID", "b.csv.ID", "CONFIRMED", "", "Ann")
    cf.review_relationship(conn, "M", "a.csv.ID", "b.csv.ID", "REJECTED", "different meaning", "Bea")  # latest decision wins
    analysis = {"relationships": [{"child": "a.csv.ID", "parent": "b.csv.ID"}, {"child": "c.csv.ID", "parent": "b.csv.ID"}]}
    cf.attach_reviews(conn, "M", analysis)
    assert analysis["relationships"][0]["review"]["status"] == "REJECTED" and analysis["relationships"][0]["review"]["reviewer"] == "Bea"
    assert analysis["relationships"][1]["review"] is None
    from ui.role_policy import allowed_roles_for
    assert allowed_roles_for("POST", "/api/relationships/review") == {"Platform Admin", "Migration Analyst"}


def test_non_key_columns_that_share_values_are_not_offered_as_relationships():
    assert cf._key_like("LEGACY_CLIENT_ID") and cf._key_like("PARTY_SRC_ID") and cf._key_like("ACCT_NUM") and cf._key_like("ClientID")
    assert not cf._key_like("CLIENT_TYPE_SRC") and not cf._key_like("REGISTRATION_TYPE") and not cf._key_like("STATUS")
    files = [{"name": "a.csv", "entity": "Account", "rows": [{"REG_TYPE": t} for t in ("IRA", "JNT", "TRU", "ORG")]},
             {"name": "b.csv", "entity": "Client", "rows": [{"CLIENT_TYPE": t} for t in ("IRA", "JNT", "TRU", "ORG")]}]
    assert cf.analyze(files)["relationships"] == []


def test_conflicts_are_not_computed_by_joining_on_non_key_columns():
    a = {"name": "a.csv", "entity": "Account", "rows": [{"REG_TYPE": t, "OPEN_DT": d} for t, d in (("IRA", "1"), ("JNT", "2"), ("TRU", "3"), ("ORG", "4"))]}
    b = {"name": "b.csv", "entity": "Account", "rows": [{"REGISTRATION_TYPE": t, "OPEN_DT": d} for t, d in (("IRA", "9"), ("JNT", "8"), ("TRU", "7"), ("ORG", "6"))]}
    assert cf.analyze([a, b])["conflicts"] == []

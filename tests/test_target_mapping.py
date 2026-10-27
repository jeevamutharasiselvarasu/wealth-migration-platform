import asyncio
import sqlite3

import httpx
import pytest

from control_plane.services import target_mapping as tm
from control_plane.services import transformation_rules as tr

CANON = {"Account": ["ACCT_NUM", "CUSTODIAN_CODE", "REP_CODE", "REG_TYPE", "OPEN_DATE"],
         "Client": ["CLIENT_ID", "LEGAL_NAME", "TAX_ID", "CLIENT_TYPE"],
         "Household": ["HH_ID", "HH_NAME", "REP_CODE"]}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    import control_plane.db as cdb
    monkeypatch.setattr(cdb, "DB_PATH", tmp_path / "t.db")
    cdb.init_db()
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    return c


def test_spec_is_labelled_demo_and_limited_to_p0_entities():
    spec = tm.load_spec()
    assert spec["status"] == "DEMO_NOT_OWNER_CONFIRMED" and set(spec["entities"]) == {"Account", "Client", "Household"}


def test_proposals_are_idempotent_and_never_guess_missing_canonical_fields(conn):
    created = tm.propose(conn, "MIG-T", {**CANON, "Client": ["CLIENT_ID", "LEGAL_NAME"]})
    assert created == 12 and tm.propose(conn, "MIG-T", CANON) == 0
    rows = {(m["entity_name"], m["target_field"]): m for m in tm.list_mappings(conn, "MIG-T")}
    assert rows[("Account", "OPEN_DT")]["canonical_field"] == "OPEN_DATE"
    assert rows[("Client", "TAX_NUMBER")]["canonical_field"] is None  # TAX_ID not in canonical list: stays unresolved
    with pytest.raises(ValueError, match="No canonical source"):
        tm.review(conn, rows[("Client", "TAX_NUMBER")]["target_mapping_id"], "APPROVED", "Ann", "ok")


def test_review_requires_reason_and_rejects_unapproved_rules_and_repeat_decisions(conn):
    tm.propose(conn, "MIG-T", CANON)
    conn.commit()
    m = {(x["entity_name"], x["target_field"]): x for x in tm.list_mappings(conn, "MIG-T")}[("Account", "CUSTODIAN")]
    with pytest.raises(ValueError, match="rejection reason"):
        tm.review(conn, m["target_mapping_id"], "REJECTED", "Ann", "")
    with pytest.raises(ValueError, match="not an approved"):
        tm.review(conn, m["target_mapping_id"], "APPROVED", "Ann", "ok", "RULE:NOPE@1")
    done = tm.review(conn, m["target_mapping_id"], "APPROVED", "Ann", "ok", "TRIM")
    assert done["status"] == "APPROVED" and done["transformation_expression"] == "TRIM" and done["reviewer"] == "Ann"
    with pytest.raises(ValueError, match="already APPROVED"):
        tm.review(conn, m["target_mapping_id"], "REJECTED", "Ann", "again")


def test_preview_applies_rules_reports_findings_and_contains_no_values(conn):
    tr.create_rule_version(conn, "CUST_MAP", "VALUE_MAP", "codes", {"map": {"SCH": "SCHWAB"}}, "Ann")
    tr.decide_rule(conn, "CUST_MAP", 1, "APPROVED", "Bob", "ok")
    conn.commit()
    tm.propose(conn, "MIG-T", CANON)
    conn.commit()
    for m in tm.list_mappings(conn, "MIG-T"):
        if m["entity_name"] == "Account" and m["target_field"] in ("CUSTODIAN", "ACCOUNT_NUMBER"):
            tm.review(conn, m["target_mapping_id"], "APPROVED", "Ann", "ok", "RULE:CUST_MAP@1" if m["target_field"] == "CUSTODIAN" else "")
    conn.commit()
    approved = [m for m in tm.list_mappings(conn, "MIG-T") if m["status"] == "APPROVED"]
    rows = {"Account": [{"ACCT_NUM": "A1", "CUSTODIAN_CODE": "SCH"}, {"ACCT_NUM": "", "CUSTODIAN_CODE": "ZZZ"}]}
    result = tm.preview(tm.load_spec(), approved, rows)["Account"]
    assert result["row_count"] == 2
    by = {f["target_field"]: f for f in result["fields"]}
    assert by["ACCOUNT_NUMBER"]["blank_values"] == 1 and by["CUSTODIAN"]["blank_values"] == 0
    assert "ACCOUNT_NUMBER" in result["blank_required"]
    assert set(result["uncovered_required"]) == {"ADVISOR_CODE", "REGISTRATION", "OPEN_DT"}
    assert any("unmapped value 'ZZZ'" in w for w in result["warnings"])
    assert "SCHWAB" not in repr(result) and "A1" not in repr(result)  # counts and findings only


def test_routes_are_role_gated_and_flow_from_proposal_to_preview(conn, tmp_path, monkeypatch):
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-R"
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Analyst One", "role": "Migration Analyst"})
    src = tmp_path / "accounts.csv"
    src.write_text("ACCT,CUST\nA1,SCH\nA2,SCH\n")
    store.entity_source_files["Account"] = {"ENTITY_MASTER": {"filename": "accounts.csv", "stored_path": str(src),
                                                            "inference_approval_status": "APPROVED"}}
    conn.execute("INSERT INTO canonical_mappings (mapping_id, migration_id, entity_name, source_field, canonical_field, mapping_type, "
                 "transformation_expression, confidence_score, is_confirmed, created_by, updated_utc) VALUES "
                 "('M1','MIG-R','Account','ACCT','ACCT_NUM','APPROVED_ALIAS','DIRECT_COPY',0.9,1,'x','now'),"
                 "('M2','MIG-R','Account','CUST','CUSTODIAN_CODE','APPROVED_ALIAS','DIRECT_COPY',0.9,1,'x','now')")
    conn.commit()

    async def call(role, method, path, data=None):
        app_module.session_mgr.active_sessions["sid"]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.request(method, path, data=data, follow_redirects=False, headers={"accept": "text/html"})

    denied = asyncio.run(call("Domain SME", "POST", "/api/target-mapping/propose"))
    assert denied.status_code == 303 and "may+not+perform" in denied.headers["location"]
    assert "No target mapping proposals" in asyncio.run(call("Migration Analyst", "GET", "/target-mapping")).text
    early = asyncio.run(call("Migration Analyst", "POST", "/api/target-mapping/preview"))
    assert "error=" in early.headers["location"]
    assert "notice=" in asyncio.run(call("Migration Analyst", "POST", "/api/target-mapping/propose")).headers["location"]
    ids = {(m["entity_name"], m["target_field"]): m["target_mapping_id"] for m in tm.list_mappings(conn, "MIG-R")}
    for field in ("ACCOUNT_NUMBER", "CUSTODIAN"):
        ok = asyncio.run(call("Migration Analyst", "POST", f"/api/target-mapping/{ids[('Account', field)]}/review",
                              {"decision": "APPROVED", "comments": "ok"}))
        assert "notice=" in ok.headers["location"]
    sme = asyncio.run(call("Domain SME", "POST", f"/api/target-mapping/{ids[('Account', 'OPEN_DT')]}/review", {"decision": "APPROVED", "comments": "x"}))
    assert "may+not+perform" in sme.headers["location"]
    page = asyncio.run(call("Validation Specialist", "POST", "/api/target-mapping/preview")).text
    assert "Preview (dry run)" in page and "2 source row(s)" in page and "Files joined on ACCT_NUM" in page and "Required target fields without an approved mapping" in page
    assert "SCH" not in page.split("Preview (dry run)")[1]


def test_merge_by_key_joins_files_without_inventing_blanks_and_counts_conflicts():
    a = [{"CLIENT_ID": "C1", "LEGAL_NAME": "Ann"}, {"CLIENT_ID": "C2", "LEGAL_NAME": "Bo"}]
    b = [{"CLIENT_ID": "C1", "TAX_ID": "111"}, {"CLIENT_ID": "C2", "TAX_ID": ""}, {"TAX_ID": "999"}]
    c = [{"CLIENT_ID": "C1", "LEGAL_NAME": "Ann B"}]
    out = tm.merge_by_key([a, b, c], "CLIENT_ID")
    by = {r.get("CLIENT_ID"): r for r in out["rows"] if r.get("CLIENT_ID")}
    assert out["keyed_rows"] == 2 and out["unkeyed_rows"] == 1 and out["conflicts"] == 1
    assert by["C1"] == {"CLIENT_ID": "C1", "LEGAL_NAME": "Ann", "TAX_ID": "111"}  # first kept, tax joined
    assert by["C2"]["TAX_ID"] == ""  # genuinely blank for this key
    assert tm.merge_by_key([a], None)["unkeyed_rows"] == 2

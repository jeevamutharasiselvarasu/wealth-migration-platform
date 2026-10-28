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


def test_spec_is_labelled_simulated_and_limited_to_p0_entities():
    spec = tm.load_spec()
    assert spec["status"] == "SIMULATED_NOT_OWNER_CONFIRMED" and set(spec["entities"]) == {"Account", "Client", "Household"}


def test_proposals_are_idempotent_and_never_guess_missing_canonical_fields(conn):
    created = tm.propose(conn, "MIG-T", {**CANON, "Client": ["CLIENT_ID", "LEGAL_NAME"]})
    assert created == 14
    rows = {(m["entity_name"], m["target_field"]): m for m in tm.list_mappings(conn, "MIG-T")}
    assert rows[("Account", "SIMP_ACCT_MASTER.OPENED_ON")]["canonical_field"] == "OPEN_DATE"
    assert rows[("Client", "SIMP_CUSTOMER_TAX.TAX_NO")]["canonical_field"] is None  # TAX_ID not in canonical list: stays unresolved
    with pytest.raises(ValueError, match="No canonical source"):
        tm.review(conn, rows[("Client", "SIMP_CUSTOMER_TAX.TAX_NO")]["target_mapping_id"], "APPROVED", "Ann", "ok")
    assert tm.propose(conn, "MIG-T", CANON) == 0  # idempotent; refreshes the now-resolvable TAX_NO source
    assert {m["target_field"]: m for m in tm.list_mappings(conn, "MIG-T")}["SIMP_CUSTOMER_TAX.TAX_NO"]["canonical_field"] == "TAX_ID"


def test_review_requires_reason_and_rejects_unapproved_rules_and_repeat_decisions(conn):
    tm.propose(conn, "MIG-T", CANON)
    conn.commit()
    m = {(x["entity_name"], x["target_field"]): x for x in tm.list_mappings(conn, "MIG-T")}[("Account", "SIMP_ACCT_MASTER.CUSTODIAN_CD")]
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
        if m["entity_name"] == "Account" and m["target_field"] in ("SIMP_ACCT_MASTER.CUSTODIAN_CD", "SIMP_ACCT_MASTER.ACCT_NO"):
            tm.review(conn, m["target_mapping_id"], "APPROVED", "Ann", "ok", "RULE:CUST_MAP@1" if m["target_field"] == "SIMP_ACCT_MASTER.CUSTODIAN_CD" else "")
    conn.commit()
    approved = [m for m in tm.list_mappings(conn, "MIG-T") if m["status"] == "APPROVED"]
    rows = {"Account": [{"ACCT_NUM": "A1", "CUSTODIAN_CODE": "SCH"}, {"ACCT_NUM": "", "CUSTODIAN_CODE": "ZZZ"}]}
    result = tm.preview(tm.load_spec(), approved, rows)["Account"]
    assert result["row_count"] == 2
    by = {f["target_field"]: f for f in result["fields"]}
    assert by["SIMP_ACCT_MASTER.ACCT_NO"]["blank_values"] == 1 and by["SIMP_ACCT_MASTER.CUSTODIAN_CD"]["blank_values"] == 0
    assert "SIMP_ACCT_MASTER.ACCT_NO" in result["blank_required"]
    assert set(result["uncovered_required"]) == {"SIMP_ACCT_MASTER.OPENED_ON", "SIMP_ACCT_MASTER.ACCT_STATUS", "SIMP_ACCT_REGISTRATION.REG_CD", "SIMP_ADVISOR_ASSIGNMENT.ADVISOR_ID"}
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
    assert "must complete the one-time target setup" in asyncio.run(call("Migration Analyst", "GET", "/target-mapping")).text
    assert "Set up target system (one time)" in asyncio.run(call("Platform Admin", "GET", "/target-mapping")).text
    assert "Provision the simulated target system first" in asyncio.run(call("Migration Analyst", "POST", "/api/target-mapping/propose")).headers["location"].replace("+", " ")
    assert "may+not+perform" in asyncio.run(call("Domain SME", "POST", "/api/target-system/provision")).headers["location"]
    assert "may+not+perform" in asyncio.run(call("Migration Analyst", "POST", "/api/target-system/provision")).headers["location"]
    assert "notice=" in asyncio.run(call("Platform Admin", "POST", "/api/target-system/provision")).headers["location"]
    again = asyncio.run(call("Platform Admin", "POST", "/api/target-system/provision")).headers["location"]
    assert "error=" in again and "already" in again
    assert "Set up target system (one time)" not in asyncio.run(call("Platform Admin", "GET", "/target-mapping")).text
    assert asyncio.run(call("Migration Analyst", "GET", "/target-setup")).headers["location"] == "/target-mapping#target-system"
    assert "No target mapping proposals" in asyncio.run(call("Migration Analyst", "GET", "/target-mapping")).text
    early = asyncio.run(call("Migration Analyst", "POST", "/api/target-mapping/preview"))
    assert "error=" in early.headers["location"]
    assert "notice=" in asyncio.run(call("Migration Analyst", "POST", "/api/target-mapping/propose")).headers["location"]
    ids = {(m["entity_name"], m["target_field"]): m["target_mapping_id"] for m in tm.list_mappings(conn, "MIG-R")}
    for field in ("SIMP_ACCT_MASTER.ACCT_NO", "SIMP_ACCT_MASTER.CUSTODIAN_CD"):
        ok = asyncio.run(call("Migration Analyst", "POST", f"/api/target-mapping/{ids[('Account', field)]}/review",
                              {"decision": "APPROVED", "comments": "ok"}))
        assert "notice=" in ok.headers["location"]
    sme = asyncio.run(call("Domain SME", "POST", f"/api/target-mapping/{ids[('Account', 'SIMP_ACCT_MASTER.OPENED_ON')]}/review", {"decision": "APPROVED", "comments": "x"}))
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


def test_target_system_is_provisioned_once_and_differs_from_canonical(conn, tmp_path):
    import sqlite3 as sq
    from control_plane.services import simulated_target as st
    assert st.snapshot(conn) is None
    snap = st.provision(conn, "Ann")
    conn.commit()
    with pytest.raises(ValueError, match="already provisioned"):
        st.provision(conn, "Ann")
    spec = tm.load_spec()
    info = st.describe(spec)
    names = {t["table"] for t in info["tables"]}
    assert info["drift"] == [] and snap["snapshot_id"].startswith("TGTSYS-")
    assert {"SIMP_ACCT_MASTER", "SIMP_ACCT_REGISTRATION", "SIMP_ADVISOR_ASSIGNMENT", "SIMP_CUSTOMER_TAX", "SIMP_GROUP"} <= names
    assert len({n for n in names if n.startswith("SIMP_ACCT")}) == 2  # one canonical entity split across tables
    db = sq.connect(st.target_db_path())
    assert db.execute("SELECT COUNT(*) FROM SIMP_ACCT_MASTER").fetchone()[0] == 0  # no migration data, ever
    assert db.execute("SELECT COUNT(*) FROM SIMP_REF_REGISTRATION").fetchone()[0] == 5
    db.close()
    canonical_names = {f for fields in CANON.values() for f in fields}
    assert not canonical_names & {f["column"] for b in spec["entities"].values() for f in b["fields"]}  # no column name is shared


def test_gap_report_lists_missing_unused_and_code_lists():
    report = tm.gap_report(tm.load_spec(), CANON)
    missing = {(g["entity"], g["target_field"]): g for g in report["missing"]}
    assert set(missing) == {("Account", "SIMP_ACCT_MASTER.ACCT_STATUS"), ("Client", "SIMP_CUSTOMER.ONBOARD_DT"), ("Client", "SIMP_CUSTOMER_TAX.TAX_COUNTRY")}
    assert missing[("Account", "SIMP_ACCT_MASTER.ACCT_STATUS")]["required"] and not missing[("Client", "SIMP_CUSTOMER.ONBOARD_DT")]["required"]
    assert {"entity": "Household", "canonical_field": "REP_CODE"} in report["unused"]
    assert {c["target_field"] for c in report["code_lists"]} == {"SIMP_ACCT_REGISTRATION.REG_CD", "SIMP_CUSTOMER.SEGMENT_CD"}


def test_gap_becomes_change_request_the_sme_approves_then_proposals_resolve(conn, tmp_path, monkeypatch):
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-G"
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid", {"email": "t@example.invalid", "name": "Pat", "role": "Migration Analyst"})

    async def call(role, path, data=None):
        app_module.session_mgr.active_sessions["sid"]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.post(path, data=data, follow_redirects=False, headers={"accept": "text/html"})

    asyncio.run(call("Platform Admin", "/api/target-system/provision"))
    form = {"entity_name": "Account", "target_field": "SIMP_ACCT_MASTER.ACCT_STATUS"}
    assert "may+not+perform" in asyncio.run(call("Validation Specialist", "/api/target-mapping/gaps/raise", form)).headers["location"]
    assert "notice=" in asyncio.run(call("Migration Analyst", "/api/target-mapping/gaps/raise", form)).headers["location"]
    assert "error=" in asyncio.run(call("Migration Analyst", "/api/target-mapping/gaps/raise", form)).headers["location"]  # no duplicate
    bad = asyncio.run(call("Migration Analyst", "/api/target-mapping/gaps/raise", {"entity_name": "Account", "target_field": "SIMP_ACCT_MASTER.ACCT_NO"}))
    assert "error=" in bad.headers["location"]  # not a gap: the form cannot invent requests
    row = conn.execute("SELECT * FROM canonical_change_requests WHERE migration_id = 'MIG-G'").fetchall()
    assert len(row) == 1 and row[0]["status"] == "OPEN" and row[0]["canonical_field"] == "ACCOUNT_STATUS" and row[0]["change_type"] == "ADD_FIELD"
    rid = row[0]["request_id"]
    asyncio.run(call("Migration Analyst", "/api/target-mapping/propose"))
    pending = {m["target_field"]: m for m in tm.list_mappings(conn, "MIG-G")}["SIMP_ACCT_MASTER.ACCT_STATUS"]
    assert pending["canonical_field"] is None  # unresolved until the SME decides and applies
    assert "may+not+perform" in asyncio.run(call("Migration Analyst", f"/api/canonical-changes/{rid}/decision", {"decision": "APPROVED"})).headers["location"]
    asyncio.run(call("Domain SME", f"/api/canonical-changes/{rid}/decision", {"decision": "APPROVED", "note": "ok"}))
    asyncio.run(call("Domain SME", f"/api/canonical-changes/{rid}/apply"))
    asyncio.run(call("Migration Analyst", "/api/target-mapping/propose"))
    after = {m["target_field"]: m for m in tm.list_mappings(conn, "MIG-G")}["SIMP_ACCT_MASTER.ACCT_STATUS"]
    assert after["canonical_field"] == "ACCOUNT_STATUS" and after["status"] == "PROPOSED"


def test_demo_load_is_restricted_to_flagged_account_and_provisions_target(tmp_path, monkeypatch):
    import asyncio
    import httpx
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gate.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    mgr = app_module.session_mgr
    monkeypatch.setattr(mgr, "demo_mode", True)
    monkeypatch.setitem(mgr.authorized_users, "flagged@example.invalid", {"can_load_demo": True})
    monkeypatch.setitem(mgr.authorized_users, "plain@example.invalid", {"can_load_demo": False})
    for sid, email in (("s-flag", "flagged@example.invalid"), ("s-plain", "plain@example.invalid")):
        monkeypatch.setitem(mgr.active_sessions, sid, {"email": email, "name": "T", "role": "Platform Admin"})

    async def post(sid):
        t = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=t, base_url="http://testserver", cookies={"session_id": sid}) as c:
            return await c.post("/api/demo/load", follow_redirects=False)

    assert asyncio.run(post("s-plain")).status_code == 403
    ok = asyncio.run(post("s-flag"))
    assert ok.status_code == 303
    assert mgr.can_load_demo("flagged@example.invalid") and not mgr.can_load_demo("plain@example.invalid")


def test_jeeva_is_the_only_demo_load_account_in_users_file():
    import json
    from pathlib import Path
    users = json.loads((Path(__file__).resolve().parents[1] / "demo_data" / "demo_users.json").read_text())
    items = users if isinstance(users, list) else users.get("users", users)
    flagged = [u.get("email") for u in items if isinstance(u, dict) and u.get("can_load_demo")]
    assert flagged == ["jeeva@simplicitywealth.com"]


def test_demo_load_card_and_target_connection_panel_render(tmp_path, monkeypatch):
    import asyncio
    import httpx
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "render.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    mgr = app_module.session_mgr
    monkeypatch.setattr(mgr, "demo_mode", True)
    monkeypatch.setitem(mgr.authorized_users, "jeeva@simplicitywealth.com", {"can_load_demo": True, "roles": ["Platform Admin"]})
    monkeypatch.setitem(mgr.active_sessions, "s-j", {"email": "jeeva@simplicitywealth.com", "name": "J", "role": "Platform Admin"})
    monkeypatch.setitem(mgr.active_sessions, "s-a", {"email": "analyst.demo@simplicitywealth.com", "name": "A", "role": "Migration Analyst"})

    async def get(sid, path):
        t = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=t, base_url="http://testserver", cookies={"session_id": sid}) as c:
            return await c.get(path)

    home_j = asyncio.run(get("s-j", "/")).text
    home_a = asyncio.run(get("s-a", "/")).text
    setup_a = asyncio.run(get("s-a", "/migration-setup"))
    assert 'aria-label="Demo load"' in home_j and "/api/demo/load" in home_j
    assert 'aria-label="Demo load"' not in home_a
    assert 'aria-label="Target system connection"' in setup_a.text


def test_refresh_button_shows_once_mappings_exist():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "ui" / "templates" / "target_mapping.html").read_text()
    assert "Refresh proposals from canonical model" in html and html.count('action="/api/target-mapping/propose"') == 2

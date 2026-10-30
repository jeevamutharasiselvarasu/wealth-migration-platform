import asyncio
import sqlite3

import httpx
import pytest

from control_plane.services import target_connector as tc
from control_plane.services import target_mapping as tm
from control_plane.services import transformation_rules as tr

CANON = {"Account": ["ACCT_NUM", "CUSTODIAN_CODE", "REP_CODE", "REG_TYPE", "OPEN_DATE"],
         "Client": ["CLIENT_ID", "LEGAL_NAME", "TAX_ID", "CLIENT_TYPE"],
         "Household": ["HH_ID", "HH_NAME", "REP_CODE"]}


CANON_TYPES = {"Account": {"ACCT_NUM": "string", "CUSTODIAN_CODE": "string", "REP_CODE": "string", "REG_TYPE": "string", "OPEN_DATE": "date"},
               "Client": {"CLIENT_ID": "string", "LEGAL_NAME": "string", "TAX_ID": "string", "CLIENT_TYPE": "string"},
               "Household": {"HH_ID": "string", "HH_NAME": "string", "REP_CODE": "string"}}


@pytest.fixture()
def target_service(tmp_path, monkeypatch):
    """The standalone simulated target service's own code, reached through the connector's HTTP seam."""
    import simulated_target_service.app as target_app
    import simulated_target_service.database as tdb

    monkeypatch.setenv("SIMULATED_TARGET_DB", str(tmp_path / "target.db"))
    tdb.bootstrap()
    routes = {"/health": target_app.health, "/schema": target_app.schema}

    def fake_get(url):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else "/"
        return routes[path]()
    monkeypatch.setattr(tc, "_http_get", fake_get)
    return target_app


def test_target_service_api_is_read_only_and_exposes_schema_without_row_data(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from simulated_target_service.app import app as target_app

    monkeypatch.setenv("SIMULATED_TARGET_DB", str(tmp_path / "svc.db"))
    with TestClient(target_app) as client:
        assert client.get("/health").json()["status"] == "ok"
        schema = client.get("/schema").json()
        assert {t["name"] for t in schema["tables"]} >= {"SIMP_ACCT_MASTER", "SIMP_GROUP"} and "SIMP_REF_SEGMENT" in schema["code_lists"]
        assert any(c["generated"] for t in schema["tables"] for c in t["columns"])
        assert "canonical" not in repr(schema).lower()  # the target knows nothing of the platform's model
        assert client.post("/schema").status_code == 405 and client.put("/schema").status_code == 405


def _confirm_target(conn):
    tc.configure(conn, "Admin One", "Simplicity", "http://127.0.0.1:8100", [], True, "Sam")
    tc.infer_model(conn, CANON_TYPES)
    tc.confirm(conn, "Admin One")
    conn.commit()
    return tc.spec(conn)


@pytest.fixture()
def spec(conn, target_service):
    return _confirm_target(conn)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    import control_plane.db as cdb
    monkeypatch.setattr(cdb, "DB_PATH", tmp_path / "t.db")
    cdb.init_db()
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    return c


def test_spec_is_inferred_from_the_running_target_and_limited_to_p0_entities(spec):
    assert spec["status"] == "INFERRED_CONFIRMED" and spec["spec_id"].startswith("INFERRED-")
    assert set(spec["entities"]) == {"Account", "Client", "Household"}
    hints = {f["name"]: f["canonical_hint"] for b in spec["entities"].values() for f in b["fields"]}
    assert hints["SIMP_ACCT_MASTER.ACCT_NO"] == "ACCT_NUM" and hints["SIMP_ACCT_MASTER.OPENED_ON"] == "OPEN_DATE"
    assert hints["SIMP_ADVISOR_ASSIGNMENT.ADVISOR_ID"] == "REP_CODE" and hints["SIMP_CUSTOMER.SEGMENT_CD"] == "CLIENT_TYPE"
    assert hints["SIMP_GROUP.GROUP_REF"] == "HH_ID" and hints["SIMP_CUSTOMER_TAX.TAX_NO"] == "TAX_ID"
    assert hints["SIMP_ACCT_MASTER.ACCT_STATUS"] is None and hints["SIMP_CUSTOMER_TAX.TAX_COUNTRY"] is None  # real gaps stay unmatched
    assert not any(f["column"].endswith("_SK") for b in spec["entities"].values() for f in b["fields"])  # surrogate keys are not mapped


def test_proposals_are_idempotent_and_never_guess_missing_canonical_fields(conn, spec):
    created = tm.propose(conn, "MIG-T", {**CANON, "Client": ["CLIENT_ID", "LEGAL_NAME"]}, spec)
    assert created == 14
    rows = {(m["entity_name"], m["target_field"]): m for m in tm.list_mappings(conn, "MIG-T", spec)}
    assert rows[("Account", "SIMP_ACCT_MASTER.OPENED_ON")]["canonical_field"] == "OPEN_DATE"
    assert rows[("Client", "SIMP_CUSTOMER_TAX.TAX_NO")]["canonical_field"] is None  # TAX_ID not in canonical list: stays unresolved
    with pytest.raises(ValueError, match="No canonical source"):
        tm.review(conn, rows[("Client", "SIMP_CUSTOMER_TAX.TAX_NO")]["target_mapping_id"], "APPROVED", "Ann", "ok")
    assert tm.propose(conn, "MIG-T", CANON, spec) == 0  # idempotent; refreshes the now-resolvable TAX_NO source
    assert {m["target_field"]: m for m in tm.list_mappings(conn, "MIG-T", spec)}["SIMP_CUSTOMER_TAX.TAX_NO"]["canonical_field"] == "TAX_ID"


def test_review_requires_reason_and_rejects_unapproved_rules_and_repeat_decisions(conn, spec):
    tm.propose(conn, "MIG-T", CANON, spec)
    conn.commit()
    m = {(x["entity_name"], x["target_field"]): x for x in tm.list_mappings(conn, "MIG-T", spec)}[("Account", "SIMP_ACCT_MASTER.CUSTODIAN_CD")]
    with pytest.raises(ValueError, match="rejection reason"):
        tm.review(conn, m["target_mapping_id"], "REJECTED", "Ann", "")
    with pytest.raises(ValueError, match="not an approved"):
        tm.review(conn, m["target_mapping_id"], "APPROVED", "Ann", "ok", "RULE:NOPE@1")
    done = tm.review(conn, m["target_mapping_id"], "APPROVED", "Ann", "ok", "TRIM")
    assert done["status"] == "APPROVED" and done["transformation_expression"] == "TRIM" and done["reviewer"] == "Ann"
    with pytest.raises(ValueError, match="already APPROVED"):
        tm.review(conn, m["target_mapping_id"], "REJECTED", "Ann", "again")


def test_preview_applies_rules_reports_findings_and_contains_no_values(conn, spec):
    tr.create_rule_version(conn, "CUST_MAP", "VALUE_MAP", "codes", {"map": {"SCH": "SCHWAB"}}, "Ann")
    tr.decide_rule(conn, "CUST_MAP", 1, "APPROVED", "Bob", "ok")
    conn.commit()
    tm.propose(conn, "MIG-T", CANON, spec)
    conn.commit()
    for m in tm.list_mappings(conn, "MIG-T", spec):
        if m["entity_name"] == "Account" and m["target_field"] in ("SIMP_ACCT_MASTER.CUSTODIAN_CD", "SIMP_ACCT_MASTER.ACCT_NO"):
            tm.review(conn, m["target_mapping_id"], "APPROVED", "Ann", "ok", "RULE:CUST_MAP@1" if m["target_field"] == "SIMP_ACCT_MASTER.CUSTODIAN_CD" else "")
    conn.commit()
    approved = [m for m in tm.list_mappings(conn, "MIG-T", spec) if m["status"] == "APPROVED"]
    rows = {"Account": [{"ACCT_NUM": "A1", "CUSTODIAN_CODE": "SCH"}, {"ACCT_NUM": "", "CUSTODIAN_CODE": "ZZZ"}]}
    result = tm.preview(spec, approved, rows)["Account"]
    assert result["row_count"] == 2
    by = {f["target_field"]: f for f in result["fields"]}
    assert by["SIMP_ACCT_MASTER.ACCT_NO"]["blank_values"] == 1 and by["SIMP_ACCT_MASTER.CUSTODIAN_CD"]["blank_values"] == 0
    assert "SIMP_ACCT_MASTER.ACCT_NO" in result["blank_required"]
    assert set(result["uncovered_required"]) == {"SIMP_ACCT_MASTER.OPENED_ON", "SIMP_ACCT_MASTER.ACCT_STATUS", "SIMP_ACCT_REGISTRATION.REG_CD", "SIMP_ADVISOR_ASSIGNMENT.ADVISOR_ID"}
    assert any("unmapped value 'ZZZ'" in w for w in result["warnings"])
    assert "SCHWAB" not in repr(result) and "A1" not in repr(result)  # counts and findings only


def test_routes_are_role_gated_and_flow_from_proposal_to_preview(conn, tmp_path, monkeypatch, target_service):
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
    assert "Save &amp; test connection" in asyncio.run(call("Platform Admin", "GET", "/target-mapping")).text
    assert "Complete and confirm the one-time target system setup first" in asyncio.run(call("Migration Analyst", "POST", "/api/target-mapping/propose")).headers["location"].replace("+", " ")
    cfg_form = {"target_name": "Simplicity", "endpoint": "http://127.0.0.1:8100", "include_code_lists": "on", "owner": "Sam"}
    for role in ("Domain SME", "Migration Analyst", "Validation Specialist"):
        for path in ("configure", "infer", "confirm"):
            assert "may+not+perform" in asyncio.run(call(role, "POST", f"/api/target-system/{path}", cfg_form if path == "configure" else None)).headers["location"]
    assert "error=" in asyncio.run(call("Platform Admin", "POST", "/api/target-system/confirm")).headers["location"]  # nothing inferred yet
    assert "error=" in asyncio.run(call("Platform Admin", "POST", "/api/target-system/configure", {**cfg_form, "endpoint": "http://example.com"})).headers["location"]
    assert "notice=" in asyncio.run(call("Platform Admin", "POST", "/api/target-system/configure", cfg_form)).headers["location"]
    assert "error=" in asyncio.run(call("Platform Admin", "POST", "/api/target-mapping/propose")).headers["location"]  # inferred but not confirmed
    assert "notice=" in asyncio.run(call("Platform Admin", "POST", "/api/target-system/infer")).headers["location"]
    page = asyncio.run(call("Platform Admin", "GET", "/target-mapping")).text
    assert "Inferred target model" in page and "Confirm target model" in page and "SIMP_ACCT_MASTER.ACCT_NO" in page
    assert "notice=" in asyncio.run(call("Platform Admin", "POST", "/api/target-system/confirm")).headers["location"]
    locked = asyncio.run(call("Platform Admin", "POST", "/api/target-system/configure", cfg_form)).headers["location"]
    assert "error=" in locked and "locked" in locked.replace("+", " ")
    for role in ("Domain SME", "Migration Analyst", "Validation Specialist"):
        assert "may+not+perform" in asyncio.run(call(role, "POST", "/api/target-system/reopen", {"reason": "x"})).headers["location"]
    assert "Reopen setup to reconfigure" in asyncio.run(call("Platform Admin", "GET", "/target-mapping")).text
    assert "error=" in asyncio.run(call("Platform Admin", "POST", "/api/target-system/reopen", {"reason": " "})).headers["location"]
    assert "notice=" in asyncio.run(call("Platform Admin", "POST", "/api/target-system/reopen", {"reason": "Owner changed the code lists"})).headers["location"]
    assert "error=" in asyncio.run(call("Platform Admin", "POST", "/api/target-mapping/propose")).headers["location"]  # mapping paused
    assert "Save &amp; test connection" in asyncio.run(call("Platform Admin", "GET", "/target-mapping")).text
    for step, form in (("configure", cfg_form), ("infer", None), ("confirm", None)):
        assert "notice=" in asyncio.run(call("Platform Admin", "POST", f"/api/target-system/{step}", form)).headers["location"]
    assert "Confirm target model" not in asyncio.run(call("Platform Admin", "GET", "/target-mapping")).text
    assert asyncio.run(call("Migration Analyst", "GET", "/target-setup")).headers["location"] == "/target-mapping#target-system"
    assert "No target mapping proposals" in asyncio.run(call("Migration Analyst", "GET", "/target-mapping")).text
    early = asyncio.run(call("Migration Analyst", "POST", "/api/target-mapping/preview"))
    assert "error=" in early.headers["location"]
    assert "notice=" in asyncio.run(call("Migration Analyst", "POST", "/api/target-mapping/propose")).headers["location"]
    ids = {(m["entity_name"], m["target_field"]): m["target_mapping_id"] for m in tm.list_mappings(conn, "MIG-R", tc.spec(conn))}
    for field in ("SIMP_ACCT_MASTER.ACCT_NO", "SIMP_ACCT_MASTER.CUSTODIAN_CD"):
        ok = asyncio.run(call("Migration Analyst", "POST", f"/api/target-mapping/{ids[('Account', field)]}/review",
                              {"decision": "APPROVED", "comments": "ok"}))
        assert "notice=" in ok.headers["location"]
    sme = asyncio.run(call("Domain SME", "POST", f"/api/target-mapping/{ids[('Account', 'SIMP_ACCT_MASTER.OPENED_ON')]}/review", {"decision": "APPROVED", "comments": "x"}))
    assert "may+not+perform" in sme.headers["location"]
    page = asyncio.run(call("Validation Specialist", "POST", "/api/target-mapping/preview")).text
    assert "Preview (dry run)" in page and "2 source row(s)" in page and "Files joined on ACCT_NUM" in page and "Required target fields without an approved mapping" in page
    assert "SCH" not in page.split("Preview (dry run)")[1]
    assert "Generate ADF package" in asyncio.run(call("Validation Specialist", "GET", "/target-mapping")).text
    assert "may+not+perform" in asyncio.run(call("Domain SME", "POST", "/api/packages/generate")).headers["location"]
    gen = asyncio.run(call("Validation Specialist", "POST", "/api/packages/generate")).headers["location"]
    assert "notice=" in gen and "generated+against+the+DRAFT+contract" in gen
    assert "Nothing+changed" in asyncio.run(call("Validation Specialist", "POST", "/api/packages/generate")).headers["location"]
    pkg_row = conn.execute("SELECT package_id, package_version, yaml_text FROM deployment_packages").fetchone()
    assert pkg_row["package_version"] == 1
    from control_plane.services import package_contract as pc
    doc = pc.validate_text(pkg_row["yaml_text"])
    assert doc.meta.simulated and doc.meta.contract_status == "DRAFT_NOT_AGREED" and len(doc.canonical_to_target) == 2 and doc.target.confirmed_by
    dl = asyncio.run(call("Validation Specialist", "GET", f"/api/packages/{pkg_row['package_id']}/download"))
    assert dl.status_code == 200 and "x-yaml" in dl.headers["content-type"] and dl.text == pkg_row["yaml_text"]
    assert asyncio.run(call("Validation Specialist", "GET", "/api/packages/PKG-NOPE/download")).status_code == 404
    import pytest
    with pytest.raises(ValueError, match="draft contract"):
        pc.validate_text(pkg_row["yaml_text"].replace("canonical_to_target:", "canonical_to_target_x:", 1))
    import yaml as _y
    tampered = _y.safe_load(pkg_row["yaml_text"])
    tampered["environment"]["values"]["password"] = "x"
    with pytest.raises(ValueError, match="secret-like"):
        pc.validate_text(_y.safe_dump(tampered))
    assert "Download YAML" in asyncio.run(call("Validation Specialist", "GET", "/target-mapping")).text
    assert "may+not+perform" in asyncio.run(call("Migration Analyst", "POST", "/api/execution/run")).headers["location"]
    ran = asyncio.run(call("Validation Specialist", "POST", "/api/execution/run")).headers["location"]
    assert "SIMULATED+run+RUN-" in ran and "No+data+left" in ran
    down = asyncio.run(call("Validation Specialist", "POST", "/api/execution/run", {"outage": "on"})).headers["location"]
    assert "FAILED" in down
    failed_id = conn.execute("SELECT run_id FROM execution_runs WHERE status = 'FAILED'").fetchone()[0]
    first_id = conn.execute("SELECT run_id FROM execution_runs WHERE status != 'FAILED'").fetchone()[0]
    assert "error=" in asyncio.run(call("Validation Specialist", "POST", "/api/execution/run", {"retry_of": first_id})).headers["location"]
    assert "SIMULATED+run" in asyncio.run(call("Validation Specialist", "POST", "/api/execution/run", {"retry_of": failed_id})).headers["location"]
    assert "already+retried" in asyncio.run(call("Validation Specialist", "POST", "/api/execution/run", {"retry_of": failed_id})).headers["location"]
    exec_page = asyncio.run(call("Validation Specialist", "GET", "/reconciliation")).text
    assert "SIMULATED data plane" in exec_page and "retry of " + failed_id in exec_page and "Run simulated execution" in exec_page
    assert "Run simulated execution" not in asyncio.run(call("Migration Analyst", "GET", "/reconciliation")).text
    cov_page = asyncio.run(call("Validation Specialist", "GET", "/target-mapping")).text
    assert "Target coverage" in cov_page and "Transformation-assisted" in cov_page
    assert "may+not+perform" in asyncio.run(call("Domain SME", "POST", "/api/target-mapping/adf-package")).headers["location"]
    zipped = asyncio.run(call("Validation Specialist", "POST", "/api/target-mapping/adf-package"))
    assert zipped.status_code == 200 and zipped.headers["content-type"] == "application/zip" and "SIMULATED" in zipped.headers["content-disposition"]
    import io, json, zipfile
    names = zipfile.ZipFile(io.BytesIO(zipped.content))
    mf = json.loads(names.read("manifest.json"))
    assert mf["simulated"] is True and mf["entities"]["Account"]["approvedMappings"] == 2 and mf["generatedBy"] == "Analyst One"
    assert any(e.get("event") == "ADF_PACKAGE_GENERATED" for e in app_module.state_store.audit_log.read_all()) if hasattr(app_module.state_store.audit_log, "read_all") else True


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


def test_target_setup_is_one_time_and_target_is_separate_and_different_from_canonical(conn, target_service):
    import ast
    import pathlib
    import sqlite3 as sq
    import simulated_target_service.database as tdb
    with pytest.raises(ValueError, match="live targets are out of scope"):
        tc.configure(conn, "Ann", "Simplicity", "http://prod.example.com", [], True, "")
    cfg = tc.configure(conn, "Ann", "Simplicity", "http://127.0.0.1:8100", [], True, "")
    assert cfg["status"] == "CONNECTED"
    with pytest.raises(ValueError, match="Infer the target model"):
        tc.confirm(conn, "Ann")
    tc.infer_model(conn, CANON_TYPES)
    tc.confirm(conn, "Ann")
    conn.commit()
    with pytest.raises(ValueError, match="locked"):
        tc.configure(conn, "Ann", "Simplicity", "http://127.0.0.1:8100", [], True, "")
    with pytest.raises(ValueError, match="already confirmed"):
        tc.infer_model(conn, CANON_TYPES)
    spec = tc.spec(conn)
    assert tc.get_config(conn)["confirmed_by"] == "Ann"
    db = sq.connect(tdb.db_path())
    assert db.execute("SELECT COUNT(*) FROM SIMP_ACCT_MASTER").fetchone()[0] == 0  # no migration data, ever
    db.close()
    canonical_names = {f for fields in CANON.values() for f in fields}
    assert not canonical_names & {f["column"] for b in spec["entities"].values() for f in b["fields"]}  # no column name is shared
    # the target service is independent of the platform: it imports nothing from it
    for path in pathlib.Path(tdb.__file__).parent.glob("*.py"):
        imported = {n.module.split(".")[0] for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.ImportFrom) and n.module}
        imported |= {a.name.split(".")[0] for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Import) for a in n.names}
        assert not imported & {"control_plane", "ui", "agents", "data_plane"}, path.name


def test_unreachable_target_is_reported_not_raised(conn, monkeypatch):
    def boom(url):
        raise OSError("connection refused")
    monkeypatch.setattr(tc, "_http_get", boom)
    cfg = tc.configure(conn, "Ann", "Simplicity", "http://127.0.0.1:9", [], True, "")
    assert cfg["status"] == "CONFIGURED" and "Could not reach the target" in cfg["connection_detail"]
    with pytest.raises(ValueError, match="Test the connection"):
        tc.infer_model(conn, CANON_TYPES)


def test_scope_limits_inference_to_selected_tables(conn, target_service):
    tc.configure(conn, "Ann", "Simplicity", "http://127.0.0.1:8100", ["SIMP_GROUP", "SIMP_CUSTOMER"], False, "")
    model = tc.infer_model(conn, CANON_TYPES)["model"]
    tables = {t for b in model["entities"].values() for t in b["table_columns"]}
    assert tables == {"SIMP_GROUP", "SIMP_CUSTOMER"}
    assert all(f["code_list"] is None and f["codes"] == [] for b in model["entities"].values() for f in b["fields"])  # code lists not read


def test_gap_report_lists_missing_unused_and_code_lists(spec):
    report = tm.gap_report(spec, CANON)
    missing = {(g["entity"], g["target_field"]): g for g in report["missing"]}
    assert set(missing) == {("Account", "SIMP_ACCT_MASTER.ACCT_STATUS"), ("Client", "SIMP_CUSTOMER.ONBOARD_DT"), ("Client", "SIMP_CUSTOMER_TAX.TAX_COUNTRY")}
    assert missing[("Account", "SIMP_ACCT_MASTER.ACCT_STATUS")]["required"] and not missing[("Client", "SIMP_CUSTOMER.ONBOARD_DT")]["required"]
    assert {"entity": "Household", "canonical_field": "REP_CODE"} in report["unused"]
    assert {c["target_field"] for c in report["code_lists"]} == {"SIMP_ACCT_REGISTRATION.REG_CD", "SIMP_CUSTOMER.SEGMENT_CD"}


def test_gap_becomes_change_request_the_sme_approves_then_proposals_resolve(conn, tmp_path, monkeypatch, target_service):
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

    asyncio.run(call("Platform Admin", "/api/target-system/configure", {"target_name": "Simplicity", "endpoint": "http://127.0.0.1:8100", "include_code_lists": "on"}))
    asyncio.run(call("Platform Admin", "/api/target-system/infer"))
    asyncio.run(call("Platform Admin", "/api/target-system/confirm"))
    spec = tc.spec(conn)
    assert spec is not None
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
    pending = {m["target_field"]: m for m in tm.list_mappings(conn, "MIG-G", spec)}["SIMP_ACCT_MASTER.ACCT_STATUS"]
    assert pending["canonical_field"] is None  # unresolved until the SME decides and applies
    assert "may+not+perform" in asyncio.run(call("Migration Analyst", f"/api/canonical-changes/{rid}/decision", {"decision": "APPROVED"})).headers["location"]
    asyncio.run(call("Domain SME", f"/api/canonical-changes/{rid}/decision", {"decision": "APPROVED", "note": "ok"}))
    asyncio.run(call("Domain SME", f"/api/canonical-changes/{rid}/apply"))
    refreshed = asyncio.run(call("Migration Analyst", "/api/target-mapping/propose")).headers["location"]
    assert "now+have+a+canonical+source" in refreshed
    assert "Nothing+to+refresh" in asyncio.run(call("Migration Analyst", "/api/target-mapping/propose")).headers["location"]
    after = {m["target_field"]: m for m in tm.list_mappings(conn, "MIG-G", spec)}["SIMP_ACCT_MASTER.ACCT_STATUS"]
    assert after["canonical_field"] == "ACCOUNT_STATUS" and after["status"] == "PROPOSED"


def test_demo_load_is_restricted_to_flagged_account_and_infers_but_does_not_confirm_target(tmp_path, monkeypatch, target_service):
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
    assert ok.status_code == 303 and "inferred" in ok.headers["location"]
    conn = db.get_db_connection() if hasattr(db, "get_db_connection") else sqlite3.connect(tmp_path / "gate.db")
    conn.row_factory = sqlite3.Row
    cfg = tc.get_config(conn)
    assert cfg["status"] == "INFERRED" and cfg["confirmed_by"] is None  # a human confirms the target model, not the demo button
    conn.close()
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


def test_unmatched_columns_get_a_suggested_canonical_name_for_change_requests():
    from control_plane.services.target_inference import suggest_canonical_name as name
    client = ["CLIENT_ID", "LEGAL_NAME", "TAX_ID", "CLIENT_TYPE"]
    assert name("Client", "ONBOARD_DT", client) == "CLIENT_ONBOARD_DATE"
    assert name("Client", "TAX_COUNTRY", client) == "TAX_COUNTRY"
    assert name("Account", "ACCT_STATUS", ["ACCT_NUM"]) == "ACCOUNT_STATUS"


def test_fields_added_by_change_requests_match_regardless_of_declared_type(conn, target_service):
    typed = {**CANON_TYPES, "Client": {**CANON_TYPES["Client"], "CLIENT_ONBOARD_DATE": ""}, "Account": {**CANON_TYPES["Account"], "ACCOUNT_STATUS": ""}}
    tc.configure(conn, "Ann", "Simplicity", "http://127.0.0.1:8100", [], True, "")
    model = tc.infer_model(conn, typed)["model"]
    hints = {f["name"]: f["canonical_hint"] for b in model["entities"].values() for f in b["fields"]}
    assert hints["SIMP_CUSTOMER.ONBOARD_DT"] == "CLIENT_ONBOARD_DATE" and hints["SIMP_ACCT_MASTER.ACCT_STATUS"] == "ACCOUNT_STATUS"


def test_mappings_from_an_earlier_target_model_do_not_silently_block_new_proposals(conn, spec):
    tm.ensure_table(conn)
    conn.execute("INSERT INTO target_mappings (target_mapping_id, migration_id, entity_name, target_field, canonical_field, status, spec_version, created_utc) "
                 "VALUES ('TGT-OLD', 'MIG-OLD', 'Account', 'SIMP_ACCT_MASTER.ACCT_NO', 'ACCT_NUM', 'APPROVED', 'old-model', 'now')")
    with pytest.raises(ValueError, match="earlier target model"):
        tm.propose(conn, "MIG-OLD", CANON, spec)
    assert tm.propose(conn, "MIG-NEW", CANON, spec) == 14  # a new migration maps to the confirmed target normally


# ---------------------------------------------------------------- ADF package (simulated Azure values)
def _adf_rule(kind, params, status="APPROVED"):
    return {"rule_type": kind, "params": tr_validate(kind, params), "status": status}


def tr_validate(kind, params):
    from control_plane.services import transformation_rules as tr
    return tr.validate_params(kind, params)


def _adf_spec():
    return {"spec_version": "v1", "target_system": "Simplicity", "entities": {"Account": {"target_table": "SIMP_ACCT_MASTER", "fields": [
        {"name": f"T.{c}", "table": "T", "column": c, "type": ty, "required": req} for c, ty, req in
        (("ACCT_NO", "string", True), ("OPEN_DT", "date", True), ("REG_CD", "string", False), ("REP_ID", "string", True))]}}}


def _adf_maps(**over):
    base = [("T.ACCT_NO", "ACCT_NUM", "DIRECT_COPY"), ("T.OPEN_DT", "OPEN_DATE", "RULE:D@1"), ("T.REG_CD", "REG_TYPE", "RULE:V@1")]
    return [{"entity_name": "Account", "target_field": t, "canonical_field": c, "transformation_expression": e, "status": over.get(t, "APPROVED"), "reviewer": "Ann"}
            for t, c, e in base]


def test_adf_package_from_approved_mappings_only_and_simulated():
    import io, json, zipfile
    from control_plane.services import adf_package, azure_simulated
    env = azure_simulated.simulated_environment("MIG-1")
    rules = {"RULE:D@1": _adf_rule("DATE_FORMAT", {"input_formats": ["MM/DD/YYYY", "YYYYMMDD"]}),
             "RULE:V@1": _adf_rule("VALUE_MAP", {"map": {"IND'V": "INDIVIDUAL"}})}
    pkg = adf_package.build("MIG-1", _adf_spec(), _adf_maps(**{"T.REG_CD": "PROPOSED"}), rules, env, "Ann")
    m = pkg["manifest"]
    assert m["simulated"] is True and m["deployed"] is False and m["entities"]["Account"]["approvedMappings"] == 2
    assert any("T.REP_ID" in w for w in m["warnings"])  # required field without an approved mapping is reported
    flow = json.loads(pkg["files"]["dataflow/DF_Account_T.json"])["properties"]["typeProperties"]["scriptLines"]
    script = "\n".join(flow)
    assert "ACCT_NO = ACCT_NUM" in script and "toDate(trim(OPEN_DATE), 'MM/dd/yyyy')" in script and "REG_CD" not in script
    zf = zipfile.ZipFile(io.BytesIO(adf_package.to_zip(pkg)))
    names = set(zf.namelist())
    assert {"manifest.json", "arm/ARMTemplateForFactory.json", "pipeline/PL_XODUS_MIG_1.json", "linkedService/LS_SQL_Target_Sim.json", "README_SIMULATED.txt"} <= names
    blob = "".join(zf.read(n).decode() for n in names).lower()
    assert "accountkey" not in blob and "password" not in blob and "00000000-0000-0000-0000-000000000000" in blob
    assert "AzureKeyVaultSecret" in zf.read("linkedService/LS_SQL_Target_Sim.json").decode()


def test_adf_translate_each_rule_and_refuses_unregistered():
    import pytest
    from control_plane.services import adf_package as a
    assert a.translate("RULE:V@1", "REG", _adf_rule("VALUE_MAP", {"map": {"IND'V": "X"}})) == \
        "iif(isNull(REG) || trim(REG) == '', REG, case(upper(trim(REG)), 'IND\\'V', 'X', REG))"
    assert "toDecimal" in a.translate("R", "AMT", _adf_rule("NUMBER_CLEAN", {"strip_chars": "$,", "decimals": 2}))
    assert "concatWS(' ', trim(A), trim(B))" in a.translate("R", "A", _adf_rule("CONCAT", {"sources": ["A", "B"]}))
    assert "[2]" in a.translate("R", "A", _adf_rule("SPLIT_PART", {"delimiter": " ", "index": 1}))
    assert a.translate("R", "A", _adf_rule("DEFAULT_IF_BLANK", {"default": "UNKNOWN"})).startswith("iif(")
    assert "'N/A'" in a.translate("R", "A", _adf_rule("IF_EQUALS", {"when_equals": "N/A", "then": ""}))
    for bad in (None, _adf_rule("VALUE_MAP", {"map": {"A": "B"}}, status="DRAFT")):
        with pytest.raises(ValueError, match="approved, registered"):
            a.translate("RULE:X@1", "A", bad)
    with pytest.raises(ValueError, match="safe column name"):
        a.translate("DIRECT_COPY", "A; DROP", None)


def test_adf_package_needs_an_approved_mapping():
    import pytest
    from control_plane.services import adf_package, azure_simulated
    with pytest.raises(ValueError, match="Approve at least one"):
        adf_package.build("M", _adf_spec(), _adf_maps(**{"T.ACCT_NO": "PROPOSED", "T.OPEN_DT": "REJECTED", "T.REG_CD": "PROPOSED"}), {}, azure_simulated.simulated_environment("M"), "Ann")


def test_coverage_splits_direct_transformed_pending_and_counts_only_approved():
    spec = {"entities": {"Account": {"fields": [
        {"name": "A", "required": True}, {"name": "B", "required": True}, {"name": "C", "required": False},
        {"name": "D", "required": True}, {"name": "E", "required": False}]}}}
    def m(f, st, expr="DIRECT_COPY"):
        return {"entity_name": "Account", "target_field": f, "status": st, "transformation_expression": expr}
    cov = tm.coverage(spec, [m("A", "APPROVED"), m("B", "APPROVED", "RULE:X@1"), m("C", "PROPOSED"), m("D", "REJECTED")])["Account"]
    assert (cov["direct"], cov["transformed"], cov["effective"], cov["pending"], cov["unmapped"]) == (1, 1, 2, 1, 2)
    assert cov["effective_pct"] == 40 and (cov["required_effective"], cov["required_total"]) == (2, 3)
    assert tm.coverage(None, []) == {}


def test_package_refuses_unapproved_rules_versions_on_change_and_exports_schema(tmp_path):
    import sqlite3 as sq
    import pytest
    from control_plane.services import package_contract as pc
    spec = {"entities": {"Account": {"fields": [{"name": "T.A", "table": "T", "column": "A"}]}}}
    base = dict(migration_id="M", source_system="CoreCap", target_system="Simplicity", firm={"firm_id": "F", "config_version": "1", "canonical_contract_ref": "c"},
                target={"name": "S", "model_version": "v", "confirmed_by": "x", "confirmed_utc": "u"}, source_files=[], spec=spec, scenarios=[],
                source_mappings=[{"entity": "Account", "source_field": "ACCT", "canonical_field": "ACCT_NUM", "transformation": "DIRECT_COPY"}],
                environment={"simulated": True, "banner": "b", "factoryName": "f"})
    m = {"entity_name": "Account", "target_field": "T.A", "canonical_field": "ACCT_NUM", "status": "APPROVED", "reviewer": "Ann", "transformation_expression": "RULE:X@1"}
    with pytest.raises(ValueError, match="not an approved rule"):
        pc.build_body(approved_target=[m], rules={"RULE:X@1": {"status": "DRAFT", "rule_type": "VALUE_MAP", "params": {}, "description": "d"}}, **base)
    with pytest.raises(ValueError, match="Approve at least one target mapping"):
        pc.build_body(approved_target=[{**m, "status": "PROPOSED"}], rules={}, **base)
    body = pc.build_body(approved_target=[{**m, "transformation_expression": "DIRECT_COPY"}], rules={}, **base)
    conn = sq.connect(tmp_path / "p.db")
    conn.row_factory = sq.Row
    first = pc.generate(conn, "M", "Ann", body)
    assert first == {**first, "package_version": 1, "new": True}
    assert pc.generate(conn, "M", "Ann", body)["new"] is False
    body2 = pc.build_body(approved_target=[{**m, "transformation_expression": "DIRECT_COPY", "target_field": "T.A"}], rules={}, **{**base, "firm": {**base["firm"], "config_version": "2"}})
    assert pc.generate(conn, "M", "Ann", body2)["package_version"] == 2
    assert [p["package_version"] for p in pc.list_packages(conn, "M")] == [2, 1]
    assert "canonical_to_target" in pc.json_schema()["properties"]

import asyncio
import sqlite3

import httpx
import pytest

from control_plane.services import transformation_rules as tr


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import control_plane.db as cdb
    monkeypatch.setattr(cdb, "DB_PATH", tmp_path / "rules.db")
    cdb.init_db()
    return tmp_path / "rules.db"


def _run(rule_type, params, value, record=None):
    return tr.apply_rule(rule_type, tr.validate_params(rule_type, params), value, record)


def test_value_map_is_case_insensitive_and_reports_unmapped_values():
    params = {"map": {"sch": "SCHWAB"}, "case_insensitive": True}
    assert _run("VALUE_MAP", params, " Sch ") == ("SCHWAB", None)
    value, warning = _run("VALUE_MAP", params, "XYZ")
    assert value == "XYZ" and "unmapped value" in warning


def test_value_map_rejects_conflicts_and_empty_maps():
    with pytest.raises(ValueError):
        tr.validate_params("VALUE_MAP", {"map": {}})
    with pytest.raises(ValueError):
        tr.validate_params("VALUE_MAP", {"map": {"a": "1", "A": "2"}})


def test_date_format_parses_declared_formats_only():
    params = {"input_formats": ["MM/DD/YYYY", "YYYYMMDD"]}
    assert _run("DATE_FORMAT", params, "3/7/2021")[0] == "2021-03-07"
    assert _run("DATE_FORMAT", params, "20210307")[0] == "2021-03-07"
    value, warning = _run("DATE_FORMAT", params, "07-Mar-2021")
    assert value == "07-Mar-2021" and "none of the declared formats" in warning
    with pytest.raises(ValueError):
        tr.validate_params("DATE_FORMAT", {"input_formats": ["%Y; drop"]})


def test_number_clean_handles_currency_parentheses_and_rounding():
    params = {"strip_chars": "$,", "decimals": 2, "parentheses_negative": True}
    assert _run("NUMBER_CLEAN", params, "$1,234.567")[0] == "1234.57"
    assert _run("NUMBER_CLEAN", params, "($50)")[0] == "-50.00"
    assert "not a number" in _run("NUMBER_CLEAN", params, "abc")[1]
    with pytest.raises(ValueError):
        tr.validate_params("NUMBER_CLEAN", {"strip_chars": "x"})


def test_concat_split_default_and_conditional_rules():
    assert _run("CONCAT", {"sources": ["first_name", "last_name"], "separator": " "}, "Ann",
                {"FIRST_NAME": "Ann", "LAST_NAME": "Lee"})[0] == "Ann Lee"
    value, warning = _run("CONCAT", {"sources": ["A", "B"]}, "x", {"A": "x", "B": ""})
    assert value == "x" and "blank source" in warning
    assert _run("SPLIT_PART", {"delimiter": " ", "index": 1}, "Ann Lee")[0] == "Lee"
    assert _run("DEFAULT_IF_BLANK", {"default": "UNKNOWN"}, "  ")[0] == "UNKNOWN"
    assert _run("DEFAULT_IF_BLANK", {"default": "UNKNOWN"}, "ok")[0] == "ok"
    assert _run("IF_EQUALS", {"when_equals": "N/A", "then": ""}, "N/A")[0] == ""
    assert _run("IF_EQUALS", {"when_equals": "N/A", "then": ""}, "x")[0] == "x"
    with pytest.raises(ValueError):
        tr.validate_params("EVAL", {"code": "1+1"})


def test_leading_zeros_survive_value_and_split_rules():
    assert _run("DEFAULT_IF_BLANK", {"default": "0"}, "000847")[0] == "000847"
    assert _run("SPLIT_PART", {"delimiter": "-", "index": 0}, "000847-A")[0] == "000847"


def test_versions_are_immutable_and_only_approved_versions_execute(db):
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    v1 = tr.create_rule_version(conn, "cust_map", "VALUE_MAP", "custodian codes", {"map": {"SCH": "SCHWAB"}}, "Ann")
    assert v1["expression"] == "RULE:CUST_MAP@1" and v1["status"] == "DRAFT"
    conn.commit()
    assert not SchemaMapperAgent.supports_transformation("RULE:CUST_MAP@1")
    with pytest.raises(ValueError):
        SchemaMapperAgent(approved_transformations={"C": "RULE:CUST_MAP@1"})
    tr.decide_rule(conn, "CUST_MAP", 1, "APPROVED", "Bob", "ok")
    conn.commit()
    assert SchemaMapperAgent.supports_transformation("RULE:CUST_MAP@1")
    with pytest.raises(ValueError):
        tr.decide_rule(conn, "CUST_MAP", 1, "REJECTED", "Bob", "late")
    v2 = tr.create_rule_version(conn, "CUST_MAP", "VALUE_MAP", "more codes", {"map": {"SCH": "SCHWAB", "FID": "FIDELITY"}}, "Ann")
    assert v2["version"] == 2 and v2["status"] == "DRAFT"
    assert not SchemaMapperAgent.supports_transformation("RULE:CUST_MAP@2")
    with pytest.raises(ValueError):
        tr.create_rule_version(conn, "CUST_MAP", "TRIM_ALL", "x", {}, "Ann")
    with pytest.raises(ValueError):
        tr.decide_rule(conn, "CUST_MAP", 2, "REJECTED", "Bob", "")  # reason required


def test_retire_is_blocked_while_an_approved_mapping_uses_the_version(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tr.create_rule_version(conn, "TRIM_X", "SPLIT_PART", "first token", {"delimiter": " ", "index": 0}, "Ann")
    tr.decide_rule(conn, "TRIM_X", 1, "APPROVED", "Bob", "ok")
    conn.execute("INSERT INTO canonical_mappings (mapping_id, migration_id, entity_name, source_field, canonical_field, "
                 "mapping_type, transformation_expression, confidence_score, is_confirmed, created_by, updated_utc) "
                 "VALUES ('M1', 'MIG', 'Client', 'NAME', 'LEGAL_NAME', 'APPROVED_ALIAS', 'RULE:TRIM_X@1', 0.9, 1, 'x', 'now')")
    with pytest.raises(ValueError, match="used by 1"):
        tr.retire_rule(conn, "TRIM_X", 1, "Bob")
    conn.execute("DELETE FROM canonical_mappings")
    assert tr.retire_rule(conn, "TRIM_X", 1, "Bob")["status"] == "RETIRED"


def test_mapper_applies_approved_rules_for_renamed_and_identity_columns(db):
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tr.create_rule_version(conn, "FULL_NAME", "CONCAT", "first + last", {"sources": ["FIRST_NAME", "LAST_NAME"], "separator": " "}, "Ann")
    tr.create_rule_version(conn, "OPEN_ISO", "DATE_FORMAT", "us dates", {"input_formats": ["MM/DD/YYYY"]}, "Ann")
    for rid in ("FULL_NAME", "OPEN_ISO"):
        tr.decide_rule(conn, rid, 1, "APPROVED", "Bob", "ok")
    conn.commit()
    mapper = SchemaMapperAgent(
        custom_catalog={"FIRST_NAME": "LEGAL_NAME"},
        approved_transformations={"FIRST_NAME": "RULE:FULL_NAME@1", "OPEN_DATE": "RULE:OPEN_ISO@1"},
    )
    result = mapper.map_source_to_canonical("Account", [
        {"ACCT_NUM": "C1", "FIRST_NAME": "Ann", "LAST_NAME": "Lee", "OPEN_DATE": "3/7/2021"},
        {"ACCT_NUM": "C2", "FIRST_NAME": "Bo", "LAST_NAME": "", "OPEN_DATE": "bad"},
    ])
    rows = result["mapped_records"]
    assert rows[0]["LEGAL_NAME"] == "Ann Lee" and rows[0]["OPEN_DATE"] == "2021-03-07"
    assert rows[1]["LEGAL_NAME"] == "Bo" and rows[1]["OPEN_DATE"] == "bad"
    lineage = {l["source_field"]: l for l in result["field_lineages"]}
    assert lineage["OPEN_DATE"]["rule_applied"].startswith("APPROVED_RULE:OPEN_ISO@1")
    warnings = " ".join(result["warnings"])
    assert "FULL_NAME@1: blank source field(s): LAST_NAME" in warnings and "OPEN_ISO@1" in warnings


def test_routes_are_role_gated_and_rule_can_be_applied_during_mapping_review(db, tmp_path, monkeypatch):
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-R"
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Analyst One", "role": "Migration Analyst"})

    async def call(role, method, path, data=None):
        app_module.session_mgr.active_sessions["sid"]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.request(method, path, data=data, follow_redirects=False,
                                        headers={"accept": "text/html"})

    form = {"rule_id": "CUST_MAP", "rule_type": "VALUE_MAP", "description": "custodian codes",
            "params_json": '{"map": {"SCH": "SCHWAB"}}'}
    denied = asyncio.run(call("Validation Specialist", "POST", "/api/transformation-rules", form))
    assert denied.status_code == 303 and "may+not+perform" in denied.headers["location"]
    denied = asyncio.run(call("Domain SME", "POST", "/api/transformation-rules/CUST_MAP/1/decision", {"decision": "APPROVED"}))
    assert denied.status_code == 303 and "may+not+perform" in denied.headers["location"]
    check = sqlite3.connect(db)
    tr.ensure_table(check)
    assert check.execute("SELECT COUNT(*) FROM transformation_rules").fetchone()[0] == 0
    infer_form = {"rule_text": "- Map SCH to SCHWAB\n- Make it nicer"}
    for role in ("Validation Specialist", "Domain SME"):
        assert "may+not+perform" in asyncio.run(call(role, "POST", "/api/transformation-rules/infer", infer_form)).headers["location"]
    assert "Infer draft rules" in asyncio.run(call("Migration Analyst", "GET", "/transformation-rules")).text
    inferred = asyncio.run(call("Migration Analyst", "POST", "/api/transformation-rules/infer", infer_form)).headers["location"]
    assert "notice=" in inferred and "1+draft+rule" in inferred and "not+recognised" in inferred
    assert "error=" in asyncio.run(call("Migration Analyst", "POST", "/api/transformation-rules/infer", {"rule_text": "nonsense"})).headers["location"]
    assert check.execute("SELECT COUNT(*) FROM transformation_rules WHERE status = 'DRAFT'").fetchone()[0] == 1
    check.execute("DELETE FROM transformation_rules")
    check.commit()
    bad = asyncio.run(call("Migration Analyst", "POST", "/api/transformation-rules", {**form, "params_json": '{"map": {}}'}))
    assert "error=" in bad.headers["location"]
    ok = asyncio.run(call("Migration Analyst", "POST", "/api/transformation-rules", form))
    assert "notice=" in ok.headers["location"]
    page = asyncio.run(call("Migration Analyst", "GET", "/transformation-rules")).text
    assert "RULE:CUST_MAP@1" in page and "DRAFT" in page and "Approve version" in page
    asyncio.run(call("Migration Analyst", "POST", "/api/transformation-rules/CUST_MAP/1/decision", {"decision": "APPROVED", "comments": "ok"}))

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO mapping_suggestions (suggestion_id, run_id, migration_id, entity_name, source_field, canonical_field, "
                 "proposed_rule, confidence_score, status, created_utc) VALUES ('S1', 'R1', 'MIG-R', 'Account', 'CUST', 'CUSTODIAN_CODE', "
                 "'DIRECT_COPY', 0.8, 'PENDING', '2026-01-01')")
    conn.commit()
    pre = asyncio.run(call("Migration Analyst", "GET", "/agent-pipeline"))
    assert pre.status_code == 200
    refused = asyncio.run(call("Migration Analyst", "POST", "/api/mapping-suggestions/S1/review",
                               {"decision": "APPROVED", "reviewer": "x", "comments": "ok", "rule_override": "RULE:NOPE@1"}))
    assert refused.status_code == 400
    done = asyncio.run(call("Migration Analyst", "POST", "/api/mapping-suggestions/S1/review",
                            {"decision": "APPROVED", "reviewer": "x", "comments": "ok", "rule_override": "RULE:CUST_MAP@1"}))
    assert done.status_code == 303
    row = conn.execute("SELECT transformation_expression, created_by FROM canonical_mappings WHERE source_field = 'CUST'").fetchone()
    assert row == ("RULE:CUST_MAP@1", "Analyst One")


def test_bare_custodian_column_is_proposed_as_custodian_code(db):
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    result = SchemaMapperAgent().map_source_to_canonical("Account", [{"LEGACY_ACCT_NO": "AC-1", "CUSTODIAN": "CUST-A"}])
    lineage = {l["source_field"]: l["canonical_field"] for l in result["field_lineages"]}
    assert lineage["CUSTODIAN"] == "CUSTODIAN_CODE"


# ---------------------------------------------------------------- XODUS-008 inference from business-rule text
def test_text_rules_infer_validated_proposals_and_report_what_is_not_recognised():
    from control_plane.services import rule_inference as ri
    text = ("# heading ignored\n- Map SCH to SCHWAB, FID to FIDELITY\n- Dates are MM/DD/YYYY or YYYYMMDD\n- Strip $ and , and keep 2 decimals for amounts\n"
            "- Concatenate FIRST_NAME and LAST_NAME with a space\n- Split on \"-\" and take the second part\n- Default to \"UNKNOWN\" if blank\n"
            "- Replace \"N/A\" with \"\"\n- Run DROP TABLE accounts\n- Make it nicer")
    out = ri.infer(text)
    kinds = [r.get("rule_type") for r in out]
    assert kinds == ["VALUE_MAP", "DATE_FORMAT", "NUMBER_CLEAN", "CONCAT", "SPLIT_PART", "DEFAULT_IF_BLANK", "IF_EQUALS", None, None]
    assert out[0]["params"]["map"] == {"SCH": "SCHWAB", "FID": "FIDELITY"} and out[3]["params"]["sources"] == ["FIRST_NAME", "LAST_NAME"]
    assert out[4]["params"] == {"delimiter": "-", "index": 1} and out[1]["params"]["input_formats"] == ["MM/DD/YYYY", "YYYYMMDD"]
    assert not out[7]["recognised"] and "No supported pattern" in out[7]["reason"]


def test_inferred_rules_are_drafts_deduplicated_and_unusable_until_approved(tmp_path, monkeypatch):
    import sqlite3
    from control_plane.services import rule_inference as ri, transformation_rules as tr
    conn = sqlite3.connect(tmp_path / "r.db")
    conn.row_factory = sqlite3.Row
    tr.ensure_table(conn)
    first = ri.save_drafts(conn, "Map SCH to SCHWAB\nGibberish line", "Ann")
    assert len(first["created"]) == 1 and len(first["unrecognised"]) == 1
    rule = tr.list_rules(conn)[0]
    assert rule["status"] == "DRAFT" and 'From business rule: "Map SCH to SCHWAB"' in rule["description"] and rule["created_by"] == "Ann"
    again = ri.save_drafts(conn, "Map SCH to SCHWAB", "Ann")
    assert again["created"] == [] and len(again["skipped"]) == 1 and len(tr.list_rules(conn)) == 1
    assert tr.list_rules(conn)[0]["status"] == "DRAFT"  # still needs a human approval


def test_business_rules_asset_registers_from_setup_and_feeds_rule_inference(db, tmp_path, monkeypatch):
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-K"
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid", {"email": "t@example.invalid", "name": "Ann", "role": "Migration Analyst"})

    async def call(method, path, **kw):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.request(method, path, follow_redirects=False, headers={"accept": "text/html"}, **kw)

    rules_md = b"# Rules\n- Map SCH to SCHWAB\n- Default to \"UNKNOWN\" if blank\n- Do something vague\n"
    # The Business rules option on the setup page must register (it was rejected as an unsupported category before).
    for category in ("business_rules", "target_schema", "firm_profile", "questionnaire"):
        up = asyncio.run(call("POST", "/api/knowledge/file-upload", data={"category": category, "return_to": "/migration-setup"},
                              files={"file": (f"{category}.md", rules_md, "text/markdown")}))
        assert up.status_code == 303, category
    bad = asyncio.run(call("POST", "/api/knowledge/file-upload", data={"category": "nonsense"}, files={"file": ("x.md", rules_md, "text/markdown")}))
    assert bad.status_code == 400
    doc = next(d for d in store.firm_knowledge_docs if d["category"] == "business_rules")
    assert doc["full_text"].startswith("# Rules")
    page = asyncio.run(call("GET", "/transformation-rules")).text
    assert f"{doc['id']} · business_rules.md" in page and "Infer draft rules from asset" in page
    none = asyncio.run(call("POST", "/api/transformation-rules/infer", data={"asset_id": "DOC-999"}))
    assert "error=" in none.headers["location"]
    done = asyncio.run(call("POST", "/api/transformation-rules/infer", data={"asset_id": doc["id"]})).headers["location"]
    assert "2+draft+rule" in done and "not+recognised" in done
    check = sqlite3.connect(db)
    descs = [r[0] for r in check.execute("SELECT description FROM transformation_rules WHERE status = 'DRAFT'")]
    assert len(descs) == 2 and all(f"asset {doc['id']}, business_rules.md" in d for d in descs)


def test_demo_manifest_registers_a_business_rules_asset():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / "demo_data" / "knowledge" / "corecap_business_rules.md").read_text()
    from control_plane.services import rule_inference as ri
    assert sum(1 for r in ri.infer(text) if r["recognised"]) == 5
    assert "corecap_business_rules.md" in (root / "ui" / "app.py").read_text()


def test_notice_text_is_cut_at_a_word_boundary():
    from ui.app import _shorten
    long = "Synthetic demo rules for the CoreCap pilot. Each line is a business rule that becomes a draft"
    out = _shorten(long, 60)
    assert out.endswith("…") and len(out) <= 61 and not out.rstrip("…").endswith(("Migratio", "bus"))
    assert _shorten("short text", 60) == "short text"


def test_strict_value_map_fails_blank_and_unlisted_values():
    params = tr.validate_params("VALUE_MAP", {"map": {"ira": "IRA"}, "strict": True})
    assert _run("VALUE_MAP", params, "Ira") == ("IRA", None)
    assert _run("VALUE_MAP", params, "Estate")[0] is None and "unmapped value" in _run("VALUE_MAP", params, "Estate")[1]
    assert _run("VALUE_MAP", params, "")[0] is None and "blank" in _run("VALUE_MAP", params, "  ")[1]
    assert tr.is_strict("VALUE_MAP", params) and not tr.is_strict("VALUE_MAP", {"map": {"A": "B"}, "case_insensitive": True})


def test_if_blank_derives_a_status_from_whether_a_field_is_filled():
    params = tr.validate_params("IF_BLANK", {"when_blank": "Open", "when_filled": "Closed"})
    assert _run("IF_BLANK", params, "") == ("Open", None) and _run("IF_BLANK", params, None) == ("Open", None)
    assert _run("IF_BLANK", params, "2024-01-31") == ("Closed", None)
    with pytest.raises(ValueError):
        tr.validate_params("IF_BLANK", {"when_blank": "Open"})
    from control_plane.services import adf_package
    rule = {"status": "APPROVED", "rule_type": "IF_BLANK", "params": params}
    expr = adf_package.translate("RULE:X@1", "CLOSED_DATE", rule)
    assert expr == "iif(isNull(CLOSED_DATE) || trim(CLOSED_DATE) == '', 'Open', 'Closed')"


def test_lookup_returns_the_parent_value_and_reports_misses():
    params = tr.validate_params("LOOKUP", {"parent_file": "advisors.csv", "parent_key": "Id", "return_column": "Name"})
    table = tr.build_lookup([{"ID": "a1", "name": "Pat"}, {"ID": "a2", "name": ""}], params)
    ctx = {tr.lookup_key(params): table}
    assert tr.apply_rule("LOOKUP", params, " A1 ", None, ctx) == ("Pat", None)
    assert tr.apply_rule("LOOKUP", params, "zz", None, ctx) == (None, "no matching row in the lookup file")
    assert tr.apply_rule("LOOKUP", params, "a2", None, ctx)[0] is None
    assert tr.apply_rule("LOOKUP", params, "", None, ctx) == (None, "blank lookup key")
    assert tr.apply_rule("LOOKUP", params, "a1", None, {}) == (None, "lookup file not available")


def test_lookup_needs_file_key_and_column():
    with pytest.raises(ValueError):
        tr.validate_params("LOOKUP", {"parent_file": "advisors.csv", "parent_key": "Id"})


def test_lookup_translates_to_a_cached_lookup_expression():
    from control_plane.services import adf_package
    rule = {"status": "APPROVED", "rule_type": "LOOKUP", "params": {"parent_file": "advisors.csv", "parent_key": "Id", "return_column": "Name"}}
    expr = adf_package.translate("RULE:X@1", "ADVISOR_ID", rule)
    assert "lkp_advisors#lookup(trim(ADVISOR_ID)).Name" in expr

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

from control_plane.services import rule_inference as ri
from control_plane.services import rule_suggestions as rs
from control_plane.services import transformation_rules as tr

ROOT = Path(__file__).resolve().parents[1]
CROSS = {"custodian": {"SCH": "SCHWAB"}, "registration": {}, "client_type": {"INDV": "INDIVIDUAL"}}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import control_plane.db as cdb
    monkeypatch.setattr(cdb, "DB_PATH", tmp_path / "rules.db")
    cdb.init_db()
    return tmp_path / "rules.db"


def _col(field, values, entity="Account", file="accounts.csv"):
    return {"entity": entity, "file": file, "field": field, "values": values}


def test_business_rules_file_has_only_recognised_rule_lines():
    text = (ROOT / "demo_data" / "knowledge" / "corecap_business_rules.md").read_text()
    results = ri.infer(text)
    assert results and all(r["recognised"] for r in results), [r for r in results if not r["recognised"]]
    assert {r["rule_type"] for r in results} == {"VALUE_MAP", "DATE_FORMAT", "NUMBER_CLEAN", "CONCAT"}
    assert not any(r["rule_type"] == "DEFAULT_IF_BLANK" for r in results)  # decisions note: never invent defaults


def test_suggests_date_number_and_lookup_rules_from_data():
    cols = [_col("OPEN_DT", ["03/15/2021", "11/02/2020"]), _col("BALANCE", ["$1,200.50", "(300)"]),
            _col("CUSTODIAN", ["SCH", "ZZZ", "SCH", "SCH"])]
    out = {s["source_field"]: s for s in rs.suggest(cols, [], CROSS)}
    assert out["OPEN_DT"]["rule_type"] == "DATE_FORMAT" and out["OPEN_DT"]["params"]["input_formats"][0] == "MM/DD/YYYY"
    assert out["BALANCE"]["rule_type"] == "NUMBER_CLEAN" and out["BALANCE"]["params"]["strip_chars"] == "$,"
    lookup = out["CUSTODIAN"]
    assert lookup["rule_type"] == "VALUE_MAP" and lookup["needs_input"] == 1
    entries = {e["value"]: e for e in lookup["entries"]}
    assert entries["SCH"]["target"] == "SCHWAB" and entries["SCH"]["known"] and entries["ZZZ"]["target"] == "" and not entries["ZZZ"]["known"]


def test_nothing_is_suggested_for_identifiers_or_already_iso_dates():
    cols = [_col("LEGACY_ACCT_NO", ["A-1", "A-2", "A-3"]), _col("OPEN_DT", ["2021-03-15", "2020-11-02"])]
    assert rs.suggest(cols, [], CROSS) == []


def test_existing_rule_that_fits_is_suggested_instead_of_a_duplicate(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tr.create_rule_version(conn, "CUST_MAP", "VALUE_MAP", "From business rule: x", {"map": {"SCH": "SCHWAB", "FID": "FIDELITY"}}, "Ann")
    conn.commit()
    rules = tr.list_rules(conn)
    out = rs.suggest([_col("CUSTODIAN", ["SCH", "FID", "ZZZ"])], rules, CROSS)
    assert len(out) == 1 and out[0]["kind"] == "EXISTING" and out[0]["expression"] == "RULE:CUST_MAP@1"
    assert out[0]["origin"] == "business-rules file" and out[0]["uncovered"] == ["ZZZ"]


def test_concat_suggested_when_name_parts_exist_without_full_name():
    cols = [_col("FIRST_NAME", ["A"], "Client", "c.csv"), _col("LAST_NAME", ["B"], "Client", "c.csv")]
    out = rs.suggest(cols, [], CROSS)
    assert any(s["rule_type"] == "CONCAT" and s["params"]["sources"] == ["FIRST_NAME", "LAST_NAME"] for s in out)


def _client(monkeypatch, tmp_path, role="Migration Analyst"):
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-S"
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid", {"email": "t@example.invalid", "name": "Ann", "role": role})

    async def call(method, path, **kw):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as c:
            return await c.request(method, path, follow_redirects=False, headers={"accept": "text/html"}, **kw)
    return store, call


def test_inference_preview_explains_each_line_and_creates_nothing(db, tmp_path, monkeypatch):
    _, call = _client(monkeypatch, tmp_path)
    text = "# comment\nMap SCH to SCHWAB\nThis is just prose about custodians\n"
    page = asyncio.run(call("POST", "/api/transformation-rules/infer-preview", data={"rule_text": text}))
    assert page.status_code == 200
    assert "What was inferred" in page.text and "1 of 2 line(s) recognised" in page.text
    assert "Map SCH to SCHWAB" in page.text and "Not recognised" in page.text and "No supported pattern matched" in page.text
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM transformation_rules").fetchone()[0] == 0


def test_suggestion_endpoint_saves_a_draft_with_only_the_targets_the_reviewer_supplied(db, tmp_path, monkeypatch):
    _, call = _client(monkeypatch, tmp_path)
    data = {"rule_type": "VALUE_MAP", "rule_id": "SUG_CUSTODIAN_AB12", "source_field": "CUSTODIAN", "evidence": "2 coded values",
            "value::1": "SCH", "target::1": "SCHWAB", "value::2": "ZZZ", "target::2": ""}
    done = asyncio.run(call("POST", "/api/transformation-rules/suggestion", data=data))
    assert done.status_code == 303 and "notice=" in done.headers["location"]
    row = sqlite3.connect(db).execute("SELECT status, params_json, description FROM transformation_rules").fetchone()
    assert row[0] == "DRAFT" and '"SCH": "SCHWAB"' in row[1] and "ZZZ" not in row[1] and "Suggested from the data" in row[2]
    empty = asyncio.run(call("POST", "/api/transformation-rules/suggestion", data={**data, "rule_id": "SUG_CUSTODIAN_CD34", "target::1": ""}))
    assert "error=" in empty.headers["location"]


def test_compliance_reviewer_cannot_use_the_suggestion_endpoints(db, tmp_path, monkeypatch):
    _, call = _client(monkeypatch, tmp_path, role="Compliance Reviewer")
    for path, data in (("/api/transformation-rules/suggestion", {"rule_type": "DATE_FORMAT", "rule_id": "SUG_X_AAAAA"}),
                       ("/api/transformation-rules/infer-preview", {"rule_text": "Map A to B"})):
        denied = asyncio.run(call("POST", path, data=data))
        assert denied.status_code == 303 and "may+not+perform" in denied.headers["location"]


def test_actions_update_in_place_and_tables_freeze_header_and_first_column():
    base = (ROOT / "ui" / "templates" / "base.html").read_text()
    assert 'fetch(url.href, {method: "POST"' in base and "data-full-reload" in base and "replaceWith" in base
    css = (ROOT / "ui" / "static" / "xodus.css").read_text()
    assert ".tw thead th{position:sticky;top:0" in css and "position:sticky;left:0" in css

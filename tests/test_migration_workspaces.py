import asyncio
from urllib.parse import urlencode

import httpx

from tests.test_firm_config import _post_setup, _setup_client


def _call(app_module, session_id, method, path, data=None):
    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": session_id}) as client:
            return await client.request(method, path, content=urlencode(data) if data else None,
                                        headers={"Content-Type": "application/x-www-form-urlencoded"} if data else None, follow_redirects=False)
    return asyncio.run(run())


def _second_setup(app_module, session_id):
    return _call(app_module, session_id, "POST", "/api/migration/setup", [
        ("migration_name", "Harbor RIA to Simplicity"), ("source_firm", "Harbor RIA"), ("target_system", "Simplicity"),
        ("canonical_version", "Simplicity-MCL-v1"), ("source_acquisition", "SAMPLE_FILES"), ("entities", "Household")])


def test_finished_migration_seeds_the_next_one_as_a_template(tmp_path, monkeypatch):
    app_module, sid, state = _setup_client(tmp_path, monkeypatch, "tpl")
    assert _post_setup(app_module, sid).status_code == 303
    first_id = state.questionnaire_data["migration_id"]

    page = _call(app_module, sid, "GET", "/migrations").text
    assert "CoreCap to Simplicity" in page and "Active" in page and "Start a new migration from this" in page

    response = _call(app_module, sid, "POST", "/api/migration/new", [("template_id", first_id)])
    assert response.status_code == 303 and "from_template=1" in response.headers["location"]
    second_id = state.questionnaire_data["migration_id"]
    assert second_id != first_id
    # Nothing is in scope or approved until the new setup is saved; only configuration is offered as pre-fill.
    assert state.questionnaire_data["migration_setup"] == {} and set(state.entity_scopes.values()) == {"OUT_OF_SCOPE"}
    prefill = state.questionnaire_data["migration_template"]
    assert prefill["target_system"] == "Simplicity" and prefill["template_migration_id"] == first_id
    assert "migration_name" not in prefill and "source_firm" not in prefill

    form = _call(app_module, sid, "GET", "/migration-setup?new_migration=1&from_template=1").text
    assert "Pre-filled from an earlier migration" in form and first_id in form

    assert _second_setup(app_module, sid).status_code == 303
    assert state.questionnaire_data["migration_setup"]["template_migration_id"] == first_id
    listing = _call(app_module, sid, "GET", "/migrations").text
    assert "Harbor RIA" in listing and "CoreCap to Simplicity" in listing and "Earlier" in listing
    assert "started from template" in listing


def test_unknown_or_unconfigured_template_is_rejected(tmp_path, monkeypatch):
    app_module, sid, state = _setup_client(tmp_path, monkeypatch, "tpl404")
    assert _post_setup(app_module, sid).status_code == 303
    current = state.questionnaire_data["migration_id"]
    assert _call(app_module, sid, "POST", "/api/migration/new", [("template_id", "MIG-DOESNOTEXIST")]).status_code == 404
    assert state.questionnaire_data["migration_id"] == current  # a rejected request changes nothing


def test_analyst_may_start_from_template_but_read_only_roles_may_not(tmp_path, monkeypatch):
    app_module, sid, state = _setup_client(tmp_path, monkeypatch, "tplrole")
    assert _post_setup(app_module, sid).status_code == 303
    monkeypatch.setitem(app_module.session_mgr.active_sessions, sid, {"email": "t@example.invalid", "name": "Reader", "role": "Validation Specialist"})
    page = _call(app_module, sid, "GET", "/migrations").text
    assert "CoreCap to Simplicity" in page and "Start a new migration from this" not in page
    denied = _call(app_module, sid, "POST", "/api/migration/new", [("template_id", state.questionnaire_data["migration_id"])])
    assert denied.status_code in (303, 403)
    assert "may+not+perform" in denied.headers.get("location", "") or denied.status_code == 403


def test_template_migration_seeds_proposals_but_never_approvals(tmp_path, monkeypatch):
    import sqlite3
    app_module, sid, state = _setup_client(tmp_path, monkeypatch, "tplseed")
    assert _post_setup(app_module, sid).status_code == 303
    first_id = state.questionnaire_data["migration_id"]
    conn = sqlite3.connect(tmp_path / "tplseed.db")
    conn.execute("INSERT INTO canonical_mappings (mapping_id, migration_id, entity_name, source_field, canonical_field, mapping_type, transformation_expression, confidence_score, is_confirmed, created_by, updated_utc) "
                 "VALUES ('M1', ?, 'Account', 'WEIRD_ACCT_REF', 'ACCT_NUM', 'DIRECT', 'TRIM', 0.9, 1, 'tester', '2026-11-03')", (first_id,))
    conn.commit(); conn.close()

    assert _call(app_module, sid, "POST", "/api/migration/new", [("template_id", first_id)]).status_code == 303
    assert _second_setup(app_module, sid).status_code == 303
    second_id = state.questionnaire_data["migration_id"]
    seed = app_module._template_mapping_seeds(second_id, "Account")
    assert seed["seeds"] == {"WEIRD_ACCT_REF": "ACCT_NUM"} and seed["template_id"] == first_id
    assert app_module._template_mapping_seeds(first_id, "Account")["seeds"] == {}  # the template itself is not seeded from itself
    assert app_module._get_approved_mapping_rules(second_id, "Account") == {}  # nothing approved in the new migration


def test_generated_proposal_from_template_is_pending_and_says_where_it_came_from(tmp_path, monkeypatch):
    import sqlite3
    app_module, sid, state = _setup_client(tmp_path, monkeypatch, "tplgen")
    assert _post_setup(app_module, sid).status_code == 303
    first_id = state.questionnaire_data["migration_id"]
    db = tmp_path / "tplgen.db"
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO canonical_mappings (mapping_id, migration_id, entity_name, source_field, canonical_field, mapping_type, transformation_expression, confidence_score, is_confirmed, created_by, updated_utc) "
                 "VALUES ('M1', ?, 'Account', 'WEIRD_ACCT_REF', 'ACCT_NUM', 'DIRECT', 'DIRECT_COPY', 0.9, 1, 'tester', '2026-11-03')", (first_id,))
    conn.commit(); conn.close()
    assert _call(app_module, sid, "POST", "/api/migration/new", [("template_id", first_id)]).status_code == 303
    assert _second_setup(app_module, sid).status_code == 303
    second_id = state.questionnaire_data["migration_id"]
    csv_path = tmp_path / "harbor_accounts.csv"
    csv_path.write_text("WEIRD_ACCT_REF,NOTES\nA-1,x\nA-2,y\n")
    state.entity_scopes["Account"] = "IN_SCOPE"
    state.entity_sample_files["Account"] = {"filename": "harbor_accounts.csv", "stored_path": str(csv_path), "status": "UPLOADED", "file_id": "F1"}
    app_module._map_uploaded_source_entity("Account")
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT canonical_field, status, inference_basis FROM mapping_suggestions WHERE migration_id = ? AND source_field = 'WEIRD_ACCT_REF'", (second_id,)).fetchall()
    approved = conn.execute("SELECT COUNT(*) FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (second_id,)).fetchone()[0]
    conn.close()
    assert rows and rows[0][0] == "ACCT_NUM" and rows[0][1] == "PENDING"
    assert "CoreCap to Simplicity" in (rows[0][2] or "") and approved == 0


def test_new_migration_does_not_inherit_the_previous_name_or_source(tmp_path, monkeypatch):
    app_module, sid, state = _setup_client(tmp_path, monkeypatch, "tplname")
    assert _post_setup(app_module, sid).status_code == 303
    first_id = state.questionnaire_data["migration_id"]
    assert _call(app_module, sid, "POST", "/api/migration/new", [("template_id", first_id)]).status_code == 303
    assert state.questionnaire_data["firm_name"] == "" and state.questionnaire_data["migration_name"] == ""
    form = _call(app_module, sid, "GET", "/migration-setup?new_migration=1&from_template=1").text
    assert 'name="migration_name" value=""' in form and 'name="source_firm" value=""' in form

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

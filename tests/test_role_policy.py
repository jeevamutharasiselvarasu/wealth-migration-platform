import asyncio
import re
from urllib.parse import urlencode

import httpx
import pytest

from ui import role_policy as policy

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _post_routes(app_module):
    return sorted({route.path for route in app_module.app.routes
                   if "POST" in getattr(route, "methods", set())})


def test_every_post_route_is_classified_by_the_role_policy():
    import ui.app as app_module

    unclassified = [path for path in _post_routes(app_module)
                    if not policy.is_classified(re.sub(r"\{[^}]+\}", "x", path))]
    assert unclassified == [], f"Classify these routes in ui/role_policy.py: {unclassified}"


def test_policy_matrix_matches_the_documented_responsibilities():
    a = policy.allowed_roles_for
    assert a("POST", "/api/migration/setup") == {"Platform Admin", "Migration Analyst"}
    assert a("POST", "/api/demo/load") == {"Platform Admin"}
    assert a("POST", "/api/kernel/update") == {"Platform Admin", "Domain SME"}
    assert a("POST", "/api/entities/upload-sample-file") == {"Platform Admin", "Migration Analyst"}
    assert a("POST", "/api/entities/confirm-inference") == {"Platform Admin", "Migration Analyst"}
    assert a("POST", "/api/mapping-suggestions/S-1/review") == {"Platform Admin", "Migration Analyst"}
    assert a("POST", "/api/mapping-suggestions/approve-entity") == {"Platform Admin", "Migration Analyst"}
    assert a("POST", "/api/mapping-gaps/RFI-1/response") == {"Platform Admin", "Migration Analyst", "Domain SME"}
    assert a("GET", "/api/migration/setup") is None and a("POST", "/login") is None


@pytest.mark.parametrize("role,setup,upload,confirm,review,answer", [
    ("Platform Admin", True, True, True, True, True),
    ("Migration Analyst", True, True, True, True, True),
    ("Domain SME", False, False, False, False, True),
    ("Validation Specialist", False, False, False, False, False),
])
def test_roles_are_enforced_server_side(tmp_path, monkeypatch, role, setup, upload, confirm, review, answer):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "roles.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Role Tester", "role": role})
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    setup_body = urlencode([("migration_name", "M"), ("source_firm", "CoreCap"), ("target_system", "Simplicity"),
                            ("canonical_version", "Simplicity-MCL-v1"), ("source_acquisition", "SAMPLE_FILES"),
                            ("entities", "Account")])

    async def call(path, **kwargs):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"sid": "x", "session_id": "sid"}) as client:
            return await client.post(path, follow_redirects=False, **kwargs)

    def denied(response):
        return response.status_code == 403 and "may not perform" in response.text

    r = asyncio.run(call("/api/migration/setup", content=setup_body, headers=FORM))
    assert (not denied(r)) is setup
    r = asyncio.run(call("/api/entities/upload-sample-file", files=[("files", ("a.csv", b"ACCT_NUM\nA1\n", "text/csv"))]))
    assert (not denied(r)) is upload
    r = asyncio.run(call("/api/entities/confirm-entity", content="entity_name=Account&comments=x", headers=FORM))
    assert (not denied(r)) is confirm
    r = asyncio.run(call("/api/mapping-suggestions/NOPE/review",
                         content="decision=APPROVED&reviewer=x&comments=x", headers=FORM))
    assert (not denied(r)) is review
    r = asyncio.run(call("/api/mapping-gaps/RFI-NOPE/response", content="response_text=x", headers=FORM))
    assert (not denied(r)) is answer


def test_denied_html_form_post_redirects_back_with_a_readable_error(tmp_path, monkeypatch):
    import ui.app as app_module

    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid2",
                        {"email": "t@example.invalid", "name": "Role Tester", "role": "Validation Specialist"})

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": "sid2"}) as client:
            return await client.post("/api/entities/confirm-entity", content="entity_name=Account", follow_redirects=False,
                                     headers={**FORM, "accept": "text/html", "referer": "http://testserver/entity-sample-files"})
    response = asyncio.run(run())
    assert response.status_code == 303
    assert response.headers["location"].startswith("/entity-sample-files?error=")
    assert "may+not+perform" in response.headers["location"]


def test_unauthenticated_requests_still_reach_the_login_redirect():
    import ui.app as app_module

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/api/entities/confirm-entity", content="entity_name=Account",
                                     headers=FORM, follow_redirects=False)
    assert asyncio.run(run()).status_code in {303, 307}


def test_personas_are_assigned_per_user_and_a_normal_user_holds_analyst_and_validator():
    from ui.auth import session_mgr

    assigned = session_mgr.assigned_roles
    assert assigned("user.demo@simplicitywealth.com") == {"Migration Analyst", "Validation Specialist"}
    assert assigned("analyst.demo@simplicitywealth.com") == {"Migration Analyst"}
    assert assigned("validator.demo@simplicitywealth.com") == {"Validation Specialist"}
    assert assigned("sme.demo@simplicitywealth.com") == {"Domain SME", "Migration Analyst", "Validation Specialist"}
    assert assigned("jeeva@simplicitywealth.com") == set(session_mgr.ALL_ROLES)
    assert assigned("nobody@example.invalid") == set()


def test_domain_sme_can_update_kernel_specs_and_the_change_is_attributed(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "kernel.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Canonical Owner", "role": "Domain SME"})
    state = WorkspaceStateStore()
    monkeypatch.setattr(app_module, "state_store", state)
    body = urlencode([("sleeve_invariant", "1.0"), ("sleeve_tolerance", "0.5"), ("aum_drift_gate", "1.0")])

    async def post(role):
        app_module.session_mgr.active_sessions["sid"]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": "sid"}) as client:
            return await client.post("/api/kernel/update", content=body, headers=FORM, follow_redirects=False)

    assert asyncio.run(post("Migration Analyst")).status_code == 403
    assert asyncio.run(post("Domain SME")).status_code == 303
    assert state.kernel_specs["last_modified_by"] == "Canonical Owner"

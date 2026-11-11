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
    assert a("POST", "/api/migration/setup") == {"IMO Lead", "Migration Analyst"}
    assert a("POST", "/api/demo/load") == {"Migration Analyst"}  # fills a migration workspace, so not an admin action
    assert a("POST", "/api/kernel/update") == {"Platform Admin", "Domain SME"}
    assert a("POST", "/api/entities/upload-sample-file") == {"Migration Analyst"}
    assert a("POST", "/api/entities/confirm-inference") == {"Migration Analyst"}
    assert a("POST", "/api/mapping-suggestions/S-1/review") == {"Migration Analyst"}
    assert a("POST", "/api/mapping-suggestions/approve-entity") == {"Migration Analyst"}
    assert a("POST", "/api/mapping-gaps/RFI-1/response") == {"Migration Analyst", "Domain SME"}
    assert a("GET", "/api/migration/setup") is None and a("POST", "/login") is None


def test_template_roles_imo_lead_configures_and_compliance_reviewer_is_read_only():
    """Suite template roles: the IMO Lead creates and configures workspaces but approves no mapping; the Compliance Reviewer holds no write capability."""
    from ui.auth import SessionManager
    from control_plane.services import user_registry

    assert {"IMO Lead", "Compliance Reviewer"} <= set(SessionManager.ALL_ROLES) and set(SessionManager.ALL_ROLES) == set(user_registry.ALL_ROLES)
    write_roles = set().union(*(roles for _, roles in policy._COMPILED))
    assert "Compliance Reviewer" not in write_roles
    for path in ("/api/migration/setup", "/api/migration/new", "/api/entities/save-scopes"):
        assert "IMO Lead" in policy.allowed_roles_for("POST", path)
    for path in ("/api/mapping-suggestions/approve-entity", "/api/entities/confirm-inference", "/api/validation/run", "/api/kernel/update", "/api/demo/load"):
        assert "IMO Lead" not in policy.allowed_roles_for("POST", path)


@pytest.mark.parametrize("role,setup,upload,confirm,review,answer", [
    ("Platform Admin", False, False, False, False, False),
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
    assert assigned("jeeva@simplicitywealth.com") == {"Platform Admin"}  # infrastructure-only: no migration personas
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


def test_setup_and_discovery_hide_forms_the_active_persona_cannot_use(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "forms.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Tester", "role": "Platform Admin"})
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())

    async def page(role, path):
        app_module.session_mgr.active_sessions["sid"]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": "sid"}) as client:
            return (await client.get(path)).text

    for role, can_setup, can_discover in (("Migration Analyst", True, True),
                                          ("Domain SME", False, False), ("Validation Specialist", False, False)):
        setup = asyncio.run(page(role, "/migration-setup"))
        assert ("Save setup" in setup) is can_setup, role
        assert ("<fieldset disabled" in setup) is (not can_setup), role
        discovery = asyncio.run(page(role, "/entity-sample-files"))
        assert ('action="/api/entities/upload-sample-file"' in discovery) is can_discover, role


def test_canonical_change_request_lifecycle_and_roles(tmp_path, monkeypatch):
    import sqlite3

    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ccr.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Alex Analyst", "role": "Migration Analyst"})
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    new = urlencode([("entity_name", "Account"), ("canonical_field", "ACCOUNT_TITLE"), ("change_type", "ADD_FIELD"),
                     ("proposed_change", "Add free-text account title"), ("reason", "CoreCap carries titles")])

    async def call(role, name, method, path, body=None):
        app_module.session_mgr.active_sessions["sid"].update(role=role, name=name)
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": "sid"}) as client:
            return await client.request(method, path, content=body, headers=FORM if body else None, follow_redirects=False)

    # Only Analyst/Admin raise; SME and Validator may not.
    assert asyncio.run(call("Domain SME", "Sam SME", "POST", "/api/canonical-changes", new)).status_code == 403
    assert asyncio.run(call("Validation Specialist", "Val", "POST", "/api/canonical-changes", new)).status_code == 403
    assert asyncio.run(call("Migration Analyst", "Alex Analyst", "POST", "/api/canonical-changes", new)).status_code == 303
    bad = asyncio.run(call("Migration Analyst", "Alex Analyst", "POST", "/api/canonical-changes",
                           urlencode([("entity_name", "Party"), ("canonical_field", "X"), ("change_type", "ADD_FIELD"),
                                      ("proposed_change", "p"), ("reason", "r")])))
    assert "error=" in bad.headers["location"]

    conn = sqlite3.connect(tmp_path / "ccr.db")
    rid, status_, by = conn.execute("SELECT request_id, status, requested_by FROM canonical_change_requests").fetchone()
    assert (status_, by) == ("OPEN", "Alex Analyst")

    path = f"/api/canonical-changes/{rid}/decision"
    # The raising analyst cannot decide; the SME can, and reject/defer need a note.
    assert asyncio.run(call("Migration Analyst", "Alex Analyst", "POST", path, "decision=APPROVED&note=x")).status_code == 403
    no_note = asyncio.run(call("Domain SME", "Sam SME", "POST", path, "decision=REJECTED&note="))
    assert "error=" in no_note.headers["location"]
    assert conn.execute("SELECT status FROM canonical_change_requests").fetchone()[0] == "OPEN"
    done = asyncio.run(call("Domain SME", "Sam SME", "POST", path, "decision=APPROVED&note=Agreed"))
    assert done.status_code == 303 and "notice=" in done.headers["location"]
    row = conn.execute("SELECT status, decided_by, decision_note FROM canonical_change_requests").fetchone()
    assert row == ("APPROVED", "Sam SME", "Agreed")
    again = asyncio.run(call("Domain SME", "Sam SME", "POST", path, "decision=REJECTED&note=late"))
    assert "already" in again.headers["location"]

    for role, raise_form, decide_ok in (("Migration Analyst", True, False), ("Domain SME", False, True), ("Validation Specialist", False, False)):
        html = asyncio.run(call(role, "Viewer", "GET", "/canonical-changes")).text
        assert ('action="/api/canonical-changes"' in html) is raise_form, role
        assert ("Read-only for" in html) is (role == "Validation Specialist"), role


def test_approved_add_field_is_applied_as_overlay_and_becomes_a_mapping_target(tmp_path, monkeypatch):
    import sqlite3

    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ovl.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Alex Analyst", "role": "Migration Analyst"})
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())

    async def call(role, name, method, path, body=None):
        app_module.session_mgr.active_sessions["sid"].update(role=role, name=name)
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": "sid"}) as client:
            return await client.request(method, path, content=body, headers=FORM if body else None, follow_redirects=False)

    new = urlencode([("entity_name", "Account"), ("canonical_field", "account_title"), ("change_type", "ADD_FIELD"),
                     ("proposed_change", "Free-text account title"), ("reason", "CoreCap carries titles")])
    assert asyncio.run(call("Migration Analyst", "Alex Analyst", "POST", "/api/canonical-changes", new)).status_code == 303
    conn = sqlite3.connect(tmp_path / "ovl.db")
    rid = conn.execute("SELECT request_id FROM canonical_change_requests").fetchone()[0]
    apply_path = f"/api/canonical-changes/{rid}/apply"

    # Not applicable until approved; only the SME/Admin may apply.
    assert asyncio.run(call("Migration Analyst", "Alex Analyst", "POST", apply_path)).status_code == 403
    early = asyncio.run(call("Domain SME", "Sam SME", "POST", apply_path))
    assert "error=" in early.headers["location"]
    assert SchemaMapperAgent._identity_target("Account", "ACCOUNT_TITLE") is None

    asyncio.run(call("Domain SME", "Sam SME", "POST", f"/api/canonical-changes/{rid}/decision", "decision=APPROVED&note=ok"))
    done = asyncio.run(call("Domain SME", "Sam SME", "POST", apply_path))
    assert done.status_code == 303 and "notice=" in done.headers["location"]
    assert SchemaMapperAgent._identity_target("Account", "ACCOUNT_TITLE") == "ACCOUNT_TITLE"
    assert SchemaMapperAgent._identity_target("Client", "ACCOUNT_TITLE") is None
    assert "already" in asyncio.run(call("Domain SME", "Sam SME", "POST", apply_path)).headers["location"]
    assert "ACCT_NUM" in SchemaMapperAgent.required_fields("Account")
    assert "ACCOUNT_TITLE" not in SchemaMapperAgent.required_fields("Account")
    page = asyncio.run(call("Domain SME", "Sam SME", "GET", "/canonical-changes")).text
    assert "Applied to the canonical model" in page and "nullable: true" in page
    assert conn.execute("SELECT applied_by FROM canonical_model_overlay").fetchone()[0] == "Sam SME"


def test_platform_admin_sees_platform_setup_only_not_migration_data(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui import role_policy as policy
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "admin_view.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Admin", "role": "Platform Admin"})
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())

    async def get(path):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": "sid"}) as client:
            return await client.get(path, follow_redirects=False, headers={"accept": "text/html"})

    for blocked in ("/", "/discovery", "/execution", "/entity-sample-files", "/hitl", "/validation-rules", "/agent-pipeline", "/firm-knowledge"):
        r = asyncio.run(get(blocked))
        assert r.status_code == 303 and r.headers["location"].startswith("/setup?error="), blocked
    for allowed in ("/setup", "/azure-setup", "/governance"):
        assert asyncio.run(get(allowed)).status_code != 303 or "/setup?error=" not in asyncio.run(get(allowed)).headers.get("location", ""), allowed
    assert policy.admin_may_view("/azure-setup") and not policy.admin_may_view("/execution") and not policy.admin_may_view("/")
    # Platform pages carry no migration frame: no workspace name or id, no pipeline stages, no workspace configuration.
    app_module.state_store.questionnaire_data.update({"migration_id": "MIG-SECRET01", "migration_setup": {"migration_name": "Secret Deal", "entities": ["Account"]}})
    for page in ("/suite", "/setup", "/target-setup", "/azure-setup"):
        r = asyncio.run(get(page))
        assert r.status_code == 200, page
        html = r.text
        assert "Platform setup" in html and "MIG-SECRET01" not in html and "Secret Deal" not in html, page
        assert "Pipeline run" not in html and 'href="/migration-setup"' not in html, page
    # Migration-side POSTs are closed to the admin; platform-side POSTs stay open.
    assert "Platform Admin" not in policy.allowed_roles_for("POST", "/api/validation/run")
    assert "Platform Admin" in policy.allowed_roles_for("POST", "/api/azure/connection")


def test_migration_users_get_platform_setup_through_the_migration_setup_not_the_admin_pages(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from control_plane.services import target_connector
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a3.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid-a3", {"email": "a3@example.invalid", "name": "Ana", "role": "Migration Analyst"})
    monkeypatch.setattr(app_module, "publish_event", lambda *a, **k: None)
    conn = db.get_db_connection()
    target_connector.ensure_table(conn)
    conn.execute("INSERT INTO target_system_config (config_id, target_name, connector_type, endpoint, owner, status, configured_by, configured_utc, confirmed_by, confirmed_utc) "
                 "VALUES (1, 'Simplicity SQL', 'azure-sql', 'srv/db', 'Target Team', 'CONFIRMED', 'Admin', '2026-11-06', 'Admin', '2026-11-06')")
    conn.commit()
    conn.close()

    async def call(method, path, **kw):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid-a3"}) as client:
            return await client.request(method, path, follow_redirects=False, headers={"accept": "text/html"}, **kw)

    for page in ("/azure-setup", "/github-setup", "/target-setup"):
        assert asyncio.run(call("GET", page)).headers["location"].startswith("/setup?error="), page
    hub = asyncio.run(call("GET", "/setup")).text
    assert 'href="/azure-setup' not in hub and 'href="/github-setup"' not in hub and 'href="/target-setup"' not in hub
    form = asyncio.run(call("GET", "/migration-setup")).text
    assert 'data-testid="from-platform-target"' in form and 'value="Simplicity SQL"' in form
    r = asyncio.run(call("POST", "/api/migration/setup", data={"migration_name": "M", "target_system": "Somewhere else", "canonical_version": "wrong",
                                                              "entities": ["Account"], "source_acquisition": "SAMPLE_FILES"}))
    assert r.status_code == 303
    setup = app_module.state_store.questionnaire_data["migration_setup"]
    assert setup["target_system"] == "Simplicity SQL" and setup["target_owner"] == "Target Team"


def test_platform_admin_reads_migration_setup_but_not_its_data(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "admin_setup.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data.update({"migration_id": "MIG-SETUP1", "migration_setup": {"migration_name": "Setup Only", "entities": ["Account"], "source_firm": "CoreCap"}})
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid-adm", {"email": "a@example.invalid", "name": "Admin", "role": "Platform Admin"})

    async def get(path):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid-adm"}) as client:
            return await client.get(path, follow_redirects=False, headers={"accept": "text/html"})

    setup = asyncio.run(get("/migration-setup"))
    assert setup.status_code == 200 and "Read-only for Platform Admin" in setup.text and "<fieldset disabled" in setup.text
    listing = asyncio.run(get("/migrations"))
    assert listing.status_code == 200 and "/api/migration/open" not in listing.text and "/api/migration/new" not in listing.text
    for data_page in ("/", "/discovery", "/entity-sample-files", "/agent-pipeline", "/target-mapping", "/execution", "/validation-rules"):
        assert asyncio.run(get(data_page)).headers.get("location", "").startswith("/setup?error="), data_page

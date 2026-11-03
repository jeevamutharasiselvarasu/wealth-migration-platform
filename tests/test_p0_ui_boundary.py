from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_p0_ui_does_not_render_deferred_wealth_modules_or_assign_waves():
    pipeline = (ROOT / "ui" / "templates" / "agent_pipeline.html").read_text()
    dashboard = (ROOT / "ui" / "templates" / "dashboard.html").read_text()
    knowledge = (ROOT / "ui" / "templates" / "firm_knowledge.html").read_text()
    for deferred in ("Sleeve Math Agent", "Sleeve / Allocation", "Position (Snapshot)", "Execute 9-Agent Pipeline", "Restricted Models"):
        assert deferred not in pipeline
    assert "Primary Custodian Rails" not in dashboard
    assert "Condition-based wave segregation" in dashboard
    assert "CONFIGURED · NOT EVALUATED" in dashboard
    assert "No records or entities are assigned to a wave by this dashboard." in dashboard
    assert "name=\"sleeve_invariant\"" not in knowledge
    assert "name=\"aum_drift_gate\"" not in knowledge



def _shell_source():
    """base.html plus the shared suite top bar and persona selector it includes."""
    t = ROOT / "ui" / "templates"
    return "".join((t / n).read_text(encoding="utf-8") for n in ("base.html", "_suite_top.html", "_persona_select.html"))

def test_dashboard_knowledge_assets_are_not_fabricated_by_default_state():
    state = (ROOT / "ui" / "state.py").read_text()
    assert "Amplify UMA Sleeve and Holding Schema Contract" not in state
    assert 'self.firm_knowledge_docs: List[Dict[str, Any]] = []' in state
    assert '"program_status": "P0_SOURCE_TO_CANONICAL"' in state

def test_p0_navigation_exposes_persona_first_integrated_control_plane_workflow():
    navigation = _shell_source()
    app_source = (ROOT / "ui" / "app.py").read_text()
    persona = (ROOT / "ui" / "templates" / "_persona_select.html").read_text()
    assert 'action="/persona-selection"' in persona and "persona_options" in persona  # persona is a top-bar selector, as in the template
    assert '_suite_top.html' in navigation
    assert "pg.steps" in navigation and 'class="runh"' in navigation and "Next: {{ pg.next.label }}" in navigation  # stage rail with run progress
    for route in ('/migration-setup', '/entity-sample-files', '/agent-pipeline', '/reconciliation'):
        assert f'"{route}"' in app_source  # stepper destinations
    assert 'href="/firm-knowledge"' in navigation  # Governance reference, not a workflow step
    assert 'href="/hitl"' not in navigation
    assert "Persona" in navigation and "persona_options" in navigation
    assert '"discover": "Discover"' in app_source
    assert "DATA PLANE (SPECIALIST)" not in navigation


def test_dashboard_exposes_only_the_three_entity_p0_flow():
    dashboard = (ROOT / "ui" / "templates" / "dashboard.html").read_text()
    for entity in ("Account", "Household", "Client"):
        assert entity in dashboard
    for stage in (
        "Discover & profile",
        "Map & capture lineage",
        "Review & approve",
        "Validate & record",
    ):
        assert stage in dashboard
    assert "Target loading and wave assignment are not executed in this phase." in dashboard


def test_global_ui_shell_matches_agentic_demo_visual_language():
    shell = _shell_source()
    for token in ("suite.css", "dm.css", "Inter", 'class="xtop', 'class="pbar"', 'class="side"', 'class="main'):
        assert token in shell
    for entity in ("Account", "Household", "Client"):
        assert entity in shell


def test_mapping_studio_does_not_claim_unverified_readiness_or_target_loading():
    pipeline = (ROOT / "ui" / "templates" / "agent_pipeline.html").read_text()
    assert ">98%</div>" not in pipeline
    assert "HIGH QUALITY · READY" not in pipeline
    assert "P0 · APPROVAL REQUIRED" in pipeline
    assert "target loading" in pipeline.lower()
    assert "wave assignment" in pipeline.lower()



def test_login_has_no_role_field_and_persona_is_chosen_from_the_top_bar_selector():
    login = (ROOT / "ui" / "templates" / "login.html").read_text()
    persona = (ROOT / "ui" / "templates" / "_persona_select.html").read_text()
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'name="role"' not in login
    assert 'name="role"' in persona
    assert '@app.get("/persona-selection"' not in app  # no separate persona page; the top-bar selector is the only control
    assert not (ROOT / "ui" / "templates" / "persona_selection.html").exists()
    assert '@app.post("/persona-selection"' in app
    assert 'RedirectResponse(url="/suite"' in app  # login goes straight to the suite home; persona switches from the top bar
    assert 'next.startswith("/") and not next.startswith("//")' in app  # persona switch only returns to same-site paths


def test_migration_setup_collects_target_azure_knowledge_and_source_before_discovery():
    setup = (ROOT / "ui" / "templates" / "migration_setup.html").read_text()
    for token in ("Target system and canonical contract", "Azure environment and access",
                  "Knowledge and mapping evidence", "Source acquisition method",
                  "SAMPLE_FILES", "AZURE_CONNECTION", "Account", "Household", "Client"):
        assert token in setup
    assert "Do not paste secrets" in setup
    assert "target loading" in setup.lower()


def test_source_discovery_does_not_ask_user_to_assign_entity():
    source = (ROOT / "ui" / "templates" / "entity_sample_files.html").read_text()
    inference = (ROOT / "control_plane" / "services" / "entity_inference.py").read_text()
    assert '<select name="entity_name"' not in source
    assert "Analyse files & infer entities" in source
    assert "classify_source_file" in (ROOT / "control_plane" / "services" / "entity_classification_agent.py").read_text()
    assert "NEEDS_CLARIFICATION" in inference
    assert "data_dictionary_notes" in source


def test_mapping_review_supports_approval_rejection_and_information_requests():
    app = (ROOT / "ui" / "app.py").read_text()
    studio = (ROOT / "ui" / "templates" / "agent_pipeline.html").read_text()
    assert 'value="APPROVED"' in studio
    assert 'value="REJECTED"' in studio
    assert "rejection reason" in studio.lower()
    assert 'mapping_information_requests' in app
    assert '/api/mapping-gaps/{request_id}/response' in app


def test_target_contract_is_a_required_prerequisite_for_migration_setup():
    setup = (ROOT / "ui" / "templates" / "migration_setup.html").read_text()
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'name="canonical_version"' in setup
    assert 'required placeholder="Version or target schema document reference"' in setup
    assert "Target system and canonical schema / contract reference are required" in app


def test_azure_connection_requires_environment_identifiers_without_collecting_secrets():
    app = (ROOT / "ui" / "app.py").read_text()
    setup = (ROOT / "ui" / "templates" / "migration_setup.html").read_text()
    assert 'setup["source_acquisition"] == "AZURE_CONNECTION"' in app
    for field in ("azure_tenant_id", "azure_subscription_id", "azure_resource_group",
                  "azure_storage_account", "azure_container"):
        assert field in app
        assert field in setup
    assert "Do not paste secrets" in setup


def test_top_bar_selector_shows_the_active_role():
    persona = (ROOT / "ui" / "templates" / "_persona_select.html").read_text()
    assert "active_persona" in persona and "selected" in persona


def test_entity_level_approval_ui_and_backend_are_evidence_gated():
    app = (ROOT / "ui" / "app.py").read_text()
    source = (ROOT / "ui" / "templates" / "entity_sample_files.html").read_text()
    studio = (ROOT / "ui" / "templates" / "agent_pipeline.html").read_text()
    assert '@app.post("/api/entities/confirm-entity")' in app
    assert 'inference.get("status") != "INFERRED"' in app
    assert 'record.get("inference_approval_status") == "REJECTED"' in app
    assert 'Approve all {{ entity }} files' in source
    assert '@app.post("/api/mapping-suggestions/approve-entity")' in app
    assert "BEGIN IMMEDIATE" in app
    assert "supports_transformation(rule)" in app
    assert 'Approve all {{ suggestion.entity_name }} mappings' in studio
    assert "ENTITY_LEVEL_MAPPING_APPROVED" in app


def test_persona_switch_is_limited_to_roles_assigned_to_authenticated_user():
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'session_mgr.assigned_roles(user["email"])' in app
    assert 'if role not in (session_mgr.assigned_roles(user["email"]) or {user["role"]}):' in app
    assert 'raise HTTPException(status_code=403, detail="The selected persona is not assigned to this user.")' in app


def test_mapping_browser_forms_redirect_but_api_callers_keep_json_responses():
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'async def map_uploaded_entity_file(entity_name: str = Form(...), request: Request = None):' in app
    assert 'async def review_source_mapping_suggestion(' in app
    assert 'if request is not None and "text/html" in request.headers.get("accept", ""):' in app
    assert 'RedirectResponse(url="/agent-pipeline"' in app


def test_migration_setup_persists_explicit_entity_scope_selection():
    app = (ROOT / "ui" / "app.py").read_text()
    setup = (ROOT / "ui" / "templates" / "migration_setup.html").read_text()
    assert 'form.getlist("entities")' in app
    assert "Select at least one in-scope entity" in app
    assert 'state_store.entity_scopes[entity] = "PENDING_DISCOVERY" if entity in selected_entities else "OUT_OF_SCOPE"' in app
    assert 'name="entities"' in setup
    assert "Unselected entities are excluded" in setup
    assert 'href="/firm-knowledge"' not in setup


def test_inferred_entity_requires_human_approval_before_mapping():
    app = (ROOT / "ui" / "app.py").read_text()
    inventory = (ROOT / "ui" / "templates" / "entity_sample_files.html").read_text()
    assert '@app.post("/api/entities/confirm-inference")' in app
    assert 'record["inference_approval_status"] = "APPROVED"' in app
    assert 'record["inference_approval_status"] = "REJECTED"' in app
    assert 'target_entity: str = Form("")' in app
    assert 'if selected_entity == "OUT_OF_SCOPE":' in app
    assert 'record["discovery_status"] = "OUT_OF_SCOPE"' in app
    assert 'for bucket_entity, files in state_store.entity_source_files.items()' in app
    assert 'uploaded.get("inference_approval_status") != "APPROVED"' in app
    assert 'name="decision" value="APPROVE"' in inventory
    assert 'name="decision" value="REJECT"' in inventory
    assert "Enter a rejection reason before rejecting this file." in app


def test_entity_scope_screen_redirects_into_source_discovery():
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'async def entity_selection_view(request: Request):' in app
    assert 'return RedirectResponse(url="/entity-sample-files"' in app


def test_demo_login_is_loaded_from_configuration_and_password_is_not_plaintext():
    auth = (ROOT / "ui" / "auth.py").read_text()
    users = __import__("json").loads((ROOT / "demo_data" / "demo_users.json").read_text())
    assert "XODUS_DEMO_MODE" in auth
    assert "hashlib.sha256" in auth
    assert "password123" not in auth
    assert users["users"][0]["email"] == "jeeva@simplicitywealth.com"
    assert all("password" not in key for user in users["users"] for key in user), "no credential material in tracked fixtures"
    import fnmatch
    patterns = [l.strip() for l in (ROOT / ".gitignore").read_text().splitlines() if l.strip() and not l.startswith(("#", "!"))]
    assert any(fnmatch.fnmatch("demo_data/demo_passwords.local.json", p) for p in patterns), "local password file must stay git-ignored"


def test_demo_migration_is_input_data_not_values_embedded_in_routes():
    app = (ROOT / "ui" / "app.py").read_text()
    manifest = __import__("json").loads((ROOT / "demo_data" / "demo_migration.json").read_text())
    assert '@app.post("/api/demo/load")' in app
    assert 'demo_migration.json' in app
    assert len(manifest["source_files"]) >= 6
    assert "platform_knowledge/canonical_contract.md" in manifest["migration_setup"]["knowledge_schema_ref"]
    assert "platform_knowledge/source_mapping_notes.md" in manifest["migration_setup"]["knowledge_rules_ref"]
    filenames = {item["path"].split("/")[-1] for item in manifest["source_files"]}
    assert {"accounts.csv", "households.csv", "clients.csv"}.issubset(filenames)
    assert any(name.startswith("account_") for name in filenames)
    assert any(name.startswith("household_") for name in filenames)
    assert any(name.startswith("client_") for name in filenames)


def test_demo_source_inputs_have_no_real_personal_identifiers():
    for path in (ROOT / "demo_data" / "source_files").glob("*.csv"):
        content = path.read_text()
        assert "example.test" in content or path.name != "clients.csv"


def test_entity_inference_business_rules_are_externalized_to_configuration():
    service = (ROOT / "control_plane" / "services" / "entity_inference.py").read_text()
    rules = __import__("json").loads((ROOT / "knowledge" / "entity_inference_rules.json").read_text())
    assert "load_entity_inference_rules" in service
    assert "entity_inference_rules.json" in service
    assert "ALIASES =" not in service
    assert "ENTITY_FIELDS =" not in service
    assert set(rules["entities"]) == {"Account", "Household", "Client"}
    assert "minimum_confidence" in rules["decision"]


def test_demo_manifest_includes_multiple_files_for_each_supported_entity():
    import json
    manifest = json.loads((ROOT / "demo_data" / "demo_migration.json").read_text())
    paths = [item["path"] for item in manifest["source_files"]]
    assert len(paths) >= 6
    assert any("account_" in path for path in paths)
    assert any("household_" in path for path in paths)
    assert any("client_" in path for path in paths)
    assert "platform_knowledge/canonical_contract.md" in manifest["migration_setup"]["knowledge_schema_ref"]
    assert "platform_knowledge/source_mapping_notes.md" in manifest["migration_setup"]["knowledge_rules_ref"]


def test_setup_forms_are_not_nested_and_demo_load_lands_in_discovery():
    setup = (ROOT / "ui" / "templates" / "migration_setup.html").read_text()
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'id="knowledge-upload-form"' in setup
    assert 'id="migration-setup-form"' in setup
    assert 'form="migration-setup-form"' in setup
    assert 'url="/entity-sample-files?notice="' in app


def test_source_inventory_uses_file_level_review_for_multiple_extracts():
    state = (ROOT / "ui" / "state.py").read_text()
    source = (ROOT / "ui" / "templates" / "entity_sample_files.html").read_text()
    app = (ROOT / "ui" / "app.py").read_text()
    assert '"file_id": uuid.uuid4().hex' in state
    assert 'name="file_id" value="{{ file.file_id }}"' in source
    assert 'item.get("file_id") == file_id' in app
    assert 'Entity review queue' in (ROOT / "ui" / "templates" / "agent_pipeline.html").read_text()


def test_mapping_studio_only_shows_pending_proposals_for_active_migration():
    app = (ROOT / "ui" / "app.py").read_text()
    route = app.split('@app.get("/agent-pipeline"', 1)[1].split('@app.get("/reconciliation"', 1)[0]
    assert 'active_migration_id = str(state_store.questionnaire_data.get("migration_id", "UNCONFIGURED"))' in route
    assert "WHERE s.migration_id = ? AND s.status = 'PENDING'" in route
    assert '(active_migration_id, *selected_scope),' in route
    assert 'AND s.entity_name IN ({scope_placeholders})' in route


def test_dashboard_explains_wave_conditions_without_assigning_p0_records():
    dashboard = (ROOT / "ui" / "templates" / "dashboard.html").read_text()
    app = (ROOT / "ui" / "app.py").read_text()
    assert '"wave_conditions": CohortConditionEngine.get_condition_catalog()' in app
    assert "condition.condition_name" in dashboard
    assert "condition.parameter" in dashboard
    assert "condition.threshold_value" in dashboard
    assert "condition.business_rationale" in dashboard
    assert "No records or entities are assigned to a wave by this dashboard." in dashboard


def test_agent_assistance_is_integrated_into_source_discovery_not_a_separate_navigation_step():
    app = (ROOT / "ui" / "app.py").read_text()
    navigation = (ROOT / "ui" / "templates" / "base.html").read_text()
    discovery = (ROOT / "ui" / "templates" / "entity_sample_files.html").read_text()
    assert 'MigrationAgentCoordinator().analyze(' in app
    assert 'record["agent_analysis"] = agent_analysis' in app
    assert 'return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)' in app
    assert 'href="/agent-studio"' not in navigation
    assert "Agent assistance runs inside Source Discovery automatically." in navigation
    assert "Agent-assisted analysis · automatic" in discovery
    assert "Suggested source → canonical fields" in discovery
    assert "Confirm selected" in discovery and "Reject selected" in discovery



def test_workflow_validation_errors_use_actionable_ui_modals_not_raw_json():
    app = (ROOT / "ui" / "app.py").read_text()
    discovery = (ROOT / "ui" / "templates" / "entity_sample_files.html").read_text()
    mapping = (ROOT / "ui" / "templates" / "agent_pipeline.html").read_text()
    assert 'Enter a rejection reason before rejecting this file.' in app
    assert 'url="/agent-pipeline?error=" + quote_plus(message)' in app
    assert 'id="workflow-error-modal"' in discovery
    assert 'id="workflow-error-modal"' in mapping
    assert 'Cannot approve all {entity_name} mappings yet.' in app


def test_workflow_progress_counts_open_information_requests_from_the_database(tmp_path, monkeypatch):
    """Nav badge and dashboard must not depend on the Mapping page having been visited first."""
    import control_plane.db as db
    import ui.app as app_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "progress.db")
    db.init_db()
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-T"
    store.questionnaire_data["migration_setup"] = {"entities": ["Account", "Client"]}
    monkeypatch.setattr(app_module, "state_store", store)
    conn = db.get_db_connection()
    rows = [("R1", "Account", "OPEN"), ("R2", "Client", "OPEN"), ("R3", "Client", "RESOLVED")]
    for rid, entity, status in rows:
        conn.execute(
            "INSERT INTO mapping_information_requests (request_id, migration_id, entity_name, canonical_field,"
            " status, question, created_utc) VALUES (?, 'MIG-T', ?, ?, ?, 'q', '2026-10-27')",
            (rid, entity, rid, status))
    conn.commit()
    conn.close()

    assert app_module.workflow_progress()["rfi_open"] == 2


def test_demo_sign_in_requires_locally_supplied_hash(monkeypatch):
    import hashlib
    import json

    from ui.auth import SessionManager

    monkeypatch.delenv("XODUS_DEMO_PASSWORD_HASHES", raising=False)
    monkeypatch.setattr(SessionManager, "_load_local_password_hashes", staticmethod(lambda _path: {}))
    assert SessionManager().authenticate("jeeva@simplicitywealth.com", "anything", "Platform Admin") is None

    monkeypatch.undo()
    digest = hashlib.sha256(b"unit-test-secret").hexdigest()
    monkeypatch.setenv("XODUS_DEMO_PASSWORD_HASHES", json.dumps({"jeeva@simplicitywealth.com": digest}))
    manager = SessionManager()
    assert manager.authenticate("jeeva@simplicitywealth.com", "wrong", "Platform Admin") is None
    assert manager.authenticate("jeeva@simplicitywealth.com", "unit-test-secret", "Platform Admin")
    assert manager.authenticate("analyst.demo@simplicitywealth.com", "unit-test-secret", "Migration Analyst") is None


def _progress(**over):
    base = {"scope": ["Account"], "files_total": 2, "files_confirmed": 2, "files_pending": 0, "proposals_pending": 0,
            "mappings_approved": 5, "rfi_open": 0, "changes_open": 0, "validation_status": "", "validation_when": "",
            "target_provisioned": True, "next": {"step": "validate"}}
    base.update(over)
    return base


def test_workflow_view_derives_step_states_chip_and_tasks():
    import ui.app as app_module

    view = app_module.build_workflow_view(_progress(), {"migration_name": "M1", "source_firm": "CoreCap", "target_system": "Simplicity",
                                                         "canonical_version": "v1", "firm_config_version": "1+abc"}, "MIG-X")
    states = {s["key"]: s["state"] for s in view["steps"]}
    assert states == {"setup": "done", "discover": "done", "map": "done", "validate": "current"}
    assert view["status_chip"] == {"label": "Ready to validate", "tone": "info"}
    assert view["facts"]["route"] == "CoreCap → Simplicity" and view["facts"]["name"] == "M1"

    blocked = app_module.build_workflow_view(
        _progress(rfi_open=2, files_pending=1, files_confirmed=1, next={"step": "discover"}), {}, "MIG-X")
    assert {s["key"]: s["state"] for s in blocked["steps"]}["discover"] == "current"
    assert {s["key"]: s["state"] for s in blocked["steps"]}["map"] == "todo"
    assert [t["state"] for t in blocked["step_tasks"]["map"]][-2] == "needs_you"
    no_target = app_module.build_workflow_view(_progress(target_provisioned=False, next={"step": "map"}), {}, "MIG-X")
    assert {s["key"]: s["state"] for s in no_target["steps"]}["map"] == "current"
    assert [t["state"] for t in no_target["step_tasks"]["map"]][-1] == "needs_you" and no_target["status_chip"]["label"] == "In progress · Map & review"

    empty = app_module.build_workflow_view(_progress(scope=[], files_total=0, mappings_approved=0, next={"step": "setup"}), {}, "MIG-UNCONFIGURED")
    assert empty["status_chip"]["label"] == "Setup needed" and empty["facts"]["route"] == "Not configured"

    passed = app_module.build_workflow_view(_progress(validation_status="PASS", validation_when="2026-10-27"), {}, "MIG-X")
    assert passed["status_chip"]["label"] == "Validated"


def test_validated_chip_requires_every_earlier_step_to_be_complete():
    import ui.app as app_module

    view = app_module.build_workflow_view(
        _progress(validation_status="PASS", validation_when="2026-10-27", files_pending=1, files_confirmed=1, next={"step": "discover"}), {}, "MIG-X")
    assert view["status_chip"]["label"] == "In progress · Discover"


def test_knowledge_forms_are_disabled_for_roles_without_edit_rights(tmp_path, monkeypatch):
    import asyncio
    import httpx
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "k.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Tester", "role": "Validation Specialist"})

    async def page(role):
        app_module.session_mgr.active_sessions["sid"]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return (await client.get("/firm-knowledge")).text

    for role in ("Validation Specialist", "Domain SME"):
        html = asyncio.run(page(role))
        assert html.count("<fieldset disabled") == 2 and "Read-only for " + role in html
    for role in ("Migration Analyst", "Platform Admin"):
        html = asyncio.run(page(role))
        assert "<fieldset disabled" not in html and "Read-only for" not in html


def test_information_request_form_only_for_roles_allowed_to_answer(tmp_path, monkeypatch):
    import asyncio
    import httpx
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rfi.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-X"
    store.questionnaire_data["migration_setup"] = {"entities": ["Account", "Client", "Household"]}
    store.questionnaire_data["mapping_information_requests"] = [{
        "request_id": "RFI-T1", "migration_id": "MIG-X", "entity": "Account", "canonical_field": "CUSTODIAN_CODE",
        "status": "OPEN", "question": "Which source field supplies CUSTODIAN_CODE?", "source_file": "a.csv"}]
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid", {"email": "t@example.invalid", "name": "T", "role": "Platform Admin"})
    db.init_db()

    async def page(role):
        app_module.session_mgr.active_sessions["sid"]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return (await client.get("/agent-pipeline")).text

    for role, shown in (("Platform Admin", True), ("Migration Analyst", True), ("Domain SME", True), ("Validation Specialist", False)):
        html = asyncio.run(page(role))
        assert ("/api/mapping-gaps/RFI-T1/response" in html) is shown, role
        assert ("Read-only for " + role in html) is (not shown), role


def test_ui_walk_fixes_labels_and_no_raw_none_in_templates():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "ui" / "templates"
    login = (root / "login.html").read_text(encoding="utf-8")
    assert 'for="login-email"' in login and 'id="login-email"' in login and 'for="login-password"' in login and 'id="login-password"' in login
    assert 'aria-label="Identifier and relationship notes"' in (root / "firm_knowledge.html").read_text(encoding="utf-8")
    recon = (root / "reconciliation.html").read_text(encoding="utf-8")
    assert "item.canonical_column|default('UNMAPPED', true)" in recon  # None must not render as the word None


def test_setup_upload_row_grid_cannot_overflow_the_viewport():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "ui" / "templates" / "migration_setup.html").read_text(encoding="utf-8")
    assert "minmax(0,1fr) minmax(0,2fr) auto" in src and "[1fr_2fr_auto]" not in src  # a file input must not widen the page


def test_compact_workflow_bar_and_my_queue_are_in_templates():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "ui" / "templates"
    base = (root / "base.html").read_text(encoding="utf-8")
    assert 'class="runh"' in base and 'aria-label="Data migration navigation"' in base
    assert 'aria-label="My queue"' in (root / "dashboard.html").read_text(encoding="utf-8")
    tm = (root / "target_mapping.html").read_text(encoding="utf-8")
    assert 'aria-label="On this page"' in tm and 'id="target-mappings"' in tm and "Inferred target model" in tm


def test_target_mapping_wide_tables_scroll_inside_their_own_container():
    from pathlib import Path
    tm = (Path(__file__).resolve().parents[1] / "ui" / "templates" / "target_mapping.html").read_text(encoding="utf-8")
    assert tm.count('<div class="tw"><table') >= 2  # suite .tw wrapper scrolls inside its own container so wide tables do not widen the phone layout
    assert '<table class="mt-3' not in tm  # no bare unwrapped tables


def test_git_ignored_local_users_add_rehearsal_accounts_without_replacing_tracked_ones(tmp_path, monkeypatch):
    import json, shutil
    from ui.auth import SessionManager
    shutil.copy(ROOT / "demo_data" / "demo_users.json", tmp_path / "demo_users.json")
    (tmp_path / "demo_passwords.local.json").write_text(json.dumps({"extra@example.invalid": "h1", "jeeva@simplicitywealth.com": "h2"}))
    (tmp_path / "demo_users.local.json").write_text(json.dumps({"users": [
        {"email": "extra@example.invalid", "name": "Extra", "default_role": "Platform Admin", "can_load_demo": True},
        {"email": "jeeva@simplicitywealth.com", "name": "Impostor", "default_role": "Domain SME"}]}))
    monkeypatch.setattr(SessionManager, "DEMO_DIR", tmp_path)
    mgr = SessionManager()
    assert mgr.authorized_users["extra@example.invalid"]["name"] == "Extra" and mgr.authorized_users["extra@example.invalid"]["can_load_demo"] is False
    assert mgr.authorized_users["jeeva@simplicitywealth.com"]["name"] != "Impostor"  # a local file cannot override a tracked user
    (tmp_path / "demo_users.local.json").unlink()
    assert "extra@example.invalid" not in SessionManager().authorized_users  # absent file is fine


def test_base_template_keeps_scroll_position_after_actions():
    """Form posts are sent in place; the shared layout keeps the reader's scroll position, honours anchors and toasts the notice."""
    from pathlib import Path

    html = Path("ui/templates/base.html").read_text(encoding="utf-8")
    assert "window.scrollTo(0, y)" in html and "scrollIntoView" in html  # scroll kept; anchored redirects keep their own target
    assert 'get("notice")' in html  # notice shown as a toast, since the banner is off-screen
    assert "data-full-reload" in html  # a form can opt out of in-place handling


def test_suite_shell_links_all_spaces_and_keeps_governance_and_tenant_pages_out_of_the_rail():
    """Data Migration is the only live space; Deal Room and Governance stay visible but are not links."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "ui"
    base = _shell_source()
    assert 'href="/deal-room"' in base and 'href="/governance"' in base and "planned" not in base.lower().split("suite spaces")[1][:900]  # all three spaces are live
    assert 'href="/admin/users">Tenant settings' in base  # Governance and Tenant settings are their own spaces
    assert 'class="dm{% if suite_cur == \'dm\' %} on{% endif %}" href="/continue-data-migration"' in base
    for href in ("/target-mapping", "/firm-knowledge"):
        assert f'href="{href}"' in base  # setup inputs stay in the Data Migration rail
    rail = base[base.index('<aside class="side"'):base.index("</aside>")]
    assert "/canonical-changes" not in rail and "/admin/users" not in rail  # moved to Governance and Tenant settings, not duplicated
    for stage in ("Loader & Inspector", "Discovery", "Business Rules", "Schema Mapper", "Execution", "Reconciliation"):
        assert stage in base
    for asset in ("suite.css", "dm.css", "xodus.css", "legacy.css"):
        assert (root / "static" / asset).is_file()
    assert "cdn.tailwindcss.com" in base  # fallback kept while pages are converted
    assert "Party" not in base


def test_suite_home_is_the_post_login_landing_and_opens_data_migration_on_the_same_session():
    app = (ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    suite = (ROOT / "ui" / "templates" / "suite_home.html").read_text(encoding="utf-8")
    top = (ROOT / "ui" / "templates" / "_suite_top.html").read_text(encoding="utf-8")
    login = (ROOT / "ui" / "templates" / "login.html").read_text(encoding="utf-8")
    base = (ROOT / "ui" / "templates" / "base.html").read_text(encoding="utf-8")
    assert '@app.get("/suite"' in app and 'RedirectResponse(url="/suite"' in app  # login lands on the suite home
    assert 'href="/continue-data-migration"' in suite and 'href="/migration-setup"' in suite  # Data Migration and its setup open from the suite home
    assert 'href="/"' in (ROOT / "ui" / "templates" / "continue_data_migration.html").read_text(encoding="utf-8")
    assert "Planned" not in top  # Deal Room and Governance are live, served from the supplied template
    assert "Sign in to the Xodus suite" in login and "no second password" in login
    assert 'class="xbrand" href="/suite"' in _shell_source()  # the brand returns to the suite home


def test_persona_selector_switches_in_place_and_rejects_open_redirects(tmp_path, monkeypatch):
    import asyncio, httpx
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "p.db"); monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    app_module.state_store = state_module.WorkspaceStateStore(); db.init_db()
    app_module.session_mgr.active_sessions["sid-p"] = {"email": "jeeva@simplicitywealth.com", "name": "Jeeva S.", "role": "Platform Admin"}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://t", cookies={"session_id": "sid-p"}) as c:
            page = await c.get("/suite")
            assert page.status_code == 200 and 'name="role"' in page.text and "Migration Analyst" in page.text
            assert (await c.get("/continue-data-migration")).status_code == 200
            back = await c.post("/persona-selection", data={"role": "Migration Analyst", "next": "/migration-setup"})
            assert back.headers["location"] == "/migration-setup"
            assert app_module.session_mgr.active_sessions["sid-p"]["role"] == "Migration Analyst"
            evil = await c.post("/persona-selection", data={"role": "Platform Admin", "next": "//evil.example"})
            assert evil.headers["location"] == "/suite"
    asyncio.run(run())
    app_module.session_mgr.active_sessions.pop("sid-p", None)


def test_deal_room_and_governance_serve_the_template_pages_with_app_links_and_synced_role(tmp_path, monkeypatch):
    import asyncio, httpx
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "s.db"); monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    app_module.state_store = state_module.WorkspaceStateStore(); db.init_db()
    app_module.session_mgr.active_sessions["sid-s"] = {"email": "t@example.invalid", "name": "Ann", "role": "Migration Analyst"}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://t", cookies={"session_id": "sid-s"}) as c:
            deal = await c.get("/deal-room"); gov = await c.get("/governance")
            assert deal.status_code == 200 and gov.status_code == 200
            for page in (deal.text, gov.text):
                assert 'localStorage.setItem("xodus.role","MA")' in page
                assert ".html" not in page.replace("filename", "") or all(h not in page for h in ("index.html", "data-migration.html", "deal-room.html", "governance.html", "tenant.html"))
                assert "/continue-data-migration" in page and "/suite" in page
            assert 'href="/canonical-changes"' in gov.text  # the live change-control page stays reachable from Governance
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://t") as anon:
            assert (await anon.get("/governance", follow_redirects=False)).status_code in (302, 303, 307, 401)
    asyncio.run(run())
    app_module.session_mgr.active_sessions.pop("sid-s", None)

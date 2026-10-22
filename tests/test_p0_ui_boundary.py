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


def test_dashboard_knowledge_assets_are_not_fabricated_by_default_state():
    state = (ROOT / "ui" / "state.py").read_text()
    assert "Amplify UMA Sleeve and Holding Schema Contract" not in state
    assert 'self.firm_knowledge_docs: List[Dict[str, Any]] = []' in state
    assert '"program_status": "P0_SOURCE_TO_CANONICAL"' in state

def test_p0_navigation_exposes_persona_first_integrated_control_plane_workflow():
    navigation = (ROOT / "ui" / "templates" / "base.html").read_text()
    for route in ('/persona-selection', '/migration-setup', '/entity-sample-files', '/agent-pipeline#pending-mappings', '/reconciliation'):
        assert f'href="{route}"' in navigation
    assert 'href="/firm-knowledge"' not in navigation
    assert 'href="/hitl"' not in navigation
    assert "Persona / Role" in navigation
    assert "Source Discovery & Entity Scope" in navigation
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
    shell = (ROOT / "ui" / "templates" / "base.html").read_text()
    for token in ("Space Grotesk", "IBM Plex Mono", "#152C6B", "xa-topbar", "xa-sidebar", "xa-right"):
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



def test_persona_selection_is_a_separate_step_after_login():
    login = (ROOT / "ui" / "templates" / "login.html").read_text()
    persona = (ROOT / "ui" / "templates" / "persona_selection.html").read_text()
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'name="role"' not in login
    assert 'name="role"' in persona
    assert '@app.get("/persona-selection"' in app
    assert '@app.post("/persona-selection"' in app
    assert 'RedirectResponse(url="/persona-selection"' in app


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


def test_persona_selection_is_not_cached_and_back_navigation_shows_active_role():
    app = (ROOT / "ui" / "app.py").read_text()
    persona = (ROOT / "ui" / "templates" / "persona_selection.html").read_text()
    assert 'response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"' in app
    assert 'response.headers["Pragma"] = "no-cache"' in app
    assert '"selected_role":user["role"]' in app
    assert '{% if role.id == selected_role %}checked{% endif %}' in persona
    assert '{% if loop.first %}checked{% endif %}' not in persona


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
    assert "SUPPORTED_APPROVED_TRANSFORMS" in app
    assert 'Approve all {{ suggestion.entity_name }} mappings' in studio
    assert "ENTITY_LEVEL_MAPPING_APPROVED" in app


def test_persona_switch_is_limited_to_roles_assigned_to_authenticated_user():
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'assigned_role = session_mgr.authorized_users.get(user["email"].lower(), {}).get("default_role", user["role"])' in app
    assert 'if assigned_role != "Platform Admin" and role != assigned_role:' in app
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
    assert "A rejection reason is required." in app


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
    assert len(users["users"][0]["password_sha256"]) == 64


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
    assert "Confirm entity" in discovery and "Reject" in discovery



def test_workflow_validation_errors_use_actionable_ui_modals_not_raw_json():
    app = (ROOT / "ui" / "app.py").read_text()
    discovery = (ROOT / "ui" / "templates" / "entity_sample_files.html").read_text()
    mapping = (ROOT / "ui" / "templates" / "agent_pipeline.html").read_text()
    assert 'Enter a rejection reason before rejecting this file.' in app
    assert 'url="/agent-pipeline?error=" + quote_plus(message)' in app
    assert 'id="workflow-error-modal"' in discovery
    assert 'id="workflow-error-modal"' in mapping
    assert 'Cannot approve all {entity_name} mappings yet.' in app

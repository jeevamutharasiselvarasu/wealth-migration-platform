from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_p0_ui_does_not_render_deferred_wealth_modules_or_wave_planning():
    pipeline = (ROOT / "ui" / "templates" / "agent_pipeline.html").read_text()
    dashboard = (ROOT / "ui" / "templates" / "dashboard.html").read_text()
    knowledge = (ROOT / "ui" / "templates" / "firm_knowledge.html").read_text()
    for deferred in ("Sleeve Math Agent", "Sleeve / Allocation", "Position (Snapshot)", "Execute 9-Agent Pipeline", "Restricted Models"):
        assert deferred not in pipeline
    assert "Primary Custodian Rails" not in dashboard
    assert "Condition ID" not in dashboard
    assert "Practical Segregation Conditions" not in dashboard
    assert "name=\"sleeve_invariant\"" not in knowledge
    assert "name=\"aum_drift_gate\"" not in knowledge


def test_dashboard_knowledge_assets_are_not_fabricated_by_default_state():
    state = (ROOT / "ui" / "state.py").read_text()
    assert "Amplify UMA Sleeve and Holding Schema Contract" not in state
    assert 'self.firm_knowledge_docs: List[Dict[str, Any]] = []' in state
    assert '"program_status": "P0_SOURCE_TO_CANONICAL"' in state

def test_p0_navigation_exposes_only_control_plane_workflow():
    navigation = (ROOT / "ui" / "templates" / "base.html").read_text()
    for route in ('/firm-knowledge', '/entity-selection', '/entity-sample-files', '/agent-pipeline'):
        assert f'href="{route}"' in navigation
    assert 'href="/hitl"' not in navigation
    assert 'href="/reconciliation"' in navigation
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
    assert "Analyse file & infer entity" in source
    assert "infer_entity_from_headers" in inference
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

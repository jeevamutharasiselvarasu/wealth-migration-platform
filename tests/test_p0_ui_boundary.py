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
    assert 'href="/reconciliation"' not in navigation
    assert "DATA PLANE (SPECIALIST)" not in navigation

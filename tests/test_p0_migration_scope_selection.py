from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_migration_setup_exposes_only_the_three_p0_entity_scope_options():
    template = (ROOT / "ui" / "templates" / "migration_setup.html").read_text()
    assert 'name="entities"' in template
    assert "Account" in template and "Household" in template and "Client" in template
    assert "CoreCap" in template
    assert "Simplicity" in template


def test_setup_requires_at_least_one_selected_entity_and_marks_others_out_of_scope():
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'form.getlist("entities")' in app
    assert "Select at least one in-scope entity" in app
    assert 'else "OUT_OF_SCOPE"' in app


def test_upload_and_approval_routes_enforce_selected_entity_scope():
    app = (ROOT / "ui" / "app.py").read_text()
    assert "file was not registered for downstream processing" in app
    assert "cannot be approved or mapped" in app
    assert 'entity_name not in configured_scope' in app


def test_agent_studio_is_part_of_primary_navigation_and_has_deferred_execution_notice():
    nav = (ROOT / "ui" / "templates" / "base.html").read_text()
    page = (ROOT / "ui" / "templates" / "agent_studio.html").read_text()
    assert 'href="/agent-studio"' in nav
    assert "Agents classify, profile and recommend" in page
    assert "No source values are transformed" in page
    assert "wave assignment remains deferred" in page

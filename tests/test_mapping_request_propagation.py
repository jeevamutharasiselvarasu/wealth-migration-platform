from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_approved_mapping_reconciles_required_field_information_requests():
    app = (ROOT / "ui" / "app.py").read_text()
    assert "def _sync_mapping_information_requests(migration_id: str) -> None:" in app
    assert "FROM canonical_mappings" in app
    assert "AND is_confirmed = 1" in app
    assert 'item["status"] = "RESOLVED"' in app
    assert 'item["status"] = "OPEN"' in app
    assert 'item["resolution"] = "Approved source-to-canonical mapping exists."' in app


def test_single_and_entity_level_approval_propagate_to_information_requests():
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'conn.commit()\n        _sync_mapping_information_requests(str(row["migration_id"]))' in app
    assert 'conn.commit()\n        _sync_mapping_information_requests(migration_id)' in app
    assert '_sync_mapping_information_requests(active_migration_id)' in app


def test_resolved_required_fields_are_not_recreated_as_open_duplicate_requests():
    app = (ROOT / "ui" / "app.py").read_text()
    assert "existing_requests = {" in app
    assert 'str(item.get("migration_id", migration_id)) == str(migration_id)' in app
    assert "(entity_name, canonical_field) not in existing_requests" in app


def test_mapping_studio_explains_dynamic_resolution_of_required_fields():
    template = (ROOT / "ui" / "templates" / "agent_pipeline.html").read_text()
    assert "Resolved dynamically:" in template
    assert "an approved source-to-canonical mapping now supplies this required field" in template
    assert "information_requests|selectattr('status','equalto','OPEN')|list|length" in template

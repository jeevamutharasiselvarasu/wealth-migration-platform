from control_plane.db import init_db
from ui.state import WorkspaceStateStore


def test_source_files_survive_scope_changes_and_identical_reuploads(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.state as state_module

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "source-lifecycle.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    init_db()
    state = WorkspaceStateStore()
    state.questionnaire_data["migration_id"] = "MIG-SCOPE-LIFECYCLE"
    state.questionnaire_data["migration_setup"] = {
        "entities": ["Account", "Client"],
    }

    uploaded = []
    fixtures = [
        ("account_master.csv", b"account_id,account_name\nA1,Alpha\n"),
        ("account_address.csv", b"account_id,address\nA1,1 Main St\n"),
        ("account_type.csv", b"account_id,account_type\nA1,IRA\n"),
        ("household_master.csv", b"household_id,household_name\nH1,House One\n"),
        ("client_master.csv", b"client_id,first_name\nC1,Casey\n"),
        ("client_contact.csv", b"client_id,email\nC1,casey@example.test\n"),
    ]
    for filename, body in fixtures:
        entity = "Household" if filename.startswith("household") else (
            "Account" if filename.startswith("account") else "Client"
        )
        uploaded.append(state.attach_entity_sample_file(entity, filename, body))

    assert sum(len(files) for files in state.entity_source_files.values()) == 6
    assert len({record["file_id"] for record in uploaded}) == 6

    # Deselecting Household changes eligibility, not the retained upload inventory.
    state.save_entity_scopes({"Household": "OUT_OF_SCOPE"})
    assert len(state.entity_source_files["Household"]) == 1
    state.questionnaire_data["migration_setup"]["entities"].append("Household")
    state.save_entity_scopes({"Household": "IN_SCOPE"})
    assert len(state.entity_source_files["Household"]) == 1
    assert sum(len(files) for files in state.entity_source_files.values()) == 6

    # An identical re-upload is idempotent; a same-name file with changed bytes is distinct.
    duplicate = state.attach_entity_sample_file(
        "Household", "household_master.csv", b"household_id,household_name\nH1,House One\n"
    )
    assert duplicate["file_id"] == uploaded[3]["file_id"]
    changed = state.attach_entity_sample_file(
        "Household", "household_master.csv", b"household_id,household_name\nH2,House Two\n"
    )
    assert changed["file_id"] != uploaded[3]["file_id"]
    assert len(state.entity_source_files["Household"]) == 2
    assert sum(len(files) for files in state.entity_source_files.values()) == 7

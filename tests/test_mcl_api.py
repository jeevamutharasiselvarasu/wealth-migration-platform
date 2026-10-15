import asyncio
import json
import pytest
from pathlib import Path

from ui import app as app_module


class FakeStateStore:
    def __init__(self, files):
        self.entity_scopes = {"Account": "IN_SCOPE", "Household": "IN_SCOPE", "Client": "IN_SCOPE"}
        self.entity_sample_files = files
        self.questionnaire_data = {"migration_id": "MIG-MCL-TEST"}
        self.persisted_summary = None

    def persist_mcl_execution_summary(self, **kwargs):
        self.persisted_summary = kwargs


def test_mcl_batch_endpoint_runs_three_entities_and_persists_summary(tmp_path, monkeypatch):
    files = {}
    for entity_name in ("Account", "Household", "Client"):
        path = tmp_path / f"{entity_name.lower()}.csv"
        path.write_text("sample\nvalue\n", encoding="utf-8")
        files[entity_name] = {
            "status": "UPLOADED",
            "stored_path": str(path),
            "filename": path.name,
        }

    fake_state = FakeStateStore(files)
    monkeypatch.setattr(app_module, "state_store", fake_state)

    def fake_map_entity(entity_name, actor="SourceMappingWorkflow"):
        canonical = "Client" if entity_name == "Client" else entity_name
        records = {
            "Account": [{"ACCT_NUM": "A1", "HH_ID": "H1", "CLIENT_ID": "P1"}],
            "Household": [{"HH_ID": "H1"}],
            "Client": [{"CLIENT_ID": "P1"}],
        }[entity_name]
        fields = sorted(records[0].keys())
        mapping = {
            "entity": entity_name,
            "canonical_entity": canonical,
            "status": "SUCCESS",
            "record_count": 1,
            "mapped_records": records,
            "mapped_fields": fields,
            "unmapped_fields": [],
            "missing_required": [],
            "confidence_score": 1.0,
            "warnings": [],
            "field_lineages": [],
        }
        return {
            "status": "SUCCESS",
            "run_id": f"RUN-{entity_name}",
            "entity_name": entity_name,
            "canonical_entity": canonical,
            "source_file": files[entity_name]["filename"],
            "profile": {"row_count": 1, "column_count": len(fields)},
            "mapping": mapping,
        }

    monkeypatch.setattr(app_module, "_map_uploaded_source_entity", fake_map_entity)
    response = asyncio.run(app_module.map_mcl_uploaded_files())
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["status"] == "NEEDS_REVIEW"
    assert set(payload["entities"]) == {"Account", "Household", "Client"}
    assert payload["entities"]["Client"]["canonical_entity"] == "Client"
    assert payload["entities"]["Client"]["mapping"]["mapped_records_withheld"] is True
    assert payload["entities"]["Client"]["mapping"]["preview_record_count"] == 1
    assert "mapped_records" not in payload["entities"]["Client"]["mapping"]
    assert payload["relationship_validation"]["status"] == "PASS"
    assert payload["execution_boundary"]["target_loading"] == "NOT_RUN"
    assert payload["execution_boundary"]["canonical_output_status"] == "PREVIEW_ONLY_PENDING_APPROVAL"
    assert payload["execution_boundary"]["wave_assignment"] == "DISABLED"
    assert fake_state.persisted_summary["mcl_run_id"] == payload["mcl_run_id"]


def test_mapping_suggestion_review_lifecycle(tmp_path, monkeypatch):
    from control_plane import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "controlplane.db")
    db_module.init_db()
    conn = db_module.get_db_connection()
    conn.execute(
        "INSERT INTO mapping_suggestions "
        "(suggestion_id, run_id, migration_id, entity_name, source_field, canonical_field, "
        "proposed_rule, confidence_score, status, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
        ("SUG-1", "RUN-1", "MIG-1", "Account", "LEGACY_ACCT_NO", "ACCT_NUM", "ALIAS", 0.8, "2026-10-15T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(app_module, "get_db_connection", db_module.get_db_connection)

    pending = asyncio.run(app_module.list_source_mapping_suggestions())
    pending_payload = json.loads(pending.body)
    assert pending_payload["status"] == "SUCCESS"
    assert len(pending_payload["suggestions"]) == 1
    assert pending_payload["suggestions"][0]["status"] == "PENDING"

    approved = asyncio.run(
        app_module.review_source_mapping_suggestion(
            "SUG-1", decision="APPROVED", reviewer="Domain SME", comments="Verified against source dictionary"
        )
    )
    approved_payload = json.loads(approved.body)
    assert approved_payload["suggestion"]["status"] == "APPROVED"
    assert approved_payload["suggestion"]["reviewer"] == "Domain SME"

    with pytest.raises(app_module.HTTPException) as repeated:
        asyncio.run(
            app_module.review_source_mapping_suggestion(
                "SUG-1", decision="REJECTED", reviewer="Domain SME", comments="repeat decision"
            )
        )
    assert repeated.value.status_code == 409

    conn = db_module.get_db_connection()
    audit = conn.execute("SELECT decision, stage, reviewer FROM approvals WHERE artifact_id = 'SUG-1'").fetchone()
    assert tuple(audit) == ("APPROVED", "SOURCE_TO_CANONICAL_MAPPING", "Domain SME")
    reusable = conn.execute(
        "SELECT migration_id, entity_name, source_field, canonical_field, is_confirmed "
        "FROM canonical_mappings WHERE source_field = 'LEGACY_ACCT_NO'"
    ).fetchone()
    assert tuple(reusable) == ("MIG-1", "Account", "LEGACY_ACCT_NO", "ACCT_NUM", 1)
    # Rejected proposals are auditable but must never become reusable rules.
    conn.execute(
        "INSERT INTO mapping_suggestions "
        "(suggestion_id, run_id, migration_id, entity_name, source_field, canonical_field, "
        "proposed_rule, confidence_score, status, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
        ("SUG-2", "RUN-2", "MIG-1", "Account", "BAD_ACCT_FIELD", "ACCT_NUM", "ALIAS", 0.5, "2026-10-15T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    rejected = asyncio.run(
        app_module.review_source_mapping_suggestion(
            "SUG-2", decision="REJECTED", reviewer="Domain SME", comments="Not supported by source dictionary"
        )
    )
    assert json.loads(rejected.body)["suggestion"]["status"] == "REJECTED"

    conn = db_module.get_db_connection()
    assert conn.execute(
        "SELECT COUNT(*) FROM canonical_mappings WHERE source_field = 'BAD_ACCT_FIELD'"
    ).fetchone()[0] == 0
    # A confirmed rule is reusable only for its exact migration and entity.
    conn.execute(
        "INSERT INTO canonical_mappings "
        "(mapping_id, migration_id, entity_name, source_field, canonical_field, mapping_type, "
        "transformation_expression, confidence_score, is_confirmed, created_by, updated_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("MAP-OTHER", "MIG-OTHER", "Account", "OTHER_SOURCE", "ACCT_NUM", "APPROVED_ALIAS", "DIRECT_COPY", 1.0, 1, "SME", "2026-10-15T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO canonical_mappings "
        "(mapping_id, migration_id, entity_name, source_field, canonical_field, mapping_type, "
        "transformation_expression, confidence_score, is_confirmed, created_by, updated_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("MAP-PENDING", "MIG-1", "Account", "UNAPPROVED_SOURCE", "ACCT_NUM", "ALIAS", "DIRECT_COPY", 0.9, 0, "System", "2026-10-15T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    approved_catalog = app_module._get_approved_mapping_catalog("MIG-1", "Account")
    assert approved_catalog == {"LEGACY_ACCT_NO": "ACCT_NUM"}
    assert app_module._get_approved_mapping_catalog("MIG-OTHER", "Account") == {
        "OTHER_SOURCE": "ACCT_NUM"
    }
    assert app_module._get_approved_mapping_catalog("MIG-1", "Household") == {}

    # The approved alias is actually consumed by the mapper; unapproved fields are not.
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    effective_mapper = SchemaMapperAgent(custom_catalog={**app_module.schema_mapper.catalog, **approved_catalog})
    mapped = effective_mapper.map_source_to_canonical(
        "Account", [{"LEGACY_ACCT_NO": "000123", "UNAPPROVED_SOURCE": "secret"}]
    )
    assert mapped["mapped_records"][0]["ACCT_NUM"] == "000123"
    assert "UNAPPROVED_SOURCE" in mapped["unmapped_fields"]


def test_single_entity_mapping_does_not_expose_unapproved_canonical_rows(monkeypatch):
    mapping = {
        "status": "SUCCESS",
        "mapped_records": [{"CLIENT_ID": "PRIVATE-001"}],
        "field_lineages": [{"source_field": "CLIENT_ID", "canonical_field": "CLIENT_ID", "sample_value": "PRIVATE-001"}],
    }
    result = {"status": "SUCCESS", "mapping": mapping, "execution_boundary": {}}
    monkeypatch.setattr(app_module, "_map_uploaded_source_entity", lambda entity_name: result)
    response = asyncio.run(app_module.map_uploaded_entity_file("Client"))
    payload = json.loads(response.body)
    assert payload["mapping"]["mapped_records_withheld"] is True
    assert payload["mapping"]["preview_record_count"] == 1
    assert "mapped_records" not in payload["mapping"]
    assert "PRIVATE-001" not in response.body.decode("utf-8")



@pytest.mark.parametrize(
    ("endpoint", "entity"),
    [
        (app_module.map_pilot_account, "Account"),
        (app_module.map_pilot_household, "Household"),
        (app_module.map_pilot_client, "Client"),
    ],
)
def test_pilot_mapping_apis_withhold_rows_and_raw_samples(endpoint, entity, monkeypatch):
    from types import SimpleNamespace

    raw_id = "SENSITIVE-ROW-001"
    monkeypatch.setattr(
        app_module,
        "schema_mapper",
        SimpleNamespace(
            map_source_to_canonical=lambda entity_name, records: {
                "entity": entity_name,
                "status": "SUCCESS",
                "record_count": 1,
                "mapped_records": [{"CANONICAL_ID": raw_id}],
                "field_lineages": [
                    {"source_field": "SOURCE_ID", "canonical_field": "CANONICAL_ID", "sample_value": raw_id}
                ],
                "relationship_candidates": [{"source_key": raw_id}],
            }
        ),
    )
    monkeypatch.setattr(app_module.state_store, "persist_mapping_lineage", lambda *args, **kwargs: None)
    response = asyncio.run(endpoint(records=[{"SOURCE_ID": raw_id}]))
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["mapping"]["mapped_records_withheld"] is True
    assert payload["mapping"]["preview_record_count"] == 1
    assert "mapped_records" not in payload["mapping"]
    assert "relationship_candidates" not in payload["mapping"]
    assert raw_id not in response.body.decode("utf-8")



def test_p0_scope_selector_and_upload_catalog_are_limited_to_three_entities(monkeypatch):
    from ui.state import WorkspaceStateStore

    state = WorkspaceStateStore()
    menu = state.get_full_menu_card()
    displayed = {
        entity["entity_name"]
        for entities in menu.values()
        for entity in entities
    }
    assert displayed == {"Account", "Client", "Household"}

    # Other canonical entities cannot be re-enabled through the P0 scope form.
    state.save_entity_scopes({
        "Account": "IN_SCOPE",
        "Client": "IN_SCOPE",
        "Household": "IN_SCOPE",
        "Advisor / Rep": "IN_SCOPE",
        "Party": "IN_SCOPE",
    })
    uploads = {entity["entity_name"] for entity in state.get_source_upload_entities()}
    assert uploads == {"Account", "Client", "Household"}
    assert "Advisor / Rep" not in state.entity_scopes or state.entity_scopes["Advisor / Rep"] != "IN_SCOPE"

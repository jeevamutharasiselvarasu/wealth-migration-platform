import asyncio
import json
from pathlib import Path

from ui import app as app_module


class FakeStateStore:
    def __init__(self, files):
        self.entity_scopes = {"Account": "IN_SCOPE", "Household": "IN_SCOPE", "Party": "IN_SCOPE"}
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
        canonical = "Party" if entity_name == "Client" else entity_name
        records = {
            "Account": [{"ACCT_NUM": "A1", "HH_ID": "H1", "PARTY_ID": "P1"}],
            "Household": [{"HH_ID": "H1"}],
            "Client": [{"PARTY_ID": "P1"}],
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
    assert payload["status"] == "MAPPED"
    assert set(payload["entities"]) == {"Account", "Household", "Client"}
    assert payload["entities"]["Client"]["canonical_entity"] == "Party"
    assert payload["relationship_validation"]["status"] == "PASS"
    assert payload["execution_boundary"]["target_loading"] == "NOT_RUN"
    assert payload["execution_boundary"]["wave_assignment"] == "DISABLED"
    assert fake_state.persisted_summary["mcl_run_id"] == payload["mcl_run_id"]

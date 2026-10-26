import asyncio
import copy
from pathlib import Path
from urllib.parse import urlencode

import pytest
import yaml

from control_plane.kernel.wealth_kernel import WealthManagementKnowledgeKernel
from control_plane.services.firm_config import FirmConfigError, load_firm_config

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "config" / "firm_config.yaml"


def _write_variant(tmp_path, mutate):
    raw = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "firm_config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_seed_firm_config_is_p0_source_to_canonical_only():
    config = load_firm_config(SEED)
    assert config.firm_id == "SIMPLICITY"
    assert config.target_system.name == "Simplicity"
    assert config.scope_direction == "source_to_canonical"
    assert sorted(config.entities) == ["Account", "Client", "Household"]


def test_required_fields_match_platform_kernel_required_fields():
    config = load_firm_config(SEED)
    for group in WealthManagementKnowledgeKernel.get_canonical_entities().values():
        for item in group:
            assert sorted(config.required_fields(item["entity_name"])) == sorted(item["required_fields"])


def test_rejects_party_or_missing_entity(tmp_path):
    path = _write_variant(tmp_path, lambda raw: raw["entities"].update({"Party": copy.deepcopy(raw["entities"]["Client"])}))
    with pytest.raises(FirmConfigError):
        load_firm_config(path)
    path = _write_variant(tmp_path, lambda raw: raw["entities"].pop("Household"))
    with pytest.raises(FirmConfigError):
        load_firm_config(path)


@pytest.mark.parametrize("mutate", [
    lambda raw: raw["entities"]["Account"]["fields"][0].update({"nullable": True}),   # nullable key
    lambda raw: raw["entities"]["Account"]["fields"].append({"name": "ACCT_NUM", "type": "string"}),  # duplicate
    lambda raw: raw["entities"]["Client"]["fields"][0].update({"type": "uuid"}),      # unknown type
    lambda raw: raw.update({"scope_direction": "canonical_to_target"}),               # out of scope direction
])
def test_rejects_invalid_contracts(tmp_path, mutate):
    with pytest.raises(FirmConfigError):
        load_firm_config(_write_variant(tmp_path, mutate))


def test_missing_config_raises(tmp_path):
    with pytest.raises(FirmConfigError):
        load_firm_config(tmp_path / "absent.yaml")


def test_version_id_is_stable_and_changes_with_contract(tmp_path):
    base = load_firm_config(SEED)
    assert load_firm_config(SEED).version_id == base.version_id
    changed = load_firm_config(_write_variant(
        tmp_path, lambda raw: raw["entities"]["Client"]["fields"][1].update({"nullable": True})))
    assert changed.config_version == base.config_version
    assert changed.version_id != base.version_id
    assert base.version_id.startswith("1.0.0+")


def _setup_client(tmp_path, monkeypatch, name):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / f"{name}.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module.session_mgr, "demo_mode", True)
    session_id = f"firm-config-{name}"
    monkeypatch.setitem(app_module.session_mgr.active_sessions, session_id,
                        {"email": "test@example.invalid", "name": "Firm Config Test", "role": "Platform Admin"})
    state = WorkspaceStateStore()
    monkeypatch.setattr(app_module, "state_store", state)
    return app_module, session_id, state


def _post_setup(app_module, session_id, canonical_version="Simplicity-MCL-v1"):
    import httpx

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            return await client.post(
                "/api/migration/setup",
                content=urlencode([
                    ("migration_name", "CoreCap to Simplicity"), ("source_firm", "CoreCap"),
                    ("target_system", "Simplicity"), ("canonical_version", canonical_version),
                    ("source_acquisition", "SAMPLE_FILES"), ("entities", "Account"),
                ]),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
    return asyncio.run(run())


def test_migration_setup_is_stamped_with_firm_config_version_and_survives_restart(tmp_path, monkeypatch):
    from ui.state import WorkspaceStateStore

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "stamp")
    response = _post_setup(app_module, session_id)
    assert response.status_code == 303
    expected = load_firm_config().version_id
    assert state.questionnaire_data["migration_setup"]["firm_config_version"] == expected
    assert state.questionnaire_data["migration_setup"]["firm_id"] == "SIMPLICITY"
    restarted = WorkspaceStateStore()
    assert restarted.questionnaire_data["migration_setup"]["firm_config_version"] == expected


def test_second_migration_reuses_same_firm_config_version(tmp_path, monkeypatch):
    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "reuse")
    assert _post_setup(app_module, session_id).status_code == 303
    first = state.questionnaire_data["migration_setup"]["firm_config_version"]
    assert _post_setup(app_module, session_id).status_code == 303
    assert state.questionnaire_data["migration_setup"]["firm_config_version"] == first


def test_setup_rejects_contract_reference_that_differs_from_firm_config(tmp_path, monkeypatch):
    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "mismatch")
    response = _post_setup(app_module, session_id, canonical_version="Other-Contract-v9")
    assert response.status_code == 400
    assert "migration_setup" not in state.questionnaire_data


def test_setup_requires_a_valid_firm_config(tmp_path, monkeypatch):
    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "missing")
    monkeypatch.setenv("XODUS_FIRM_CONFIG", str(tmp_path / "absent.yaml"))
    response = _post_setup(app_module, session_id)
    assert response.status_code == 409
    assert "migration_setup" not in state.questionnaire_data



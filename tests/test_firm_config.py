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


# --- Mapper and dashboard use the firm configuration as the single source ---

def test_mapper_required_fields_come_from_firm_config():
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent

    config = load_firm_config(SEED)
    for entity in ("Account", "Client", "Household"):
        assert SchemaMapperAgent.required_fields(entity) == config.required_fields(entity)
    assert "REG_TYPE" in SchemaMapperAgent.required_fields("Account")
    assert "TAX_ID" in SchemaMapperAgent.required_fields("Client")


def test_mapper_required_fields_fall_back_when_firm_config_unavailable(tmp_path, monkeypatch):
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent

    monkeypatch.setenv("XODUS_FIRM_CONFIG", str(tmp_path / "absent.yaml"))
    assert SchemaMapperAgent.required_fields("Household") == SchemaMapperAgent.MANDATORY_CANONICAL_FIELDS["Household"]
    assert SchemaMapperAgent.required_fields("Sleeve") == SchemaMapperAgent.MANDATORY_CANONICAL_FIELDS["Sleeve"]


def test_columns_named_like_canonical_fields_map_to_themselves_without_false_gaps():
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent

    # Mirrors the demo account_registration.csv headers.
    records = [{
        "LEGACY_ACCT_NO": "A-1001", "CUSTODIAN_CODE": "CUST-A", "REGISTRATION_SRC_TYPE": "INDIVIDUAL",
        "ACCOUNT_OPEN_DT": "2022-04-12", "REP_CODE": "R-01",
    }]
    result = SchemaMapperAgent().map_source_to_canonical("Account", records)
    lineage = {item["source_field"]: item for item in result["field_lineages"]}
    for field in ("CUSTODIAN_CODE", "REP_CODE"):
        assert lineage[field]["canonical_field"] == field
        assert lineage[field]["rule_applied"] == "DIRECT_COPY"
    assert result["mapped_records"][0]["REP_CODE"] == "R-01"  # identity columns are not rewritten
    assert result["missing_required"] == []
    assert not result["unmapped_fields"]


def test_identity_mapping_is_entity_scoped_and_ignores_unknown_columns():
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent

    # CUSTODIAN_CODE is canonical for Account only and is not in the alias catalog.
    account = SchemaMapperAgent().map_source_to_canonical("Account", [{"CUSTODIAN_CODE": "C1", "NOT_A_FIELD": "x"}])
    assert account["mapped_fields"] == ["CUSTODIAN_CODE"]
    assert account["unmapped_fields"] == ["NOT_A_FIELD"]
    client = SchemaMapperAgent().map_source_to_canonical("Client", [{"CUSTODIAN_CODE": "C1"}])
    assert client["mapped_fields"] == []
    assert client["unmapped_fields"] == ["CUSTODIAN_CODE"]


def test_alias_catalog_behavior_is_unchanged_for_known_aliases():
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent

    result = SchemaMapperAgent().map_source_to_canonical("Account", [{"ADVISOR_REP_NUM": "014"}])
    assert result["mapped_records"][0]["REP_CODE"] == "REP-014"  # existing PREFIX_FORMAT rule still applies


def test_dashboard_lists_required_fields_from_firm_config(tmp_path, monkeypatch):
    import httpx

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "dash")

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            await client.post(
                "/api/migration/setup",
                content=urlencode([
                    ("migration_name", "CoreCap to Simplicity"), ("source_firm", "CoreCap"),
                    ("target_system", "Simplicity"), ("canonical_version", "Simplicity-MCL-v1"),
                    ("source_acquisition", "SAMPLE_FILES"), ("entities", "Account"), ("entities", "Client"),
                ]),
                headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
            return await client.get("/")

    page = asyncio.run(run()).text
    config = load_firm_config(SEED)
    assert " · ".join(config.required_fields("Account")) in page
    assert " · ".join(config.required_fields("Client")) in page
    assert "TAX_ID" in page and "REG_TYPE" in page


def test_suggestion_confidence_is_per_field_not_the_penalized_file_score(tmp_path, monkeypatch):
    import sqlite3

    import control_plane.db as db
    import ui.state as state_module
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "confidence.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-CONF"
    # Required Account fields are mostly absent, so the file-level score is heavily penalised.
    result = SchemaMapperAgent().map_source_to_canonical(
        "Account", [{"LEGACY_ACCT_NO": "A-1", "REP_CODE": "R-1", "UNKNOWN_COL": "x"}])
    assert result["confidence_score"] < 0.5
    store.persist_mapping_lineage(result, run_id="RUN-CONF")
    conn = sqlite3.connect(tmp_path / "confidence.db")
    rows = dict(conn.execute("SELECT source_field, confidence_score FROM mapping_suggestions WHERE migration_id='MIG-CONF'"))
    assert rows == {"LEGACY_ACCT_NO": SchemaMapperAgent.ALIAS_MATCH_CONFIDENCE,
                    "REP_CODE": SchemaMapperAgent.IDENTITY_MATCH_CONFIDENCE}


def test_exact_name_columns_create_suggestions_and_no_false_information_requests(tmp_path, monkeypatch):
    """Route-level check using the demo account_registration.csv shape."""
    import httpx

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "identity-route")
    assert _post_setup(app_module, session_id).status_code == 303
    sample = (
        b"LEGACY_ACCT_NO,CUSTODIAN_CODE,REGISTRATION_SRC_TYPE,ACCOUNT_OPEN_DT,REP_CODE\r\n"
        b"A-1001,CUST-A,INDIVIDUAL,2022-04-12,R-01\r\n"
        b"A-1002,CUST-B,JOINT,2021-09-03,R-02\r\n"
        b"A-1003,CUST-A,TRUST,2020-01-19,R-01\r\n"
    )

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            up = await client.post("/api/entities/upload-sample-file",
                                   files=[("files", ("account_registration.csv", sample, "text/csv"))],
                                   follow_redirects=False)
            assert up.status_code == 303
            bucket, record = next((entity, rec) for entity, files in state.entity_source_files.items()
                                  for rec in files.values() if rec.get("filename") == "account_registration.csv")
            ok = await client.post(
                "/api/entities/confirm-inference",
                content=urlencode([("entity_name", bucket), ("decision", "APPROVE"), ("comments", "ok"),
                                   ("target_entity", "Account"), ("file_id", record["file_id"])]),
                headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
            assert ok.status_code == 303
            gen = await client.post("/api/entities/map-approved-files", follow_redirects=False)
            assert gen.status_code == 303
            return (await client.get("/api/mapping-suggestions")).json()["suggestions"]

    suggestions = {item["source_field"]: item for item in asyncio.run(run()) if item["entity_name"] == "Account"}
    for field in ("REP_CODE", "CUSTODIAN_CODE"):
        assert suggestions[field]["canonical_field"] == field
        assert suggestions[field]["confidence_score"] == 0.95
    assert suggestions["LEGACY_ACCT_NO"]["canonical_field"] == "ACCT_NUM"
    open_gaps = {(i["entity"], i["canonical_field"]) for i in
                 state.questionnaire_data.get("mapping_information_requests", []) if i.get("status") == "OPEN"}
    assert ("Account", "REP_CODE") not in open_gaps
    assert ("Account", "CUSTODIAN_CODE") not in open_gaps


def test_entity_bulk_approval_waits_for_open_required_field_requests(tmp_path, monkeypatch):
    import sqlite3

    import httpx

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "bulk-gap")
    assert _post_setup(app_module, session_id).status_code == 303
    # Only two Account columns: required fields such as CUSTODIAN_CODE stay unmapped.
    sample = b"LEGACY_ACCT_NO,ACCOUNT_OPEN_DT\r\nA-1,2022-04-12\r\nA-2,2021-09-03\r\n"
    form = {"Content-Type": "application/x-www-form-urlencoded"}

    def suggestion_statuses():
        conn = sqlite3.connect(tmp_path / "bulk-gap.db")
        try:
            return {row[0] for row in conn.execute("SELECT status FROM mapping_suggestions WHERE entity_name='Account'")}
        finally:
            conn.close()

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            await client.post("/api/entities/upload-sample-file",
                              files=[("files", ("accounts.csv", sample, "text/csv"))], follow_redirects=False)
            bucket, record = next((e, r) for e, fs in state.entity_source_files.items()
                                  for r in fs.values() if r.get("filename") == "accounts.csv")
            await client.post("/api/entities/confirm-inference",
                              content=urlencode([("entity_name", bucket), ("decision", "APPROVE"), ("comments", "ok"),
                                                 ("target_entity", "Account"), ("file_id", record["file_id"])]),
                              headers=form, follow_redirects=False)
            await client.post("/api/entities/map-approved-files", follow_redirects=False)
            body = urlencode([("entity_name", "Account"), ("reviewer", "ignored"), ("comments", "bulk")])
            blocked = await client.post("/api/mapping-suggestions/approve-entity", content=body,
                                        headers=form, follow_redirects=False)
            blocked_state = suggestion_statuses()
            open_gaps = [g for g in state.questionnaire_data["mapping_information_requests"]
                         if g["entity"] == "Account" and g["status"] == "OPEN"]
            for gap in open_gaps:
                answered = await client.post(f"/api/mapping-gaps/{gap['request_id']}/response",
                                             content=urlencode([("response_text", "Follow-up extract will supply this field.")]),
                                             headers=form, follow_redirects=False)
                assert answered.status_code == 303
            approved = await client.post("/api/mapping-suggestions/approve-entity", content=body,
                                         headers=form, follow_redirects=False)
            return blocked, blocked_state, open_gaps, approved

    blocked, blocked_state, open_gaps, approved = asyncio.run(run())
    assert open_gaps, "required Account fields should have produced information requests"
    assert blocked.status_code == 303 and "error=" in blocked.headers["location"]
    assert "open+information+request" in blocked.headers["location"]
    assert blocked_state == {"PENDING"}  # nothing approved while gaps are open
    assert approved.status_code == 303 and "notice=" in approved.headers["location"]
    assert suggestion_statuses() == {"APPROVED"}


def test_required_fields_are_covered_across_files_of_the_same_entity(tmp_path, monkeypatch):
    """Two Account files together supply every required field: no RFIs, and bulk approval needs no answers."""
    import httpx

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "cross-file")
    assert _post_setup(app_module, session_id).status_code == 303
    first = b"LEGACY_ACCT_NO,ACCOUNT_OPEN_DT\r\nA-1,2022-04-12\r\nA-2,2021-09-03\r\n"
    second = (b"CUSTODIAN_CODE,REGISTRATION_SRC_TYPE,REP_CODE\r\n"
              b"CUST-A,INDIVIDUAL,R-01\r\nCUST-B,JOINT,R-02\r\n")
    form = {"Content-Type": "application/x-www-form-urlencoded"}

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            await client.post("/api/entities/upload-sample-file",
                              files=[("files", ("accounts.csv", first, "text/csv")),
                                     ("files", ("account_registration.csv", second, "text/csv"))],
                              follow_redirects=False)
            for name in ("accounts.csv", "account_registration.csv"):
                bucket, record = next((e, r) for e, fs in state.entity_source_files.items()
                                      for r in fs.values() if r.get("filename") == name)
                await client.post("/api/entities/confirm-inference",
                                  content=urlencode([("entity_name", bucket), ("decision", "APPROVE"), ("comments", "ok"),
                                                     ("target_entity", "Account"), ("file_id", record["file_id"])]),
                                  headers=form, follow_redirects=False)
            await client.post("/api/entities/map-approved-files", follow_redirects=False)
            open_gaps = [g["canonical_field"] for g in state.questionnaire_data.get("mapping_information_requests", [])
                         if g["entity"] == "Account" and g["status"] == "OPEN"]
            approved = await client.post(
                "/api/mapping-suggestions/approve-entity",
                content=urlencode([("entity_name", "Account"), ("reviewer", "x"), ("comments", "bulk")]),
                headers=form, follow_redirects=False)
            return open_gaps, approved

    open_gaps, approved = asyncio.run(run())
    # Only the file generated first can have raised requests; any it raised are for fields the other file supplies.
    assert set(open_gaps) <= {"CUSTODIAN_CODE", "REG_TYPE", "REP_CODE", "ACCT_NUM", "OPEN_DATE"}
    assert approved.status_code == 303 and "notice=" in approved.headers["location"], approved.headers["location"]


def test_mapping_kinds_follow_the_registered_rule_family():
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent as M

    assert M.mapping_kind("DIRECT_COPY", "ACCT_NUM") == "DIRECT"
    assert M.mapping_kind("CUSTODIAN_LOOKUP (SCHWAB -> SCHW)", "CUSTODIAN_CODE") == "LOOKUP"
    assert M.mapping_kind("REG_TYPE_LOOKUP (IRA -> IRA)", "REG_TYPE") == "LOOKUP"
    assert M.mapping_kind("PREFIX_FORMAT (REP-1)", "REP_CODE") == "DERIVED"
    assert M.mapping_kind("DATE_FORMAT (MM/DD/YYYY -> YYYY-MM-DD)", "OPEN_DATE") == "DERIVED"
    assert M.mapping_kind("RELATIONSHIP_CANDIDATE_PROPOSED", None) == "RELATIONSHIP"
    assert M.mapping_kind("UNRESOLVED_NO_CANONICAL_FIELD", None) == "UNRESOLVED"
    assert M.mapping_kind("DIRECT_COPY", None) == "UNRESOLVED"  # never a direct copy without a destination
    result = M().map_source_to_canonical("Account", [{"ACCT_NUM": "A-1", "MYSTERY": "x", "ADVISOR_REP_NUM": "7"}])
    kinds = {l["source_field"]: l["mapping_kind"] for l in result["field_lineages"]}
    assert kinds["ACCT_NUM"] == "DIRECT" and kinds["MYSTERY"] == "UNRESOLVED" and kinds["ADVISOR_REP_NUM"] == "DERIVED"


def test_relationship_file_needs_an_explicit_disposition_and_is_not_mapped(tmp_path, monkeypatch):
    import httpx

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "rel-disp")
    assert _post_setup(app_module, session_id).status_code == 303
    link = b"OWNER_REF,OBJECT_REF,LINK_KIND,VALID_FROM,VALID_TO\r\nC-900,G-100,PRIMARY,2020-01-01,\r\n"
    form = {"Content-Type": "application/x-www-form-urlencoded"}

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            await client.post("/api/entities/upload-sample-file",
                              files=[("files", ("batch_004.csv", link, "text/csv"))], follow_redirects=False)
            bucket, record = next((e, r) for e, fs in state.entity_source_files.items()
                                  for r in fs.values() if r.get("filename") == "batch_004.csv")
            status_before = record["inference"]["status"]

            def body(note):
                return urlencode([("entity_name", bucket), ("decision", "APPROVE"), ("comments", note),
                                  ("target_entity", "RELATIONSHIP_DEFERRED"), ("file_id", record["file_id"])])
            missing = await client.post("/api/entities/confirm-inference", content=body(""), headers=form, follow_redirects=False)
            unchanged = record["inference"]["status"]
            done = await client.post("/api/entities/confirm-inference",
                                     content=body("Client-to-household link; no relationship entity in P0"),
                                     headers=form, follow_redirects=False)
            gen = await client.post("/api/entities/map-approved-files", follow_redirects=False)
            suggestions = (await client.get("/api/mapping-suggestions")).json()["suggestions"]
            return status_before, missing, unchanged, done, gen, suggestions, record

    status_before, missing, unchanged, done, gen, suggestions, record = asyncio.run(run())
    assert status_before == "RELATIONSHIP_REVIEW"
    assert "error=" in missing.headers["location"] and unchanged == "RELATIONSHIP_REVIEW"
    assert done.status_code == 303 and "error=" not in done.headers["location"]
    assert record["inference"]["status"] == "RELATIONSHIP_DEFERRED"
    assert record["inference_review"]["decision"] == "RELATIONSHIP_DEFERRED"
    assert "no relationship entity" in record["inference_review"]["comments"]
    assert suggestions == []  # a deferred link file creates no mapping proposals


def test_value_evidence_needs_name_and_values_and_stays_capped():
    from control_plane.services.value_evidence import MAX_EVIDENCE_CONFIDENCE, propose_from_evidence

    cols = {
        "ACCT_REF": ["000847", "000848", "000849"],      # unique id-like, ACCT token: key evidence
        "OPENED_ON": ["2021-04-12", "2019-11-03", "2023-01-29"],
        "STATUS_CODE": ["ACTIVE", "CLOSED", "ACTIVE"],   # nothing supports a required field
        "ZZZ": ["2021-04-12", "2019-11-03", "2023-01-29"],  # date values but no name hint
        "REVIEW_DATE": ["2024-02-01", "2024-03-14", "2024-05-22"],  # date values, no OPEN token
    }
    got = {p["canonical_field"]: p for p in propose_from_evidence(["ACCT_NUM", "OPEN_DATE", "REG_TYPE"], cols, 3)}
    assert set(got) == {"ACCT_NUM", "OPEN_DATE"}
    assert got["ACCT_NUM"]["source_field"] == "ACCT_REF" and got["OPEN_DATE"]["source_field"] == "OPENED_ON"
    assert all(p["confidence"] <= MAX_EVIDENCE_CONFIDENCE for p in got.values())
    # Name hint without supporting values is not enough (duplicate ids), and ties stay unresolved.
    assert propose_from_evidence(["ACCT_NUM"], {"ACCT_REF": ["1", "1", "2"]}, 3) == []
    tie = {"ACCT_REF": ["1", "2", "3"], "ACCOUNT_KEY": ["4", "5", "6"]}
    assert propose_from_evidence(["ACCT_NUM"], tie, 3) == []


def test_mapper_emits_evidence_proposals_with_basis_and_never_for_mapped_fields():
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent

    rows = [{"ACCT_NUM": "A-1", "REG_CODE": "IRA", "OPENED_ON": "2021-04-12"},
            {"ACCT_NUM": "A-2", "REG_CODE": "TOD", "OPENED_ON": "2020-01-02"}]
    result = SchemaMapperAgent().map_source_to_canonical("Account", rows)
    lineage = {l["source_field"]: l for l in result["field_lineages"]}
    assert lineage["ACCT_NUM"]["confidence"] == SchemaMapperAgent.ALIAS_MATCH_CONFIDENCE  # catalog match still wins
    for column, target in (("REG_CODE", "REG_TYPE"), ("OPENED_ON", "OPEN_DATE")):
        assert lineage[column]["canonical_field"] == target
        assert lineage[column]["mapping_basis"] == "VALUE_EVIDENCE"
        assert lineage[column]["confidence"] <= 0.6
        assert lineage[column]["rule_applied"].startswith("DIRECT_COPY (VALUE_EVIDENCE:")
        assert SchemaMapperAgent.mapping_kind(lineage[column]["rule_applied"], target) == "DIRECT"
        assert SchemaMapperAgent.supports_transformation(lineage[column]["rule_applied"])
    assert result["missing_required"] == ["CUSTODIAN_CODE", "REP_CODE"]


def test_low_confidence_evidence_proposals_need_individual_review(tmp_path, monkeypatch):
    import httpx

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "evidence-bulk")
    assert _post_setup(app_module, session_id).status_code == 303
    sample = b"ACCT_REF,REG_CODE\r\n000847,IRA\r\n000848,TOD\r\n"
    form = {"Content-Type": "application/x-www-form-urlencoded"}

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            await client.post("/api/entities/upload-sample-file",
                              files=[("files", ("batch_001.csv", sample, "text/csv"))], follow_redirects=False)
            bucket, record = next((e, r) for e, fs in state.entity_source_files.items()
                                  for r in fs.values() if r.get("filename") == "batch_001.csv")
            await client.post("/api/entities/confirm-inference",
                              content=urlencode([("entity_name", bucket), ("decision", "APPROVE"), ("comments", "ok"),
                                                 ("target_entity", "Account"), ("file_id", record["file_id"])]),
                              headers=form, follow_redirects=False)
            await client.post("/api/entities/map-approved-files", follow_redirects=False)
            sugg = (await client.get("/api/mapping-suggestions")).json()["suggestions"]
            bulk = await client.post("/api/mapping-suggestions/approve-entity",
                                     content=urlencode([("entity_name", "Account"), ("reviewer", "x"), ("comments", "b")]),
                                     headers=form, follow_redirects=False)
            one = next(s for s in sugg if s["source_field"] == "ACCT_REF")
            single = await client.post(f"/api/mapping-suggestions/{one['suggestion_id']}/review",
                                       content=urlencode([("decision", "APPROVED"), ("reviewer", "x"),
                                                          ("comments", "ACCT_REF is the account number per SME")]),
                                       headers=form, follow_redirects=False)
            return sugg, bulk, single

    sugg, bulk, single = asyncio.run(run())
    by_field = {s["source_field"]: s for s in sugg}
    assert by_field["ACCT_REF"]["canonical_field"] == "ACCT_NUM" and by_field["ACCT_REF"]["confidence_score"] == 0.6
    assert by_field["REG_CODE"]["canonical_field"] == "REG_TYPE" and by_field["REG_CODE"]["mapping_kind"] == "DIRECT"
    assert "error=" in bulk.headers["location"] and "low-confidence" in bulk.headers["location"]
    assert single.status_code == 200 and single.json().get("status") in {"SUCCESS", "APPROVED"}, single.text


def _post_demo_load(app_module, session_id):
    import httpx

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            return await client.post("/api/demo/load", follow_redirects=False)
    return asyncio.run(run())


def test_demo_load_is_stamped_with_firm_config_version_and_survives_restart(tmp_path, monkeypatch):
    from control_plane.services.workflow_event_engine import read_events
    from ui.state import WorkspaceStateStore

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "demo-stamp")
    assert _post_demo_load(app_module, session_id).status_code == 303
    expected = load_firm_config().version_id
    setup = state.questionnaire_data["migration_setup"]
    assert setup["firm_config_version"] == expected and setup["firm_id"] == "SIMPLICITY"
    migration_id = state.questionnaire_data["migration_id"]
    context = [e for e in read_events(migration_id, limit=5000) if e["event_type"] == "MIGRATION_CONTEXT_UPDATED"]
    assert context and context[-1]["payload"]["migration_setup"]["firm_config_version"] == expected
    restarted = WorkspaceStateStore()
    assert restarted.questionnaire_data["migration_setup"]["firm_config_version"] == expected


def test_demo_load_requires_a_valid_firm_config(tmp_path, monkeypatch):
    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "demo-missing")
    monkeypatch.setenv("XODUS_FIRM_CONFIG", str(tmp_path / "absent.yaml"))
    assert _post_demo_load(app_module, session_id).status_code == 409
    assert "migration_setup" not in state.questionnaire_data


def test_mapping_suggestions_api_is_scoped_to_the_active_migration(tmp_path, monkeypatch):
    import sqlite3

    import httpx

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "scope")
    assert _post_setup(app_module, session_id).status_code == 303
    active = state.questionnaire_data["migration_id"]
    conn = sqlite3.connect(tmp_path / "scope.db")
    for mig, field in ((active, "A_FIELD"), ("MIG-OLD", "B_FIELD")):
        conn.execute("INSERT INTO mapping_suggestions (suggestion_id, run_id, migration_id, entity_name, source_field, "
                     "canonical_field, proposed_rule, confidence_score, status, created_utc) "
                     "VALUES (?, 'R', ?, 'Account', ?, 'ACCT_NUM', 'DIRECT_COPY', 0.9, 'PENDING', '2026-01-01')",
                     (f"S-{mig}", mig, field))
    conn.commit()
    conn.close()

    async def run():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            default = (await client.get("/api/mapping-suggestions")).json()["suggestions"]
            explicit = (await client.get("/api/mapping-suggestions", params={"migration_id": "MIG-OLD"})).json()["suggestions"]
            everything = (await client.get("/api/mapping-suggestions", params={"migration_id": "ALL"})).json()["suggestions"]
            return default, explicit, everything

    default, explicit, everything = asyncio.run(run())
    assert [s["source_field"] for s in default] == ["A_FIELD"]
    assert [s["source_field"] for s in explicit] == ["B_FIELD"]
    assert {s["source_field"] for s in everything} == {"A_FIELD", "B_FIELD"}


def test_review_controls_are_shown_only_to_approving_roles(tmp_path, monkeypatch):
    import sqlite3

    import httpx

    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "controls")
    assert _post_setup(app_module, session_id).status_code == 303
    active = state.questionnaire_data["migration_id"]
    conn = sqlite3.connect(tmp_path / "controls.db")
    conn.execute("INSERT INTO mapping_suggestions (suggestion_id, run_id, migration_id, entity_name, source_field, "
                 "canonical_field, proposed_rule, confidence_score, status, created_utc) "
                 "VALUES ('S-1', 'R', ?, 'Account', 'ACCT_REF', 'ACCT_NUM', 'DIRECT_COPY', 0.9, 'PENDING', '2026-01-01')", (active,))
    conn.commit()
    conn.close()

    async def page(role):
        app_module.session_mgr.active_sessions[session_id]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session_id": session_id}) as client:
            return (await client.get("/agent-pipeline")).text

    for role, allowed in (("Platform Admin", True), ("Domain SME", True),
                          ("Migration Analyst", False), ("Validation Specialist", False)):
        html = asyncio.run(page(role))
        assert ("Approve mapping" in html) is allowed, role
        assert ("Approve all Account mappings" in html) is allowed, role
        assert ("Read-only for" in html) is (not allowed), role

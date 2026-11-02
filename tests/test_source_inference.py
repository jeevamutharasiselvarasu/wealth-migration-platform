"""Generalization guard for source-column inference (renamed columns the platform has never seen)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "generalization"))
from control_plane.services import source_inference as si  # noqa: E402

DIR = Path(__file__).parent / "generalization"


def _run(file):
    data = json.loads((DIR / file).read_text())
    right = wrong = total = 0
    for c in data["cases"]:
        got = {p["source_field"]: p["canonical_field"] for p in si.infer_source_mappings(c["entity"], c["rows"])}
        for src, want in c["truth"].items():
            total += 1
            g = got.get(src)
            if g == want:
                right += 1
            elif g:
                wrong += 1
    return right, wrong, total


def test_tuned_set_is_fully_mapped():
    right, wrong, total = _run("renamed_source_cases.json")
    assert (right, wrong) == (total, 0)


def test_holdout_set_stays_above_floor():
    # measured 36/40 with 2 wrong when written; the floor guards against regressions, not a target
    right, wrong, total = _run("renamed_source_holdout.json")
    assert right >= 36 and wrong <= 2 and total == 40


def test_gate_rejects_unknown_fields_and_incompatible_shapes():
    canonical = {"OPEN_DATE": {"type": "date", "description": ""}, "LEGAL_NAME": {"type": "string", "description": ""}}
    cols = {"opened": si.profile_column(["2020-01-01"]), "nm": si.profile_column(["Ann Lee"])}
    props = [{"source_field": "opened", "canonical_field": "LEGAL_NAME", "confidence": 0.9},   # date into text field
             {"source_field": "nm", "canonical_field": "NOT_A_FIELD", "confidence": 0.9},        # invented field
             {"source_field": "opened", "canonical_field": "OPEN_DATE", "confidence": 0.9},
             {"source_field": "nm", "canonical_field": "LEGAL_NAME", "confidence": 0.1}]         # below threshold
    out = si.gate("Client", props, cols, canonical)
    assert [(p["source_field"], p["canonical_field"]) for p in out] == [("opened", "OPEN_DATE")]


def test_profile_keeps_shapes_not_values():
    prof = si.profile_column(["000-00-0001", "000-00-0002"])
    assert prof["top"] == "ssn" and "0001" not in json.dumps({k: v for k, v in prof.items() if k != "blob"})


def test_mapper_proposes_inferred_columns_with_capped_confidence_and_evidence():
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    rows = json.loads((DIR / "renamed_source_cases.json").read_text())["cases"][0]["rows"]
    res = SchemaMapperAgent().map_source_to_canonical("Account", rows)
    inferred = {l["source_field"]: l for l in res["field_lineages"] if l.get("mapping_basis") == "NAME_VALUE_INFERENCE"}
    assert inferred["mkt_val"]["canonical_field"] == "BALANCE"
    assert all(l["confidence"] <= SchemaMapperAgent.INFERENCE_CONFIDENCE_CAP and l["rule_applied"] == "DIRECT_COPY" for l in inferred.values())
    assert inferred["mkt_val"]["evidence"]
    assert not res["unmapped_fields"]


def test_known_catalog_names_are_unchanged_by_inference():
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    rows = [{"LEGACY_ACCT_NO": "AC-1", "CUSTODIAN": "CUST-A", "REGISTRATION_TYPE": "INDIVIDUAL", "ACCOUNT_OPEN_DT": "2021-03-15"}]
    res = SchemaMapperAgent().map_source_to_canonical("Account", rows)
    assert not any(l.get("mapping_basis") == "NAME_VALUE_INFERENCE" for l in res["field_lineages"])


def _target(file):
    import score_target
    return score_target.run(file=file, verbose=False)


def test_unseen_target_tuned_schema():
    tables, right, total = _target("unseen_target_schema.json")
    assert tables == 3 and right >= 18 and total == 20  # two known ambiguities: custodian name vs code, display name


def test_unseen_target_holdout_floor():
    # measured 23/25 (camelCase splitting fixed after the first run scored 18/25); one known wrong match: groupingId on accounts
    tables, right, total = _target("unseen_target_holdout.json")
    assert tables == 3 and right >= 23 and total == 25

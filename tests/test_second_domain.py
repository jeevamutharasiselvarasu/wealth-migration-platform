"""Proof that nothing wealth-specific is needed to run another domain: a public library (members, books, loans) with its own canonical
configuration, target schema, source files and rules file, in a fresh process pointed at those folders. No code changes, no wealth words."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "second_domain"

SCRIPT = r'''
import csv, json, sys
from pathlib import Path
fix = Path(sys.argv[1])
from control_plane.services import firm_config as fc, target_inference as ti, entity_classification_agent as ca, rule_inference as ri, sql_package as sp
from control_plane.services import mcl_validation as mv, entity_resolution as er, value_evidence as ve
from agents.discovery.schema_mapper_agent import SchemaMapperAgent
cfg = fc.load_firm_config()
canonical = {e: {f.name: f.type for f in d.fields} for e, d in cfg.entities.items()}
model = ti.infer(json.loads((fix / "target_schema.json").read_text(encoding="utf-8")), canonical)
classified = {}
for name in ("patrons.csv", "books.csv", "checkouts.csv"):
    rows = list(csv.DictReader((fix / name).open(encoding="utf-8")))
    res = ca.classify_source_file(list(rows[0].keys()), records=rows)
    classified[name] = [res["status"], res["entity"]]
text = (fix / "library_rules.md").read_text(encoding="utf-8")
rules = [r for r in ri.infer(text) if r["recognised"]]
hints = ri.split_hints(text)
blank = next(r for r in rules if r["rule_type"] == "IF_BLANK")
sql = sp.translate("RULE:X@1", "RETURNED_ON", {"status": "APPROVED", "rule_type": "IF_BLANK", "params": blank["params"]})
members = [{"MEMBER_ID": "P-1", "FULL_NAME": "Ada"}, {"MEMBER_ID": "P-2", "FULL_NAME": "Alan"}]
items = [{"ITEM_ID": "B-1", "TITLE": "x"}]
loans = [{"LOAN_ID": "L-1", "MEMBER_ID": "P-1", "ITEM_ID": "B-1"}, {"LOAN_ID": "L-2", "MEMBER_ID": "P-9", "ITEM_ID": "B-1"}]
valid = mv.validate_mcl_relationships({"Member": {"mapped_records": members}, "Item": {"mapped_records": items}, "Loan": {"mapped_records": loans}})
mapped = SchemaMapperAgent().map_source_to_canonical("Member", [{"MEMBER_ID": "P-1", "FULL_NAME": "Ada", "Shoe": "9"}])
derived = er.derive_rows("Responsibility", {"Loan": [{"LOAN_ID": "L-1", "FAMILY_ID": "F1"}, {"LOAN_ID": "L-2", "FAMILY_ID": "F9"}],
                                           "Member": [{"MEMBER_ID": "P-2", "FAMILY_ID": "F1", "PRIORITY": "2"}, {"MEMBER_ID": "P-1", "FAMILY_ID": "F1", "PRIORITY": "1"}]})
derived_sql = er.derive_sql("Responsibility", "canon", "m1", "m1_Responsibility")
everything = [t for b in model["entities"].values() for t in b["table_columns"]] + model["unassigned_tables"]
print(json.dumps({
    "entities": list(fc.P0_ENTITIES),
    "target_entities": sorted(model["entities"]),
    "unassigned": model["unassigned_tables"],
    "tables": {e: b["target_table"] for e, b in model["entities"].items()},
    "classified": classified,
    "rule_types": sorted({r["rule_type"] for r in rules}),
    "hints": [(h["target_field"], h["entity"], h["source_field"]) for h in hints],
    "supporting": fc.supporting_plan(["Loan"], ["Loan"], cfg),
    "sql": sql,
    "derived": [derived, er.method("Responsibility"), er.recipe_for("AccountRole"), derived_sql],
    "validation": [valid["status"], [(c["entity"], c["field"], c["status"]) for c in valid["key_checks"]],
                   [(c["from_entity"], c["from_field"], c["to_entity"], c["status"]) for c in valid["relationship_checks"]]],
    "mapper": [mapped["mapped_records"], mapped["unmapped_fields"], SchemaMapperAgent.required_fields("Loan")],
    "resolution": [er.suggested_rules(), {e: sorted(v) for e, v in er.references().items()}],
    "merge_keys": [sp._is_key_field("LOAN_ID", "Loan"), sp._is_key_field("MEMBER_ID", "Loan")],
    "evidence": ve.propose_from_evidence(["MEMBER_ID"], {"card": ["P-1", "P-2"]}, 2),
    "unassigned_name": ti.entity_name_for_table(model["unassigned_tables"][0], everything) if model["unassigned_tables"] else "",
}))
'''


def _run():
    (FIX / "knowledge").mkdir(exist_ok=True)
    (FIX / "platform_knowledge").mkdir(exist_ok=True)
    env = {**os.environ, "XODUS_FIRM_CONFIG": str(FIX / "firm_config.yaml"), "XODUS_PLATFORM_KNOWLEDGE_DIR": str(FIX / "platform_knowledge"),
           "XODUS_KNOWLEDGE_DIR": str(FIX / "knowledge"), "PYTHONUTF8": "1"}
    out = subprocess.run([sys.executable, "-c", SCRIPT, str(FIX)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_library_runs_through_the_same_code_with_only_its_own_files():
    r = _run()
    assert r["entities"] == ["Member", "Item", "Loan"]  # entity names come from the configuration file
    assert r["target_entities"] == ["Item", "Loan", "Member"] and r["tables"]["Member"] == "LIB_MEMBER"  # the target's tables, matched to them
    assert r["unassigned"] == ["LIB_SHELF_AUDIT"] and r["unassigned_name"] == "ShelfAudit"  # a table with no canonical entity is reported, named from its table
    assert r["classified"] == {"patrons.csv": ["INFERRED", "Member"], "books.csv": ["INFERRED", "Item"], "checkouts.csv": ["INFERRED", "Loan"]}
    assert set(r["rule_types"]) == {"IF_BLANK", "VALUE_MAP", "CONCAT", "COPY"}
    assert r["hints"] == [["RETURN_STATUS", "Loan", "RETURNED_ON"]]
    assert r["sql"] == "CASE WHEN NULLIF(LTRIM(RTRIM(s.[RETURNED_ON])), N'') IS NULL THEN N'Out' ELSE N'Returned' END"  # the rule becomes SQL for this domain too
    assert r["validation"][0] == "FAIL"  # keys of every entity are checked, and Loan.MEMBER_ID P-9 points at no member
    assert r["validation"][1] == [["Member", "MEMBER_ID", "PASS"], ["Item", "ITEM_ID", "PASS"], ["Loan", "LOAN_ID", "PASS"]]
    assert r["validation"][2] == [["Loan", "MEMBER_ID", "Member", "FAIL"], ["Loan", "ITEM_ID", "Item", "PASS"]]
    assert r["mapper"][0] == [{"MEMBER_ID": "P-1", "FULL_NAME": "Ada"}] and r["mapper"][1] == ["Shoe"]  # a column named like a canonical field maps to it
    assert "LOAN_ID" in r["mapper"][2]  # required fields come from the configuration
    assert r["resolution"] == [{}, {"Loan": ["ITEM_ID", "MEMBER_ID"]}]  # no suggested rules without a file; references derived from keys
    assert r["merge_keys"] == [True, False] and r["evidence"] == []
    assert r["derived"][0] == [{"LOAN_ID": "L-1", "MEMBER_ID": "P-1", "ROLE": "PAYER"}] and r["derived"][1] == "FAMILY_HEAD_PAYS"  # a recipe of the library's own
    assert r["derived"][2] is None  # the first domain's recipe is not there
    assert "[canon].[m1_Loan] AS a JOIN" in r["derived"][3] and "[PRIORITY])) IN (N'1')" in r["derived"][3] and "N'PAYER'" in r["derived"][3]
    text = json.dumps(r).lower()
    assert not any(word in text for word in ("household", "advisor", "custodian", "account", "client", "wealth"))  # nothing from the first domain leaks in
    assert r["supporting"] == {"Member": "Loan.MEMBER_ID points at it", "Item": "Loan.ITEM_ID points at it"}  # from key names, not from a list

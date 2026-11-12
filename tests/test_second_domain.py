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
    text = json.dumps(r).lower()
    assert not any(word in text for word in ("household", "advisor", "custodian", "account", "client", "wealth"))  # nothing from the first domain leaks in
    assert r["supporting"] == {"Member": "Loan.MEMBER_ID points at it", "Item": "Loan.ITEM_ID points at it"}  # from key names, not from a list

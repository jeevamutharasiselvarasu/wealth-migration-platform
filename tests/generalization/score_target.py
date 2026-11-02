"""Score target_inference on an unseen target schema. Usage: python tests/generalization/score_target.py"""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from control_plane.services import target_inference
from control_plane.services.firm_config import get_firm_config

def run(infer=None, verbose=True, file="unseen_target_schema.json"):
    d = json.loads((Path(__file__).parent / file).read_text())
    cfg = get_firm_config()
    canonical = {e: {f.name: f.type for f in cfg.entities[e].fields} for e in cfg.entities}
    model = (infer or target_inference.infer)(d["schema"], canonical)
    got_tables = {t: e for e, b in model["entities"].items() for t in b["target_table"].split(", ")}
    tr = sum(got_tables.get(t) == e for t, e in d["truth_tables"].items())
    right = wrong = 0
    for e, b in model["entities"].items():
        for f in b["fields"]:
            key = f["name"]; want = d["truth_columns"].get(key); got = f["canonical_hint"]
            if got == want: right += 1
            else:
                wrong += 1
                if verbose: print(f"  {key}: got {got}, want {want}")
    total = len(d["truth_columns"])
    if verbose: print(f"tables {tr}/{len(d['truth_tables'])}; columns correct {right}/{total}; wrong/missed {wrong}; unassigned {model['unassigned_tables']}")
    return tr, right, total

if __name__ == "__main__":
    for f in ("unseen_target_schema.json", "unseen_target_holdout.json"):
        print(f); run(file=f)

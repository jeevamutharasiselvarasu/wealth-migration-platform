"""Score a mapping function against renamed_source_cases.json.
Usage: python tests/generalization/score_baseline.py
The baseline is the current SchemaMapperAgent (fixed name catalog)."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agents.discovery.schema_mapper_agent import SchemaMapperAgent

def baseline(entity, rows):
    res = SchemaMapperAgent().map_source_to_canonical(entity, rows)
    return {l["source_field"]: l.get("canonical_field") for l in res["field_lineages"] if l.get("canonical_field")}

def score(mapper, cases_file="renamed_source_cases.json"):
    data = json.loads((Path(__file__).parent / cases_file).read_text())
    total = right = wrong = 0
    for c in data["cases"]:
        got = mapper(c["entity"], c["rows"])
        for src, want in c["truth"].items():
            total += 1
            g = got.get(src)
            if g == want: right += 1  # includes want=None (correctly left unmapped)
            elif g: wrong += 1; print(f"  WRONG {c['entity']}.{src}: got {g}, want {want}")
            elif want is None: pass
            else: print(f"  MISSED {c['entity']}.{src} (want {want})")
    print(f"correct {right}/{total}  wrong {wrong}  unmapped {total-right-wrong}")
    return right, total

if __name__ == "__main__":
    for f in ("renamed_source_cases.json", "renamed_source_holdout.json"):
        print(f); score(baseline, f)

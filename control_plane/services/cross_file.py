"""Cross-file analysis of uploaded source files: equivalent columns, relationship candidates, conflicts, duplicate files.

Deterministic and read-only. Findings are evidence for a reviewer (counts and column names, never record values);
nothing is joined or merged on the reviewer's behalf.
"""
import hashlib
import re
from typing import Any, Dict, List, Tuple

MAX_ROWS = 5000
MAX_COLUMNS = 60
OVERLAP_MIN = 0.6
CONTAIN_MIN = 0.9
UNIQUE_MIN = 0.95
MIN_SHARED = 2


def _norm_col(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())


def _norm_val(v: Any) -> str:
    return str(v).strip().upper() if v is not None else ""


def _profile(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    cols: Dict[str, Dict[str, Any]] = {}
    names = list(rows[0].keys())[:MAX_COLUMNS] if rows else []
    for name in names:
        vals = [_norm_val(r.get(name)) for r in rows]
        filled = [v for v in vals if v]
        distinct = set(filled)
        cols[name] = {"values": vals, "distinct": distinct, "filled": len(filled),
                      "unique_ratio": (len(distinct) / len(filled)) if filled else 0.0, "norm": _norm_col(name)}
    return cols


def analyze(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """files: [{"name": str, "entity": str, "rows": [dict]}]."""
    prof = []
    for f in files:
        rows = f["rows"][:MAX_ROWS]
        prof.append({"name": f["name"], "entity": f["entity"], "rows": rows, "cols": _profile(rows)})
    equivalent: List[Dict[str, Any]] = []
    relationships: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    duplicates: List[Dict[str, Any]] = []

    sigs: Dict[str, str] = {}
    for p in prof:
        if not p["rows"]:
            continue
        sig = hashlib.sha256(repr(sorted(tuple(sorted((_norm_col(k), _norm_val(v)) for k, v in r.items())) for r in p["rows"])).encode()).hexdigest()
        if sig in sigs:
            duplicates.append({"files": [sigs[sig], p["name"]], "detail": "Identical content (same columns and rows)."})
        else:
            sigs[sig] = p["name"]

    for i, a in enumerate(prof):
        for b in prof[i + 1:]:
            for ca, sa in a["cols"].items():
                for cb, sb in b["cols"].items():
                    if not sa["distinct"] or not sb["distinct"]:
                        continue
                    inter = sa["distinct"] & sb["distinct"]
                    same_name = sa["norm"] == sb["norm"]
                    overlap = len(inter) / min(len(sa["distinct"]), len(sb["distinct"]))
                    if not ((len(inter) >= MIN_SHARED and overlap >= OVERLAP_MIN) or (same_name and len(inter) >= 1)):
                        continue
                    equivalent.append({"a": f"{a['name']}.{ca}", "b": f"{b['name']}.{cb}", "same_name": same_name, "shared_values": len(inter),
                                       "overlap_pct": round(100 * overlap), "evidence": "same column name" if same_name else "value overlap"})
                    for (pk_f, pk_c, pk_s), (fk_f, fk_c, fk_s) in (((a, ca, sa), (b, cb, sb)), ((b, cb, sb), (a, ca, sa))):
                        contained = len(fk_s["distinct"] & pk_s["distinct"]) / len(fk_s["distinct"])
                        if pk_s["unique_ratio"] >= UNIQUE_MIN and contained >= CONTAIN_MIN and (pk_f["entity"] != fk_f["entity"] or fk_s["unique_ratio"] < UNIQUE_MIN):
                            relationships.append({"child": f"{fk_f['name']}.{fk_c}", "child_entity": fk_f["entity"], "parent": f"{pk_f['name']}.{pk_c}",
                                                  "parent_entity": pk_f["entity"], "contained_pct": round(100 * contained),
                                                  "kind": "cross-entity link" if pk_f["entity"] != fk_f["entity"] else "companion file of the same entity"})
                            break
                    # Conflicts: join on this key pair and compare columns that share a name.
                    if sa["unique_ratio"] >= UNIQUE_MIN or sb["unique_ratio"] >= UNIQUE_MIN:
                        idx = {}
                        for r, v in zip(b["rows"], sb["values"]):
                            if v and v not in idx:
                                idx[v] = r
                        shared_cols = [(x, y) for x, sx in a["cols"].items() for y, sy in b["cols"].items()
                                       if sx["norm"] == sy["norm"] and x != ca and y != cb]
                        for x, y in shared_cols:
                            compared = diff = 0
                            for r, v in zip(a["rows"], sa["values"]):
                                other = idx.get(v)
                                if not v or other is None:
                                    continue
                                va, vb = _norm_val(r.get(x)), _norm_val(other.get(y))
                                if va and vb:
                                    compared += 1
                                    diff += va != vb
                            if compared:
                                conflicts.append({"files": [a["name"], b["name"]], "key": f"{ca} = {cb}", "column": x if x == y else f"{x} / {y}",
                                                  "compared_rows": compared, "differing_rows": diff})
    conflicts = [c for c in conflicts if c["differing_rows"]]
    return {"files_analyzed": len(prof), "equivalent_columns": equivalent, "relationships": relationships, "conflicts": conflicts, "duplicate_files": duplicates}

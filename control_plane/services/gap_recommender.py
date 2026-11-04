"""Gap recommender: deterministic suggestions for problems a run found in the sample data itself.

When a run reports a problem that the rules cannot fix (blank values, codes with no crosswalk entry, keys with no parent row,
text in an unexpected shape), this module looks at the actual source files and proposes remedies a person can accept or
dismiss. It is plain Python over counts and column names: no LLM, no record values in the output, nothing is created,
approved or executed here. Accepting a proposal that carries a rule only creates a DRAFT; a Migration Analyst or Platform
Admin still has to approve it, and a proposal about the business or the source is only a recorded request.

Remedies it can propose, chosen from what the data supports:
  ALTERNATIVE_SOURCE  another route (through other files) that would supply the missing value, with measured coverage
  CROSSWALK_GAP       values with no entry in a VALUE_MAP, with the closest existing target value as a hint
  FORMAT_FIX          a date layout or split delimiter that fits the values that failed
  KEY_MISMATCH        lookup keys that match once case, spacing or leading zeros are ignored, or have no parent row
  ROWS_WITHOUT_KEY    rows that have no value for the entity key (padding or misaligned lines in the export)
  REFERENCE_GAP       rows of one entity that point at a parent record missing from the parent entity's file
  RELAX_REQUIREMENT   a required target field that is blank for most rows (a decision for the target owner)
  SOURCE_REQUEST      a ready-to-send note to the data provider naming the file, column and row count
"""
import difflib
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from control_plane.services import transformation_rules as tr
from control_plane.services import rule_suggestions as rs

MAX_ROWS = 5000
MAX_HOPS = 3
EDGE_MIN_SHARED = 3
EDGE_MIN_OVERLAP = 0.2
GOAL_CONTAINMENT = 0.8
MAX_PATHS = 40
RELAX_BLANK_RATIO = 0.5
SIMILAR_MIN = 0.8
STATUSES = ("OPEN", "ACCEPTED", "DISMISSED", "VENDOR_REQUESTED")
_DELIMS = [",", ";", "|", "/", "-", ":", " "]


# --------------------------------------------------------------------------------------------- tokens and value-path search
def _norm(token: Any) -> str:
    t = str(token).strip().upper()
    return (t.lstrip("0") or "0") if t.isdigit() else t


def _scalars(obj: Any, out: List[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _scalars(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _scalars(v, out)
    elif obj is not None and not isinstance(obj, bool):
        out.append(str(obj))


def tokens(cell: Any) -> Set[str]:
    """Normalised tokens of a cell; a list-valued cell (JSON, or comma/semicolon/pipe separated) yields several."""
    text = "" if cell is None else str(cell).strip()
    if not text:
        return set()
    raw: List[str] = []
    if text[:1] in "[{":
        try:
            _scalars(json.loads(text), raw)
        except ValueError:
            raw = []
    if not raw:
        raw = re.split(r"[,;|]", text)
    return {n for n in (_norm(p) for p in raw if str(p).strip()) if n}


class _Prep:
    """Per-file token views of the source tables (first MAX_ROWS rows)."""

    def __init__(self, tables: Dict[str, Dict[str, Any]]):
        self.tables = tables
        self.rows = {n: t["rows"][:MAX_ROWS] for n, t in tables.items()}
        self.cols = {n: list(rows[0].keys()) if rows else [] for n, rows in self.rows.items()}
        self._tok: Dict[Tuple[str, str], List[Set[str]]] = {}
        self._dom: Dict[Tuple[str, str], Set[str]] = {}
        self._idx: Dict[Tuple[str, str], Dict[str, List[int]]] = {}

    def col_tokens(self, f: str, c: str) -> List[Set[str]]:
        if (f, c) not in self._tok:
            self._tok[(f, c)] = [tokens(r.get(c)) for r in self.rows[f]]
        return self._tok[(f, c)]

    def domain(self, f: str, c: str) -> Set[str]:
        if (f, c) not in self._dom:
            self._dom[(f, c)] = set().union(*self.col_tokens(f, c)) if self.rows[f] else set()
        return self._dom[(f, c)]

    def index(self, f: str, c: str) -> Dict[str, List[int]]:
        if (f, c) not in self._idx:
            idx: Dict[str, List[int]] = defaultdict(list)
            for i, ts in enumerate(self.col_tokens(f, c)):
                for t in ts:
                    idx[t].append(i)
            self._idx[(f, c)] = idx
        return self._idx[(f, c)]


def _edges(prep: _Prep) -> List[Tuple[str, str, str, str]]:
    """Column pairs in different files whose values overlap strongly enough to join on (value containment, not names)."""
    out = []
    names = sorted(prep.rows)
    for i, fa in enumerate(names):
        for fb in names[i + 1:]:
            for ca in prep.cols[fa]:
                da = prep.domain(fa, ca)
                if len(da) < EDGE_MIN_SHARED:
                    continue
                for cb in prep.cols[fb]:
                    db = prep.domain(fb, cb)
                    shared = len(da & db)
                    if shared >= EDGE_MIN_SHARED and shared / min(len(da), len(db)) >= EDGE_MIN_OVERLAP:
                        out.append((fa, ca, fb, cb))
    return out


def _follow(prep: _Prep, start_rows: List[int], path: List[Tuple[str, str, str, str]]) -> Dict[int, Set[int]]:
    """For each start row, the rows reached in the last file of the path."""
    reached: Dict[int, Set[int]] = {}
    for s in start_rows:
        rows = {s}
        for fa, ca, fb, cb in path:
            toks: Set[str] = set()
            ta = prep.col_tokens(fa, ca)
            for i in rows:
                toks |= ta[i]
            nxt: Set[int] = set()
            idx = prep.index(fb, cb)
            for t in toks:
                nxt.update(idx.get(t, ()))
            rows = nxt
            if not rows:
                break
        reached[s] = rows
    return reached


def find_alternative_paths(tables: Dict[str, Dict[str, Any]], home: str, home_col: str, limit: int = 2) -> List[Dict[str, Any]]:
    """Other routes that would give the blank rows of home.home_col a value like the filled ones.

    A goal is a column in another file that contains the values the filled rows already use (the key the field points to).
    A route is a chain of up to MAX_HOPS joins that never uses home_col itself. Each route is scored by how many of the blank
    rows it reaches and how many of those reach more than one candidate."""
    prep = _Prep(tables)
    if home not in prep.rows or home_col not in prep.cols.get(home, []):
        return []
    col_toks = prep.col_tokens(home, home_col)
    blank = [i for i, ts in enumerate(col_toks) if not ts]
    domain = prep.domain(home, home_col)
    if not blank or not domain:
        return []
    goals: Dict[str, List[str]] = defaultdict(list)
    for f in prep.rows:
        if f == home:
            continue
        for c in prep.cols[f]:
            if len(domain & prep.domain(f, c)) / len(domain) >= GOAL_CONTAINMENT:
                goals[f].append(c)
    if not goals:
        return []
    adj: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    for fa, ca, fb, cb in _edges(prep):
        if (fa == home and ca == home_col) or (fb == home and cb == home_col):
            continue
        adj[fa].append((ca, fb, cb))
        adj[fb].append((cb, fa, ca))
    found: List[List[Tuple[str, str, str, str]]] = []

    def walk(f: str, path: List[Tuple[str, str, str, str]], seen: Set[str]) -> None:
        if len(found) >= MAX_PATHS:
            return
        if path and f in goals:
            found.append(list(path))
        if len(path) >= MAX_HOPS:
            return
        for ca, fb, cb in adj.get(f, ()):
            if fb in seen:
                continue
            walk(fb, path + [(f, ca, fb, cb)], seen | {fb})

    walk(home, [], {home})
    scored = []
    for path in found:
        last = path[-1][2]
        reached = _follow(prep, blank, path)
        for goal_col in goals[last]:
            gt = prep.col_tokens(last, goal_col)
            cand = {s: set().union(*[gt[j] for j in rows]) if rows else set() for s, rows in reached.items()}
            covered = sum(1 for v in cand.values() if v)
            if not covered:
                continue
            scored.append({"home": home, "column": home_col, "path": path, "goal": f"{last}.{goal_col}", "blank_rows": len(blank),
                           "covered_rows": covered, "ambiguous_rows": sum(1 for v in cand.values() if len(v) > 1), "hops": len(path)})
    scored.sort(key=lambda s: (-s["covered_rows"], s["ambiguous_rows"], s["hops"]))
    out, seen_goal = [], set()
    for s in scored:
        sig = (s["goal"], s["path"][0][2])
        if sig in seen_goal:
            continue
        seen_goal.add(sig)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _route_text(p: Dict[str, Any]) -> str:
    steps = [f"{p['home']}.{p['path'][0][1]}"]
    for fa, ca, fb, cb in p["path"]:
        steps.append(f"{fb}.{cb}")
    return " → ".join(steps) + f", then {p['goal']}"


# --------------------------------------------------------------------------------------------- per-problem analysis
def _category(reason: str) -> str:
    r = reason.lower()
    if "no matching row in the lookup file" in r:
        return "LOOKUP_MISS"
    if "unmapped value" in r:
        return "UNMAPPED"
    if "matches none of the declared formats" in r:
        return "DATE"
    if "after splitting" in r:
        return "SPLIT"
    if "blank" in r:
        return "BLANK"
    return "OTHER"


def _rec(migration_id: str, kind: str, entity: str, table: str, column: str, title: str, evidence: List[str], rows: int,
         basis: str, rule_draft: Optional[Dict[str, Any]] = None, vendor_note: str = "", detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    key = hashlib.sha1(f"{migration_id}|{kind}|{entity}|{column}|{title}".encode()).hexdigest()[:8].upper()
    return {"rec_id": "REC-" + key, "kind": kind, "entity": entity, "table": table, "column": column, "title": title, "evidence": evidence,
            "rows": rows, "basis": basis, "rule_draft": rule_draft, "vendor_note": vendor_note, "detail": detail or {}}


def _apply(rule: Optional[Dict[str, Any]], value: Any, row: Dict[str, Any], lookups: Dict[Any, Any]) -> Tuple[Any, Optional[str]]:
    if not rule:
        return value, None
    return tr.apply_rule(rule["rule_type"], rule["params"], value, row, lookups)


def recommend(migration_id: str, failures: List[Dict[str, Any]], mappings: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]],
              canonical_rows: Dict[str, List[Dict[str, Any]]], tables: Dict[str, Dict[str, Any]],
              lookups: Optional[Dict[Any, Any]] = None, hints: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """tables: {file name: {"entity": str, "rows": [record], "lineage": {canonical_field: source_column}}}."""
    lookups = lookups or {}
    recs: List[Dict[str, Any]] = []
    done: Set[Tuple[str, str]] = set()
    by_col = {(m["entity"], m["target_column"]): m for m in mappings}
    keys = (hints or {}).get("entity_keys", {})
    seen_titles: Set[Tuple[str, str]] = set()
    for f in failures:
        entity, column, table = f["entity"], f["column"], f["table"]
        if (entity, column) in done:
            continue
        done.add((entity, column))
        m = by_col.get((entity, column))
        if not m:
            continue
        group = [x for x in failures if x["entity"] == entity and x["column"] == column]
        keyf = keys.get(entity)
        # rows with no entity key are reported once as ROWS_WITHOUT_KEY; they are not counted again as gaps in every field
        ent_rows = [r for r in canonical_rows.get(entity, []) if not keyf or str(r.get(keyf) or "").strip()]
        total_rows = len(ent_rows)
        cf = m["canonical_field"]
        rule = rules.get(m["transformation"]) if m["transformation"] != "DIRECT_COPY" else None
        rtype = rule["rule_type"] if rule else "DIRECT_COPY"
        blank_in, bad_vals, blank_out = 0, [], 0
        for row in ent_rows:
            v = row.get(cf)
            out, warn = _apply(rule, v, row, lookups)
            if v is None or str(v).strip() == "":
                blank_in += 1
            elif warn or (out is None or str(out).strip() == ""):
                bad_vals.append(str(v).strip())
            if out is None or str(out).strip() == "":
                blank_out += 1
        cands = [(len(t["rows"]), n, t["lineage"][cf]) for n, t in sorted(tables.items()) if t.get("entity") == entity and cf in t.get("lineage", {})]
        home = (lambda c: (c[1], c[2]))(max(cands, key=lambda c: c[0])) if cands else None
        where = f"{entity}.{column}"
        affected = max(x["rows"] for x in group)  # the reasons often hit the same rows, so they are not added up
        keyless = len(canonical_rows.get(entity, [])) - len(ent_rows)
        # ---- value-level gaps
        if bad_vals and rtype == "VALUE_MAP":
            have = {k.upper() if rule["params"].get("case_insensitive", True) else k for k in rule["params"]["map"]}
            counts = Counter(v for v in bad_vals if (v.upper() if rule["params"].get("case_insensitive", True) else v) not in have)
            targets = sorted(set(rule["params"]["map"].values()))
            lines, extra = [], {}
            for val, n in counts.most_common(12):
                near = difflib.get_close_matches(val, targets, n=1, cutoff=SIMILAR_MIN)
                lines.append(f"'{val}': {n} row(s), no entry; closest existing target value: {near[0] if near else 'none'}")
                if near:
                    extra[val.upper() if rule['params'].get('case_insensitive', True) else val] = near[0]
            if counts:
                draft = None
                if extra:
                    merged = {**rule["params"]["map"], **extra}
                    draft = {"rule_id": rule_id_of(m["transformation"]), "rule_type": "VALUE_MAP", "params": {**rule["params"], "map": merged},
                             "description": f"Proposed by gap recommender for {where}: adds {len(extra)} value(s) matched by spelling similarity. Confirm each pair."}
                recs.append(_rec(migration_id, "CROSSWALK_GAP", entity, table, column,
                                 f"{len(counts)} distinct source value(s) have no crosswalk entry for {where}", lines, sum(counts.values()),
                                 "Coded values are shown because this column is a lookup; the closest value is a spelling match only and needs a business decision.", draft,
                                 detail={"values_without_entry": len(counts), "suggested_pairs": len(extra)}))
        if bad_vals and rtype == "DATE_FORMAT":
            fits = rs.date_formats_for(bad_vals)
            partial = [fm for fm in tr.DATE_FORMATS if fm not in rule["params"]["input_formats"] and sum(rs._parses(fm, v) for v in bad_vals) / len(bad_vals) >= 0.9]
            cand = fits or partial
            if cand:
                draft = {"rule_id": rule_id_of(m["transformation"]), "rule_type": "DATE_FORMAT",
                         "params": {"input_formats": list(rule["params"]["input_formats"]) + [c for c in cand if c not in rule["params"]["input_formats"]]},
                         "description": f"Proposed by gap recommender for {where}: also read dates written as {', '.join(cand)}."}
                recs.append(_rec(migration_id, "FORMAT_FIX", entity, table, column, f"Dates that failed for {where} fit {', '.join(cand)}",
                                 [f"{len(bad_vals)} row(s) held dates the rule could not read; the layout(s) above parse at least 90% of them."], len(bad_vals),
                                 "Layout detected from the failing values; confirm it is not ambiguous (day/month) before approving.", draft))
        if bad_vals and rtype == "SPLIT_PART":
            delim = rule["params"]["delimiter"]
            counts = {d: sum(1 for v in bad_vals if d in v) for d in _DELIMS if d != delim}
            best = max(counts, key=counts.get) if counts else None
            lacking = sum(1 for v in bad_vals if delim not in v)
            ev = [f"{lacking} of {len(bad_vals)} failing value(s) do not contain the delimiter used by the rule."]
            if best and counts[best] / len(bad_vals) >= 0.7:
                ev.append(f"{counts[best]} of them contain '{best}' instead.")
            recs.append(_rec(migration_id, "FORMAT_FIX", entity, table, column, f"{where}: values are not in the shape the split rule expects", ev, len(bad_vals),
                             "Shape only; the business decides whether a second delimiter rule is right or the source should be corrected.",
                             detail={"alternative_delimiter": best if best and counts[best] / len(bad_vals) >= 0.7 else None}))
        if bad_vals and rtype == "LOOKUP":
            table_ = lookups.get(tr.lookup_key(rule["params"])) or {}
            parent = {str(k).lstrip("0").upper() for k in table_}
            fixed = sum(1 for v in bad_vals if v.lstrip("0").upper() in parent)
            orphans = len(bad_vals) - fixed
            ev = []
            if fixed:
                ev.append(f"{fixed} key(s) would match the lookup file if leading zeros, spacing or case were ignored.")
            if orphans:
                ev.append(f"{orphans} key(s) have no row in {rule['params']['parent_file']} at all.")
            if ev:
                recs.append(_rec(migration_id, "KEY_MISMATCH", entity, table, column, f"{where}: lookup keys that do not match {rule['params']['parent_file']}", ev, len(bad_vals),
                                 "Keys compared by column; no values are shown.",
                                 vendor_note=(f"{rule['params']['parent_file']} has no row for {orphans} key(s) referenced by {where}. Please supply the missing rows." if orphans else "")))
        # ---- blank values
        if blank_in and home:
            hf, hc = home
            paths = find_alternative_paths(tables, hf, hc)
            for p in paths:
                share = f"{p['covered_rows']} of {p['blank_rows']} blank row(s) in {hf}.{hc}"
                amb = f"; {p['ambiguous_rows']} would have more than one candidate" if p["ambiguous_rows"] else ""
                recs.append(_rec(migration_id, "ALTERNATIVE_SOURCE", entity, table, column,
                                 f"{where} can be derived another way: {_route_text(p)}", [f"Would supply a value for {share}{amb}."], p["covered_rows"],
                                 "The files are joined on overlapping values, not on a stated business rule. The business must confirm this route is valid.",
                                 detail={"route": [list(s) for s in p["path"]], "goal": p["goal"], "blank_rows": p["blank_rows"], "covered_rows": p["covered_rows"],
                                         "ambiguous_rows": p["ambiguous_rows"]},
                                 vendor_note=""))
        if blank_in and total_rows:
            ratio = blank_in / total_rows
            ev = [f"{blank_in} of {total_rows} {entity} row(s) have no value for {cf or column} in the sample."]
            if m.get("required") and ratio >= RELAX_BLANK_RATIO:
                recs.append(_rec(migration_id, "RELAX_REQUIREMENT", entity, table, column, f"{where} is required but blank for {round(100 * ratio)}% of rows",
                                 ev + ["A required target field with most rows blank cannot load those records."], blank_in,
                                 "A decision for the target owner: keep it required (rows are rejected) or make it optional."))
            src = f"{home[0]}.{home[1]}" if home else cf
            recs.append(_rec(migration_id, "SOURCE_REQUEST", entity, table, column, f"Ask the data provider for {src}", ev, blank_in,
                             "If the full export has the same gap, only the provider can fix it.",
                             vendor_note=f"{src} is blank for {blank_in} of {total_rows} rows in the migration sample. {where} cannot be populated for them. "
                                         f"Please confirm whether the full export has values and send a corrected file."))
        elif not (blank_in or bad_vals) and affected and affected > keyless:  # rows without the entity key are reported once as ROWS_WITHOUT_KEY
            recs.append(_rec(migration_id, "SOURCE_REQUEST", entity, table, column, f"{where}: {affected} row(s) failed with no pattern the recommender could analyse",
                             [f["reason"] for f in group][:3], affected, "No remedy was derived from the data; review the problem manually."))
    uniq, seen = [], set()
    for r in recs:
        if (r["kind"], r["title"]) in seen:
            continue
        seen.add((r["kind"], r["title"]))
        uniq.append(r)
    recs = uniq
    recs.extend(_structural(migration_id, failures, canonical_rows, keys))
    return recs


def _structural(migration_id: str, failures: List[Dict[str, Any]], canonical_rows: Dict[str, List[Dict[str, Any]]], keys: Dict[str, str]) -> List[Dict[str, Any]]:
    """Gaps in the files themselves that explain many row problems at once: missing keys and dangling references."""
    out: List[Dict[str, Any]] = []
    hit = {f["entity"] for f in failures}
    for entity in sorted(hit):
        key = keys.get(entity)
        rows = canonical_rows.get(entity, [])
        if not key or not rows:
            continue
        blank = sum(1 for r in rows if str(r.get(key) or "").strip() == "")
        if blank:
            note = (f"{blank} of {len(rows)} {entity} row(s) in the sample have no {key}. They look like padding or misaligned lines rather than records. "
                    f"Please confirm the export is complete and resend it without them.")
            out.append(_rec(migration_id, "ROWS_WITHOUT_KEY", entity, "", key, f"{blank} of {len(rows)} {entity} rows have no {key}",
                            [f"These rows cannot be identified or joined, so they are skipped or fail on every required field."], blank,
                            "Counted on the mapped key field; check whether the source export is truncated or misaligned.", vendor_note=note))
    for ref_entity in sorted(hit):
        for parent, pkey in sorted(keys.items()):
            if parent == ref_entity or not pkey:
                continue
            ref_rows = [r for r in canonical_rows.get(ref_entity, []) if str(r.get(pkey) or "").strip()]
            parent_vals = {_norm(r.get(pkey)) for r in canonical_rows.get(parent, []) if str(r.get(pkey) or "").strip()}
            if not ref_rows or not parent_vals:
                continue
            dangling = sum(1 for r in ref_rows if _norm(r.get(pkey)) not in parent_vals)
            if dangling:
                note = (f"{dangling} of {len(ref_rows)} {ref_entity} row(s) refer to a {parent} ({pkey}) that is not in the {parent} file. "
                        f"Please send the missing {parent} records or confirm these {ref_entity} rows should be excluded.")
                out.append(_rec(migration_id, "REFERENCE_GAP", ref_entity, "", pkey, f"{dangling} of {len(ref_rows)} {ref_entity} rows point at a {parent} that is not in the file",
                                [f"{pkey} is the {parent} key; those values have no matching {parent} record in the sample."], dangling,
                                "Found by comparing the same canonical key across entities.", vendor_note=note))
    return out


def rule_id_of(expression: str) -> str:
    """'RULE:ID@3' -> 'ID'."""
    return expression.split(":", 1)[-1].split("@", 1)[0]


# --------------------------------------------------------------------------------------------- storage and decisions
_DDL = """CREATE TABLE IF NOT EXISTS gap_recommendations (
    rec_id TEXT NOT NULL, migration_id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL, entity TEXT NOT NULL,
    target_column TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', reason TEXT NOT NULL DEFAULT '',
    reviewer TEXT, reviewed_utc TEXT, created_draft TEXT, PRIMARY KEY (rec_id, migration_id))"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def save(conn: sqlite3.Connection, migration_id: str, run_id: str, recs: List[Dict[str, Any]]) -> int:
    """Store the latest recommendations; an earlier decision on the same recommendation is kept."""
    ensure_table(conn)
    for r in recs:
        conn.execute("INSERT INTO gap_recommendations (rec_id, migration_id, run_id, kind, entity, target_column, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT (rec_id, migration_id) DO UPDATE SET run_id = excluded.run_id, payload_json = excluded.payload_json",
                     (r["rec_id"], migration_id, run_id, r["kind"], r["entity"], r["column"], json.dumps(r)))
    return len(recs)


def list_for_run(conn: sqlite3.Connection, migration_id: str, run_id: str) -> List[Dict[str, Any]]:
    ensure_table(conn)
    out = []
    for row in conn.execute("SELECT * FROM gap_recommendations WHERE migration_id = ? AND run_id = ? ORDER BY kind, entity, target_column", (migration_id, run_id)).fetchall():
        d = json.loads(row["payload_json"])
        d.update({"status": row["status"], "decision_reason": row["reason"], "reviewer": row["reviewer"], "reviewed_utc": row["reviewed_utc"], "created_draft": row["created_draft"]})
        out.append(d)
    return out


def decide(conn: sqlite3.Connection, migration_id: str, rec_id: str, decision: str, reason: str, reviewer: str) -> Dict[str, Any]:
    """ACCEPTED (a rule draft is created when the proposal carries one), DISMISSED (reason required) or VENDOR_REQUESTED."""
    ensure_table(conn)
    decision = str(decision).strip().upper()
    if decision not in STATUSES[1:]:
        raise ValueError("Decision must be ACCEPTED, DISMISSED or VENDOR_REQUESTED.")
    row = conn.execute("SELECT * FROM gap_recommendations WHERE rec_id = ? AND migration_id = ?", (rec_id, migration_id)).fetchone()
    if not row:
        raise ValueError("Recommendation not found.")
    rec = json.loads(row["payload_json"])
    if decision == "DISMISSED" and not str(reason).strip():
        raise ValueError("A reason is required to dismiss a recommendation.")
    draft_ref = None
    if decision == "ACCEPTED" and rec.get("rule_draft"):
        d = rec["rule_draft"]
        made = tr.create_rule_version(conn, d["rule_id"], d["rule_type"], d["description"][:900], d["params"], reviewer)
        draft_ref = made["expression"]
    if decision == "VENDOR_REQUESTED" and not rec.get("vendor_note"):
        raise ValueError("This recommendation has no data-provider request to send; accept or dismiss it instead.")
    conn.execute("UPDATE gap_recommendations SET status = ?, reason = ?, reviewer = ?, reviewed_utc = ?, created_draft = ? WHERE rec_id = ? AND migration_id = ?",
                 (decision, str(reason).strip()[:500], reviewer, datetime.now(timezone.utc).isoformat(), draft_ref, rec_id, migration_id))
    return {"rec": rec, "status": decision, "draft": draft_ref}

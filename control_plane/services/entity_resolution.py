"""Entity resolution (canonical model phase 2): the same real-world record in several source rows becomes one canonical record.

Match rules are approved like mappings: a rule is a list of canonical fields that, all populated and equal after normalising, mean
two rows are the same (for a client: the same tax id; or the same first name, last name and date of birth). Rows linked by any
approved rule form one cluster (transitively). Survivorship: the most complete row of the cluster survives (ties: the lowest key),
and each of its blank fields takes the first populated value of the others, most complete first. A crosswalk keeps every source key
with its surviving key, so references from other entities (an account role's client) are pointed at the survivor.

AccountRole needs a source that says who holds each account. When a source has none (CoreCap), the analyst can approve a
derivation instead: the head of each household (household order 1) owns every account of that household. That is a business
assumption and is shown as one. Only keys and counts are ever shown, never the matched values.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

SURVIVORSHIP = "MOST_COMPLETE"
def suggested_rules() -> Dict[str, List[List[str]]]:
    """Field sets worth proposing as match rules per entity (platform_knowledge/match_rules.yaml); none are built in."""
    import yaml
    from control_plane.paths import platform_knowledge_dir
    try:
        return yaml.safe_load((platform_knowledge_dir() / "match_rules.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def references() -> Dict[str, Dict[str, tuple]]:
    """field -> (entity, key field) it points at, per entity, from the firm configuration; resolution repoints these to the survivor."""
    from control_plane.services.firm_config import FirmConfigError, reference_fields
    try:
        return reference_fields()
    except FirmConfigError:
        return {}


ROLE_DERIVATION = "HOUSEHOLD_HEAD_OWNS"
ROLE_DERIVATION_TEXT = "The head of each household (household order 1) owns every account of that household (role OWNER)."
STRIP = " -.,/'()[]"  # removed before comparing (the generated SQL removes the same characters)
PLACEHOLDERS = ("REDACTED", "NA", "NULL", "NONE", "UNKNOWN")  # masked or missing values never match anything
_DDL = (
    """CREATE TABLE IF NOT EXISTS entity_match_rules (
        migration_id TEXT NOT NULL, entity_name TEXT NOT NULL, rule_id TEXT NOT NULL, fields_json TEXT NOT NULL, status TEXT NOT NULL,
        proposed_utc TEXT NOT NULL, decided_by TEXT, decided_utc TEXT, PRIMARY KEY (migration_id, entity_name, rule_id)
    )""",
    """CREATE TABLE IF NOT EXISTS entity_derivations (
        migration_id TEXT NOT NULL, entity_name TEXT NOT NULL, method TEXT NOT NULL, status TEXT NOT NULL, decided_by TEXT, decided_utc TEXT,
        PRIMARY KEY (migration_id, entity_name)
    )""",
)


def ensure_tables(conn: sqlite3.Connection) -> None:
    for ddl in _DDL:
        conn.execute(ddl)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rule_id(fields: List[str]) -> str:
    return "MATCH_" + "_".join(fields)


def suggest(conn: sqlite3.Connection, migration_id: str, entity: str, available: List[str]) -> int:
    """Propose the suggested match rules whose fields this migration fills. Existing rules keep their decision. Returns rules added."""
    ensure_tables(conn)
    added = 0
    for fields in suggested_rules().get(entity, []):
        if not all(f in available for f in fields):
            continue
        cur = conn.execute("INSERT INTO entity_match_rules (migration_id, entity_name, rule_id, fields_json, status, proposed_utc) "
                           "VALUES (?, ?, ?, ?, 'PROPOSED', ?) ON CONFLICT DO NOTHING", (migration_id, entity, _rule_id(fields), json.dumps(fields), _now()))
        added += cur.rowcount
    return added


def rules(conn: sqlite3.Connection, migration_id: str, entity: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    out = []
    for r in conn.execute("SELECT * FROM entity_match_rules WHERE migration_id = ? ORDER BY entity_name, rule_id", (migration_id,)).fetchall():
        if (entity and r["entity_name"] != entity) or (status and r["status"] != status):
            continue
        out.append({**dict(r), "fields": json.loads(r["fields_json"])})
    return out


def approved_rules(conn: sqlite3.Connection, migration_id: str) -> Dict[str, List[List[str]]]:
    out: Dict[str, List[List[str]]] = {}
    for r in rules(conn, migration_id, status="APPROVED"):
        out.setdefault(r["entity_name"], []).append(r["fields"])
    return out


def decide(conn: sqlite3.Connection, migration_id: str, entity: str, rule_id: str, decision: str, actor: str) -> str:
    ensure_tables(conn)
    status = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "REOPEN": "PROPOSED"}.get(str(decision or "").upper())
    if not status:
        raise ValueError("Unknown decision.")
    cur = conn.execute("UPDATE entity_match_rules SET status = ?, decided_by = ?, decided_utc = ? WHERE migration_id = ? AND entity_name = ? AND rule_id = ?",
                       (status, None if status == "PROPOSED" else actor, None if status == "PROPOSED" else _now(), migration_id, entity, rule_id))
    if not cur.rowcount:
        raise ValueError("That match rule is not proposed for this migration.")
    return status


def derivation(conn: sqlite3.Connection, migration_id: str, entity: str = "AccountRole") -> Optional[Dict[str, Any]]:
    ensure_tables(conn)
    r = conn.execute("SELECT * FROM entity_derivations WHERE migration_id = ? AND entity_name = ?", (migration_id, entity)).fetchone()
    return dict(r) if r else None


def decide_derivation(conn: sqlite3.Connection, migration_id: str, decision: str, actor: str, entity: str = "AccountRole") -> str:
    ensure_tables(conn)
    status = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}.get(str(decision or "").upper())
    if not status:
        raise ValueError("Unknown decision.")
    conn.execute("DELETE FROM entity_derivations WHERE migration_id = ? AND entity_name = ?", (migration_id, entity))
    conn.execute("INSERT INTO entity_derivations (migration_id, entity_name, method, status, decided_by, decided_utc) VALUES (?, ?, ?, ?, ?, ?)",
                 (migration_id, entity, ROLE_DERIVATION, status, actor, _now()))
    return status


def pending(conn: sqlite3.Connection, migration_id: str) -> int:
    return len(rules(conn, migration_id, status="PROPOSED"))


# ---- in memory (previews, mapping validation) ----

def match_value(row: Dict[str, Any], fields: List[str]) -> Optional[str]:
    parts = []
    for f in fields:
        v = "".join(ch for ch in str(row.get(f) if row.get(f) is not None else "").upper() if not ch.isspace() and ch not in STRIP)
        if not v or v in PLACEHOLDERS:
            return None
        parts.append(v)
    return "|".join(parts)


def _score(row: Dict[str, Any]) -> int:
    return sum(1 for v in row.values() if str(v if v is not None else "").strip())


def resolve(rows: List[Dict[str, Any]], key: str, entity_rules: List[List[str]]) -> Tuple[List[Dict[str, Any]], Dict[str, str], Dict[str, Any]]:
    """(surviving rows, {source key: surviving key}, stats). Rows without a key are kept as they are."""
    keyed = [r for r in rows if str(r.get(key) or "").strip()]
    loose = [r for r in rows if not str(r.get(key) or "").strip()]
    parent = {str(r[key]): str(r[key]) for r in keyed}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for fields in entity_rules:
        first: Dict[str, str] = {}
        for r in keyed:
            m = match_value(r, fields)
            if m is None:
                continue
            k = str(r[key])
            if m in first:
                a, b = find(first[m]), find(k)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                first[m] = k
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for r in keyed:
        clusters.setdefault(find(str(r[key])), []).append(r)
    out, xref, examples = [], {}, []
    for members in clusters.values():
        members = sorted(members, key=lambda r: (-_score(r), str(r[key])))
        survivor = dict(members[0])
        for other in members[1:]:
            for f, v in other.items():
                if not str(survivor.get(f) if survivor.get(f) is not None else "").strip() and str(v if v is not None else "").strip():
                    survivor[f] = v
        for m in members:
            xref[str(m[key])] = str(survivor[key])
        out.append(survivor)
        if len(members) > 1 and len(examples) < 10:
            examples.append([str(m[key]) for m in members])
    merged = len(keyed) - len(clusters)
    return out + loose, xref, {"rows": len(rows), "records": len(clusters) + len(loose), "merged": merged,
                               "clusters": sum(1 for m in clusters.values() if len(m) > 1), "examples": examples}


def repoint(rows: List[Dict[str, Any]], field: str, xref: Dict[str, str]) -> int:
    n = 0
    for r in rows:
        v = str(r.get(field) or "")
        if v in xref and xref[v] != v:
            r[field] = xref[v]
            n += 1
    return n


def derive_account_roles(accounts: List[Dict[str, Any]], clients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """HOUSEHOLD_HEAD_OWNS: one OWNER row per account, held by the client with household order 1 in the account's household."""
    head = {}
    for c in clients:
        if str(c.get("HH_ORDER") or "").strip() in ("1", "1.0") and c.get("HH_ID") and c.get("CLIENT_ID"):
            head.setdefault(str(c["HH_ID"]), str(c["CLIENT_ID"]))
    return [{"ACCT_NUM": a["ACCT_NUM"], "CLIENT_ID": head[str(a["HH_ID"])], "ROLE": "OWNER", "ROLE_ORDER": "1"}
            for a in accounts if a.get("ACCT_NUM") and str(a.get("HH_ID") or "") in head]


# ---- SQL (the generated canonical procedures) ----

def sql_match(fields: List[str], alias: str) -> str:
    """T-SQL for match_value: upper case, STRIP characters and spaces removed; NULL when any field is blank."""
    parts = []
    for f in fields:
        col = f"{alias}.[{f}]"
        norm = f"REPLACE(TRANSLATE(UPPER(LTRIM(RTRIM({col}))), N'{STRIP.replace(chr(39), chr(39) * 2)}', N'{' ' * len(STRIP)}'), N' ', N'')"
        parts.append(f"(CASE WHEN {norm} IN (N'', {', '.join(chr(78) + chr(39) + x + chr(39) for x in PLACEHOLDERS)}) THEN NULL ELSE {norm} END)")
    return " + N'|' + ".join(parts) if len(parts) > 1 else parts[0]


def sql_resolve(schema: str, table: str, xref: str, key: str, fields: List[str], entity_rules: List[List[str]]) -> List[str]:
    """Statements for a canonical procedure: cluster the rows (approved rules, transitively), keep the crosswalk, fill the survivor's
    blank fields from the others (most complete first), then remove the non-surviving rows."""
    t, x, k = f"[{schema}].[{table}]", f"[{schema}].[{xref}]", f"[{key}]"
    others = [f for f in fields if f != key]
    score = " + ".join(f"CASE WHEN NULLIF(LTRIM(RTRIM(c.[{f}])), N'') IS NULL THEN 0 ELSE 1 END" for f in fields) or "0"
    matches = ", ".join(f"{sql_match(r, 'c')} AS [m{i}]" for i, r in enumerate(entity_rules))
    out = [
        "IF OBJECT_ID(N'tempdb..#er') IS NOT NULL DROP TABLE #er;",
        f"SELECT CAST(c.{k} AS nvarchar(400)) AS [k], CAST(c.{k} AS nvarchar(400)) AS [g], {matches}, {score} AS [score] INTO #er FROM {t} AS c WHERE c.{k} IS NOT NULL;",
        "DECLARE @er_changed int = 1;",
        "WHILE @er_changed > 0\nBEGIN\n    SET @er_changed = 0;\n" + "".join(
            f"    UPDATE e SET e.[g] = x.[g] FROM #er AS e JOIN (SELECT [m{i}], MIN([g]) AS [g] FROM #er WHERE [m{i}] IS NOT NULL GROUP BY [m{i}]) AS x "
            f"ON x.[m{i}] = e.[m{i}] WHERE x.[g] < e.[g];\n    SET @er_changed = @er_changed + @@ROWCOUNT;\n" for i in range(len(entity_rules)))
        + "    UPDATE e SET e.[g] = x.[g] FROM #er AS e JOIN (SELECT [k], [g] FROM #er) AS x ON x.[k] = e.[g] WHERE x.[g] < e.[g];\n"
          "    SET @er_changed = @er_changed + @@ROWCOUNT;\nEND;",
        "WITH s AS (SELECT [g], [k], ROW_NUMBER() OVER (PARTITION BY [g] ORDER BY [score] DESC, [k]) AS [rn] FROM #er)\n"
        "UPDATE e SET e.[g] = s.[k] FROM #er AS e JOIN s ON s.[g] = e.[g] AND s.[rn] = 1;",
        f"TRUNCATE TABLE {x};",
        f"INSERT INTO {x} ([source_key], [survivor_key], [xodus_batch_id], [xodus_run_id]) SELECT [k], [g], @BatchId, @RunId FROM #er;",
    ]
    for f in others:
        out.append(f"UPDATE c SET c.[{f}] = (SELECT TOP (1) o.[{f}] FROM {t} AS o JOIN #er AS eo ON eo.[k] = o.{k} WHERE eo.[g] = ec.[k] "
                   f"AND NULLIF(LTRIM(RTRIM(o.[{f}])), N'') IS NOT NULL ORDER BY eo.[score] DESC, eo.[k]) FROM {t} AS c JOIN #er AS ec ON ec.[k] = c.{k} "
                   f"AND ec.[g] = ec.[k] WHERE NULLIF(LTRIM(RTRIM(c.[{f}])), N'') IS NULL;")
    out.append(f"DELETE c FROM {t} AS c JOIN #er AS e ON e.[k] = c.{k} WHERE e.[g] <> e.[k];")
    out.append("DROP TABLE #er;")
    return out

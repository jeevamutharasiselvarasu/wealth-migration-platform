"""Validation rules written in plain English or SQL, checked, dry-run, approved by a second person, then compiled into the package.

A rule is one query that counts the rows that BREAK it. It runs inside the pipeline after the wave of its entity is built
(`usp_validate_<slug>`), writes a finding like any built-in check (counts and failing keys only, never other values), and a
BLOCKING rule that fails stops the run before the next wave.

English is not interpreted by a model: it is matched against a fixed set of sentence patterns (PATTERNS below) and compiled to SQL
deterministically. Anything the patterns cannot say is written as SQL. Either way the SQL passes the same static check before it can
be saved: one read-only SELECT that returns one integer, reading only the canonical and mirror tables named through placeholders
(`{canon:Entity}`, `{mirror:LiveTable}`), with no DML, DDL, EXEC, comments, variables, CTEs, set operators, SELECT INTO or functions
outside an allow-list. A rule is saved as DRAFT, must pass a dry run (a count on the latest batch) before it is submitted, and is
approved or rejected by someone other than its author. Approved rules are compiled into the package, so each is reviewed twice (here
and in the pull request). An approved rule is never edited: a change is a new version, and approving it retires the old one.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

LAYERS = ("canonical", "mirror")
SEVERITIES = ("BLOCKING", "WARN")
STATUSES = ("DRAFT", "PROPOSED", "APPROVED", "REJECTED", "RETIRED", "SUPERSEDED")
MAX_SQL = 4000
MAX_KEYS = 50
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.,'()/-]{2,79}$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PLACEHOLDER = re.compile(r"\{(canon|mirror):([A-Za-z0-9_]+)\}")
NUMBER = r"-?\d+(?:\.\d+)?"

_DDL = """
CREATE TABLE IF NOT EXISTS validation_rules (
    rule_id TEXT NOT NULL, version INTEGER NOT NULL, migration_id TEXT NOT NULL, entity TEXT NOT NULL, layer TEXT NOT NULL, name TEXT NOT NULL,
    severity TEXT NOT NULL, english TEXT, sql_text TEXT NOT NULL, keys_sql TEXT, status TEXT NOT NULL, problems_json TEXT,
    dry_run_count INTEGER, dry_run_utc TEXT, dry_run_by TEXT, dry_run_error TEXT,
    created_by TEXT NOT NULL, created_utc TEXT NOT NULL, submitted_by TEXT, decided_by TEXT, decided_utc TEXT, decision_comments TEXT,
    PRIMARY KEY (rule_id, version)
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------------------------------------------- English -> SQL
PATTERNS = [
    ("filled", "every <field> must be filled   (also: <field> must not be blank)"),
    ("filled_entity", "every <Entity> must have <field> filled"),
    ("one_of", "<field> must be one of <a>, <b>, <c>"),
    ("unique", "<field> must be unique"),
    ("between", "<field> must be between <number> and <number>"),
    ("at_least", "<field> must be at least <number>"),
    ("at_most", "<field> must be at most <number>"),
    ("date", "<field> must be a date"),
    ("max_length", "<field> must not be longer than <n> characters"),
    ("conditional", "every <Entity> with <field> = <value> must have <field2> filled"),
    ("matching", "every <Entity> must have a matching <OtherEntity> on <field>   (or: on <field> = <other field>)"),
]
_F = r"[A-Za-z_][A-Za-z0-9_]*"
_V = r"""'[^']*'|"[^"]*"|[^\s,'"]+"""
_RULES = [
    ("filled", re.compile(rf"^every\s+(?P<f>{_F})\s+must\s+be\s+filled$", re.I)),
    ("filled", re.compile(rf"^(?P<f>{_F})\s+must\s+(?:not\s+be\s+blank|be\s+filled|be\s+populated)$", re.I)),
    ("filled_entity", re.compile(rf"^every\s+(?P<e>{_F})\s+must\s+have\s+(?P<f>{_F})\s+filled$", re.I)),
    ("one_of", re.compile(rf"^(?P<f>{_F})\s+must\s+be\s+one\s+of\s+(?P<list>.+)$", re.I)),
    ("unique", re.compile(rf"^(?P<f>{_F})\s+must\s+be\s+unique$", re.I)),
    ("between", re.compile(rf"^(?P<f>{_F})\s+must\s+be\s+between\s+(?P<a>{NUMBER})\s+and\s+(?P<b>{NUMBER})$", re.I)),
    ("at_least", re.compile(rf"^(?P<f>{_F})\s+must\s+be\s+at\s+least\s+(?P<n>{NUMBER})$", re.I)),
    ("at_most", re.compile(rf"^(?P<f>{_F})\s+must\s+be\s+at\s+most\s+(?P<n>{NUMBER})$", re.I)),
    ("date", re.compile(rf"^(?P<f>{_F})\s+must\s+be\s+a\s+(?:valid\s+)?date$", re.I)),
    ("max_length", re.compile(rf"^(?P<f>{_F})\s+must\s+not\s+be\s+longer\s+than\s+(?P<n>\d{{1,4}})\s+characters?$", re.I)),
    ("conditional", re.compile(rf"^every\s+(?P<e>{_F})\s+with\s+(?P<f>{_F})\s*=\s*(?P<v>{_V})\s+must\s+have\s+(?P<g>{_F})\s+filled$", re.I)),
    ("matching", re.compile(rf"^every\s+(?P<e>{_F})\s+must\s+have\s+a\s+matching\s+(?P<o>{_F})\s+on\s+(?P<f>{_F})(?:\s*=\s*(?P<g>{_F}))?$", re.I)),
]


class RuleError(ValueError):
    pass


def _lit(value: str) -> str:
    if re.search(r"[\x00-\x1f]", value):
        raise RuleError("A value in the rule contains a control character.")
    return "N'" + value.replace("'", "''") + "'"


def _strip_quotes(v: str) -> str:
    v = v.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v


def _split_list(text: str) -> List[str]:
    items = [_strip_quotes(m.group(0)) for m in re.finditer(r"'[^']*'|\"[^\"]*\"|[^,]+", text) if m.group(0).strip(", ")]
    items = [i for i in items if i != ""]
    if not items or len(items) > 200:
        raise RuleError("List between 1 and 200 values after 'one of'.")
    return items


def _field(name: str, entity: str, fields: Dict[str, List[str]]) -> str:
    known = {f.lower(): f for f in fields.get(entity, [])}
    if name.lower() not in known:
        raise RuleError(f"{entity} has no field '{name}'. Fields: {', '.join(sorted(fields.get(entity, []))[:25])}.")
    return known[name.lower()]


def _entity_name(name: str, fields: Dict[str, List[str]]) -> str:
    known = {e.lower(): e for e in fields}
    if name.lower() not in known:
        raise RuleError(f"'{name}' is not an entity. Entities: {', '.join(sorted(fields))}.")
    return known[name.lower()]


def compile_english(text: str, entity: str, fields: Dict[str, List[str]], key: Optional[str] = None) -> Dict[str, Any]:
    """Compile one sentence to {"sql", "keys_sql", "pattern"} (canonical layer). Raises RuleError that lists the supported sentences."""
    sentence = re.sub(r"\s+", " ", str(text or "").strip()).rstrip(".")
    supported = "Supported sentences: " + " | ".join(p for _n, p in PATTERNS) + ". For anything else write SQL."
    if not sentence:
        raise RuleError("Write the rule as a sentence. " + supported)
    if entity not in fields:
        raise RuleError(f"'{entity}' is not an entity of this migration.")
    for name, rx in _RULES:
        m = rx.match(sentence)
        if not m:
            continue
        g = m.groupdict()
        if g.get("e") and g["e"].lower() != entity.lower() and name != "matching":
            raise RuleError(f"The rule is for {entity} but the sentence names {g['e']}.")
        if name == "matching" and g["e"].lower() != entity.lower():
            raise RuleError(f"The rule is for {entity} but the sentence names {g['e']}.")
        f = _field(g["f"], entity, fields)
        col = f"c.[{f}]"
        txt = f"LTRIM(RTRIM(CAST({col} AS nvarchar(4000))))"
        blank = f"({col} IS NULL OR {txt} = '')"
        filled = f"({col} IS NOT NULL AND {txt} <> '')"
        num = f"TRY_CONVERT(decimal(38, 10), {col})"
        if name in ("filled", "filled_entity"):
            cond = blank
        elif name == "one_of":
            cond = f"{filled} AND {txt} NOT IN ({', '.join(_lit(v) for v in _split_list(g['list']))})"
        elif name == "unique":
            return {"pattern": name, "sql": f"SELECT COUNT(*) - COUNT(DISTINCT {col}) FROM {{canon:{entity}}} c WHERE {col} IS NOT NULL",
                    "keys_sql": f"SELECT TOP ({MAX_KEYS}) CAST({col} AS nvarchar(400)) FROM {{canon:{entity}}} c WHERE {col} IS NOT NULL GROUP BY {col} HAVING COUNT(*) > 1"}
        elif name == "between":
            lo, hi = (g["a"], g["b"]) if float(g["a"]) <= float(g["b"]) else (g["b"], g["a"])
            cond = f"{filled} AND ({num} IS NULL OR {num} NOT BETWEEN {lo} AND {hi})"
        elif name == "at_least":
            cond = f"{filled} AND ({num} IS NULL OR {num} < {g['n']})"
        elif name == "at_most":
            cond = f"{filled} AND ({num} IS NULL OR {num} > {g['n']})"
        elif name == "date":
            cond = f"{filled} AND TRY_CONVERT(date, {col}) IS NULL"
        elif name == "max_length":
            cond = f"LEN(CAST({col} AS nvarchar(4000))) > {int(g['n'])}"
        elif name == "conditional":
            other = _field(g["g"], entity, fields)
            ot = f"LTRIM(RTRIM(CAST(c.[{other}] AS nvarchar(4000))))"
            cond = f"{txt} = {_lit(_strip_quotes(g['v']))} AND (c.[{other}] IS NULL OR {ot} = '')"
        else:  # matching
            other_entity = _entity_name(g["o"], fields)
            theirs = _field(g.get("g") or g["f"], other_entity, fields) if g.get("g") else _field(g["f"], other_entity, fields)
            cond = f"{col} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {{canon:{other_entity}}} p WHERE p.[{theirs}] = {col})"
        keys = (f"SELECT TOP ({MAX_KEYS}) CAST(c.[{key}] AS nvarchar(400)) FROM {{canon:{entity}}} c WHERE {cond}" if key and key in fields.get(entity, []) else None)
        return {"pattern": name, "sql": f"SELECT COUNT(*) FROM {{canon:{entity}}} c WHERE {cond}", "keys_sql": keys}
    raise RuleError("That sentence is not one of the supported patterns. " + supported)


# ------------------------------------------------------------------------------------------------------------------ SQL check
# Functions a rule may call (sqlglot class names for the ones it knows, upper-case names for the rest).
_FUNC_CLASSES = {"Count", "Sum", "Min", "Max", "Avg", "Trim", "Cast", "TryCast", "Convert", "Coalesce", "Length", "Lower", "Upper", "Substring", "Replace",
                 "Abs", "Round", "Nullif", "Concat", "ConcatWs", "Left", "Right", "StrPosition", "Case", "If", "DateDiff", "DateAdd", "Floor", "Ceil", "Distinct",
                 "Exists", "And", "Or", "Not", "Paren", "In", "Between", "Is", "Cast", "Ltrim", "Rtrim", "Len", "Isnull", "Datetrunc", "Sign", "Pow", "Power", "Sqrt"}
_FUNC_NAMES = {"TRY_CONVERT", "TRY_CAST", "CONVERT", "ISNULL", "LEN", "LTRIM", "RTRIM", "CHARINDEX", "PATINDEX", "DATEDIFF", "DATEADD", "ISNUMERIC", "ISDATE",
               "ABS", "ROUND", "FLOOR", "CEILING", "UPPER", "LOWER", "SUBSTRING", "REPLACE", "LEFT", "RIGHT", "COALESCE", "NULLIF", "CONCAT", "CAST", "FORMAT"}


def _placeholder_to_ident(sql: str) -> str:
    return PLACEHOLDER.sub(lambda m: ("__C__" if m.group(1) == "canon" else "__M__") + m.group(2), sql)


def check_sql(sql: str, keys_sql: str = "", canon_entities: Iterable[str] = (), mirror_tables: Iterable[str] = ()) -> Dict[str, Any]:
    """The static check. Returns {"ok", "problems": [...], "tables": [...]}; nothing is executed."""
    import sqlglot
    from sqlglot import exp
    canon, mirror = set(canon_entities), set(mirror_tables)
    problems: List[str] = []
    tables: Set[str] = set()

    def one(label: str, text: str, keys: bool) -> None:
        text = str(text or "").strip()
        if not text:
            problems.append(f"{label}: empty.")
            return
        if len(text) > MAX_SQL:
            problems.append(f"{label}: longer than {MAX_SQL} characters.")
            return
        if ";" in text or "--" in text or "/*" in text or "@" in text:
            problems.append(f"{label}: semicolons, comments and variables are not allowed.")
            return
        for kind, name in PLACEHOLDER.findall(text):
            if (kind == "canon" and name not in canon) or (kind == "mirror" and name not in mirror):
                problems.append(f"{label}: {{{kind}:{name}}} is not a {'canonical entity' if kind == 'canon' else 'live table'} of this package.")
            tables.add(f"{kind}:{name}")
        probe = _placeholder_to_ident(text)
        if "{" in probe or "}" in probe:
            problems.append(f"{label}: a placeholder is not written as {{canon:Entity}} or {{mirror:LiveTable}}.")
            return
        try:
            parsed = sqlglot.parse(probe, read="tsql")
        except Exception as exc:  # sqlglot's ParseError / TokenError
            problems.append(f"{label}: not valid T-SQL ({type(exc).__name__}: {str(exc)[:160]}).")
            return
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
            problems.append(f"{label}: must be exactly one SELECT (no other statement, no UNION).")
            return
        node = parsed[0]
        if node.args.get("into") or node.args.get("with") or node.args.get("with_"):
            problems.append(f"{label}: SELECT INTO and WITH are not allowed.")
        if len(node.expressions) != 1:
            problems.append(f"{label}: must return exactly one column.")
        for t in node.find_all(exp.Table):
            ident = t.name or ""
            if t.args.get("db") or t.args.get("catalog") or not isinstance(t.this, exp.Identifier) or not re.fullmatch(r"__[CM]__[A-Za-z0-9_]+", ident):
                problems.append(f"{label}: tables are read only through {{canon:Entity}} or {{mirror:LiveTable}} (found '{t.sql('tsql')}').")
        for f in node.find_all(exp.Func):
            cls = type(f).__name__
            name = (f.name or "").upper() if isinstance(f, exp.Anonymous) else ""
            if cls == "Anonymous":
                if name not in _FUNC_NAMES:
                    problems.append(f"{label}: function {name or '?'} is not allowed.")
            elif cls not in _FUNC_CLASSES:
                problems.append(f"{label}: function {cls.upper()} is not allowed.")
        for bad in (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Merge, exp.Union):
            if list(node.find_all(bad)):
                problems.append(f"{label}: {bad.__name__.upper()} is not allowed.")
        if keys:
            limit = node.args.get("limit")
            top = None
            if limit is not None:
                try:
                    top = int(limit.expression.name)
                except (AttributeError, ValueError, TypeError):
                    top = None
            if top is None or top > MAX_KEYS:
                problems.append(f"{label}: must use TOP ({MAX_KEYS}) or fewer rows.")

    one("SQL", sql, False)
    if str(keys_sql or "").strip():
        one("Keys SQL", keys_sql, True)
    return {"ok": not problems, "problems": problems, "tables": sorted(tables)}


def expand(sql: str, slug: str) -> str:
    """Placeholders to the package's table names (already validated)."""
    def name(m):
        return f"[xodus_canon].[{slug}_{m.group(2)}]" if m.group(1) == "canon" else f"[xodus_mirror].[{slug}_{m.group(2)}]"
    return PLACEHOLDER.sub(name, sql)


# --------------------------------------------------------------------------------------------------------------------- lifecycle
def _row(r: Any) -> Dict[str, Any]:
    d = dict(r)
    d["problems"] = json.loads(d.pop("problems_json") or "[]")
    return d


def get(conn: sqlite3.Connection, rule_id: str, version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    if version is None:
        r = conn.execute("SELECT * FROM validation_rules WHERE rule_id = ? ORDER BY version DESC LIMIT 1", (rule_id,)).fetchone()
    else:
        r = conn.execute("SELECT * FROM validation_rules WHERE rule_id = ? AND version = ?", (rule_id, version)).fetchone()
    return _row(r) if r else None


def list_rules(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, Any]]:
    """Every version of every rule of the migration, newest first within a rule."""
    ensure_table(conn)
    return [_row(r) for r in conn.execute("SELECT * FROM validation_rules WHERE migration_id = ? ORDER BY rule_id, version DESC", (migration_id,)).fetchall()]


def create(conn: sqlite3.Connection, migration_id: str, actor: str, *, entity: str, layer: str, name: str, severity: str, english: str = "", sql: str = "",
           keys_sql: str = "", fields: Dict[str, List[str]], keys: Optional[Dict[str, str]] = None, canon_entities: Iterable[str] = (),
           mirror_tables: Iterable[str] = (), rule_id: Optional[str] = None) -> Dict[str, Any]:
    """Save a new rule (version 1) or a new version of an existing one, as a DRAFT. English is compiled; SQL is checked. A rule with problems is saved too,
    with its problems listed, so the author can fix it; it cannot be submitted until it is clean and dry-run."""
    ensure_table(conn)
    if layer not in LAYERS or severity not in SEVERITIES:
        raise RuleError("Choose a layer (canonical or mirror) and a severity (BLOCKING or WARN).")
    if not NAME.match(str(name or "").strip()):
        raise RuleError("Give the rule a name of 3 to 80 characters (letters, digits and simple punctuation).")
    if entity not in fields:
        raise RuleError(f"'{entity}' is not an entity of this migration.")
    english, sql, keys_sql = str(english or "").strip(), str(sql or "").strip(), str(keys_sql or "").strip()
    if bool(english) == bool(sql):
        raise RuleError("Write the rule either as an English sentence or as SQL, not both.")
    if english:
        if layer != "canonical":
            raise RuleError("English rules check the canonical table. Write a mirror-layer rule as SQL.")
        compiled = compile_english(english, entity, fields, (keys or {}).get(entity))
        sql, keys_sql = compiled["sql"], compiled["keys_sql"] or ""
    result = check_sql(sql, keys_sql, canon_entities, mirror_tables)
    version = 1
    if rule_id:
        prior = get(conn, rule_id)
        if not prior or prior["migration_id"] != migration_id:
            raise RuleError("That rule does not exist.")
        if prior["status"] == "PROPOSED":
            raise RuleError("That version is waiting for a decision; decide it before writing a new version.")
        version = prior["version"] + 1
        conn.execute("UPDATE validation_rules SET status = 'SUPERSEDED' WHERE rule_id = ? AND status = 'DRAFT'", (rule_id,))
    else:
        import uuid
        rule_id = "VR-" + uuid.uuid4().hex[:8].upper()
    conn.execute("INSERT INTO validation_rules (rule_id, version, migration_id, entity, layer, name, severity, english, sql_text, keys_sql, status, problems_json, "
                 "created_by, created_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?)",
                 (rule_id, version, migration_id, entity, layer, name.strip(), severity, english or None, sql, keys_sql or None, json.dumps(result["problems"]), actor, _now()))
    return get(conn, rule_id, version)


def record_dry_run(conn: sqlite3.Connection, rule_id: str, version: int, actor: str, count: Optional[int] = None, error: str = "") -> Dict[str, Any]:
    rule = get(conn, rule_id, version)
    if not rule:
        raise RuleError("That rule does not exist.")
    if rule["status"] != "DRAFT":
        raise RuleError("Only a draft can be dry-run.")
    conn.execute("UPDATE validation_rules SET dry_run_count = ?, dry_run_utc = ?, dry_run_by = ?, dry_run_error = ? WHERE rule_id = ? AND version = ?",
                 (count, _now(), actor, (error or None), rule_id, version))
    return get(conn, rule_id, version)


def submit(conn: sqlite3.Connection, rule_id: str, version: int, actor: str) -> Dict[str, Any]:
    rule = get(conn, rule_id, version)
    if not rule or rule["status"] != "DRAFT":
        raise RuleError("Only a draft can be submitted.")
    if rule["problems"]:
        raise RuleError("The rule has problems: " + " ".join(rule["problems"]))
    if rule["dry_run_count"] is None or rule["dry_run_error"]:
        raise RuleError("Run the dry run successfully before submitting: it proves the query runs on the latest batch.")
    conn.execute("UPDATE validation_rules SET status = 'PROPOSED', submitted_by = ? WHERE rule_id = ? AND version = ?", (actor, rule_id, version))
    return get(conn, rule_id, version)


def decide(conn: sqlite3.Connection, rule_id: str, version: int, decision: str, actor: str, comments: str = "") -> Dict[str, Any]:
    rule = get(conn, rule_id, version)
    if decision not in ("APPROVED", "REJECTED"):
        raise RuleError("Decision must be APPROVED or REJECTED.")
    if not rule or rule["status"] != "PROPOSED":
        raise RuleError("Only a submitted rule can be decided.")
    if actor in (rule["created_by"], rule["submitted_by"]):
        raise RuleError("You cannot decide a rule you wrote or submitted.")
    if decision == "REJECTED" and not str(comments or "").strip():
        raise RuleError("A comment is required to reject a rule.")
    conn.execute("UPDATE validation_rules SET status = ?, decided_by = ?, decided_utc = ?, decision_comments = ? WHERE rule_id = ? AND version = ?",
                 (decision, actor, _now(), str(comments or "").strip() or None, rule_id, version))
    if decision == "APPROVED":  # the new version replaces the old approved one
        conn.execute("UPDATE validation_rules SET status = 'RETIRED' WHERE rule_id = ? AND version <> ? AND status = 'APPROVED'", (rule_id, version))
    return get(conn, rule_id, version)


def retire(conn: sqlite3.Connection, rule_id: str, actor: str) -> Dict[str, Any]:
    rule = next((r for r in [get(conn, rule_id)] if r), None)
    approved = conn.execute("SELECT version FROM validation_rules WHERE rule_id = ? AND status = 'APPROVED'", (rule_id,)).fetchone()
    if not rule or not approved:
        raise RuleError("Only an approved rule can be retired.")
    conn.execute("UPDATE validation_rules SET status = 'RETIRED', decided_by = ?, decided_utc = ? WHERE rule_id = ? AND status = 'APPROVED'", (actor, _now(), rule_id))
    return get(conn, rule_id, approved["version"])


def approved_for_package(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, Any]]:
    """The approved rules the next package carries, in a stable order."""
    ensure_table(conn)
    return [_row(r) for r in conn.execute("SELECT * FROM validation_rules WHERE migration_id = ? AND status = 'APPROVED' ORDER BY rule_id", (migration_id,)).fetchall()]

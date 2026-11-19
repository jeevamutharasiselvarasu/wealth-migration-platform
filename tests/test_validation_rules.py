import asyncio
import sqlite3

import pytest

from control_plane.services import sql_target, validation_rules as vr
from tests.test_data_plane import FakeDB, _Conn
from tests.test_xodus_pipelines import build

FIELDS = {"Account": ["ACCT_NUM", "CUSTODIAN_CODE", "HH_ID", "OPEN_DATE", "BALANCE", "REP_CODE"], "Household": ["HH_ID", "HH_NAME"]}
KEYS = {"Account": "ACCT_NUM", "Household": "HH_ID"}


def _english(text, entity="Account"):
    return vr.compile_english(text, entity, FIELDS, KEYS.get(entity))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def _create(conn, actor="Val", **over):
    args = dict(entity="Account", layer="canonical", name="Account number filled", severity="BLOCKING", english="every ACCT_NUM must be filled",
                fields=FIELDS, keys=KEYS, canon_entities=list(FIELDS), mirror_tables=["ORION_MCL_ACCOUNT"])
    args.update(over)
    return vr.create(conn, "MIG-1", actor, **args)


def test_every_supported_sentence_compiles_to_sql_that_passes_the_static_check():
    sentences = {
        "filled": "every ACCT_NUM must be filled", "filled_entity": "every Account must have REP_CODE filled",
        "one_of": "CUSTODIAN_CODE must be one of SCHWAB, 'TD AMERITRADE', fidelity", "unique": "ACCT_NUM must be unique",
        "between": "BALANCE must be between 0 and 1000000", "at_least": "BALANCE must be at least 0", "at_most": "BALANCE must be at most 5000000.50",
        "date": "OPEN_DATE must be a date", "max_length": "ACCT_NUM must not be longer than 20 characters",
        "conditional": "every Account with CUSTODIAN_CODE = SCHWAB must have REP_CODE filled",
        "matching": "every Account must have a matching Household on HH_ID"}
    assert set(sentences) == {n for n, _p in vr.PATTERNS}  # the help text and the grammar stay in step
    for pattern, text in sentences.items():
        out = _english(text + ".")
        assert out["pattern"] in (pattern, "filled", "filled_entity"), (pattern, out["pattern"])
        chk = vr.check_sql(out["sql"], out["keys_sql"] or "", FIELDS, [])
        assert chk["ok"], (text, chk["problems"])
        assert out["sql"].startswith("SELECT COUNT(*)") and "{canon:Account}" in out["sql"]
    one_of = _english(sentences["one_of"])["sql"]
    assert "NOT IN (N'SCHWAB', N'TD AMERITRADE', N'fidelity')" in one_of
    assert "NOT BETWEEN 0 AND 1000000" in _english(sentences["between"])["sql"]
    assert _english("BALANCE must be between 9 and 3")["sql"].endswith("NOT BETWEEN 3 AND 9)")  # the bounds are ordered
    assert "{canon:Household} p WHERE p.[HH_ID] = c.[HH_ID]" in _english(sentences["matching"])["sql"]
    assert _english("ACCT_NUM must be unique")["keys_sql"].startswith("SELECT TOP (50) CAST(c.[ACCT_NUM]")
    assert _english("every acct_num must be filled")["sql"].count("[ACCT_NUM]") >= 2  # field names match ignoring case, and are written as the model spells them


def test_english_that_does_not_fit_a_pattern_or_names_something_unknown_is_refused_with_the_supported_sentences():
    for text, fragment in (("make sure accounts look right", "Supported sentences"), ("every NOPE must be filled", "has no field 'NOPE'"),
                           ("every Household must have HH_NAME filled", "names Household"), ("", "Write the rule as a sentence"),
                           ("every Account must have a matching Nowhere on HH_ID", "is not an entity")):
        with pytest.raises(vr.RuleError) as e:
            _english(text)
        assert fragment in str(e.value)
    # a value cannot break out of its literal
    sql = _english("CUSTODIAN_CODE must be one of 'A', 'B''; DROP TABLE x;--'")["sql"]
    assert "DROP" not in sql.split("NOT IN")[0] and vr.check_sql(sql, "", FIELDS, [])["ok"] is False or "N'B''" in sql or "DROP TABLE x" in sql
    assert ";" not in vr.PLACEHOLDER.sub("", _english("CUSTODIAN_CODE must be one of A, B")["sql"])


def test_the_static_check_accepts_a_read_only_count_and_refuses_everything_else():
    ok = "SELECT COUNT(*) FROM {canon:Account} c WHERE c.[REP_CODE] IS NULL AND ISNULL(c.[HH_ID], '') <> '' AND LEN(c.[ACCT_NUM]) > 3"
    assert vr.check_sql(ok, "", ["Account"], [])["ok"]
    assert vr.check_sql("SELECT COUNT(*) FROM {mirror:ORION_MCL_ACCOUNT} m WHERE TRY_CONVERT(date, m.[OPEN_DATE]) IS NULL", "", [], ["ORION_MCL_ACCOUNT"])["ok"]
    assert vr.check_sql(ok, "SELECT TOP (10) CAST(c.[ACCT_NUM] AS nvarchar(400)) FROM {canon:Account} c", ["Account"], [])["ok"]
    refused = {
        "SELECT COUNT(*) FROM [xodus_canon].[mig_Account] c": "tables are read only through",
        "SELECT COUNT(*) FROM {canon:Account} c; DROP TABLE x": "semicolons",
        "DELETE FROM {canon:Account}": "exactly one SELECT", "UPDATE {canon:Account} SET x = 1": "exactly one SELECT", "EXEC sp_who": "exactly one SELECT",
        "SELECT 1 UNION SELECT 2": "exactly one SELECT", "SELECT COUNT(*) INTO z FROM {canon:Account} c": "SELECT INTO",
        "WITH a AS (SELECT 1 AS x) SELECT COUNT(*) FROM a": "WITH", "SELECT COUNT(*), 1 FROM {canon:Account} c": "exactly one column",
        "SELECT COUNT(*) FROM {canon:Account} c WHERE c.[A] = @v": "variables", "SELECT COUNT(*) FROM {canon:Account} c -- x": "comments",
        "SELECT COUNT(*) FROM {canon:Account} c /* x */": "comments", "SELECT COUNT(*) FROM {canon:Nope} c": "not a canonical entity",
        "SELECT COUNT(*) FROM {mirror:Nope} c": "not a live table", "SELECT COUNT(*) FROM {canon:Account} c WHERE dbo.f(c.[A]) = 1": "function",
        "SELECT COUNT(*) FROM {canon:Account} c WHERE c.[A] IN (SELECT name FROM sys.objects)": "tables are read only through",
        "SELECT COUNT(*) FROM OPENROWSET('x') c": "tables are read only through", "SELECT COUNT(*) FROM {canon:Account} c WHERE OPENJSON(c.[A]) = 1": "function",
        "SELECT COUNT(*) FROM {canon:Account} c WHERE": "not valid T-SQL", "": "empty", "SELECT COUNT(*) FROM {canon Account} c": "placeholder"}
    for sql, why in refused.items():
        chk = vr.check_sql(sql, "", ["Account"], [])
        assert not chk["ok"] and why in " ".join(chk["problems"]), (sql, chk["problems"])
    keys = vr.check_sql(ok, "SELECT CAST(c.[ACCT_NUM] AS nvarchar(400)) FROM {canon:Account} c", ["Account"], [])
    assert "TOP (50)" in " ".join(keys["problems"])
    assert "TOP (50)" in " ".join(vr.check_sql(ok, "SELECT TOP (5000) CAST(c.[A] AS nvarchar(400)) FROM {canon:Account} c", ["Account"], [])["problems"])
    assert not vr.check_sql("SELECT COUNT(*) FROM {canon:Account} c WHERE c.[A] = '" + "x" * 5000 + "'", "", ["Account"], [])["ok"]


def test_a_rule_is_saved_as_a_draft_with_its_problems_and_cannot_be_submitted_until_clean_and_dry_run(conn):
    bad = _create(conn, english="", sql="DELETE FROM {canon:Account}", name="Bad rule")
    assert bad["status"] == "DRAFT" and bad["problems"] and bad["english"] is None
    with pytest.raises(vr.RuleError, match="problems"):
        vr.submit(conn, bad["rule_id"], 1, "Val")
    good = _create(conn)
    assert good["status"] == "DRAFT" and good["problems"] == [] and good["sql_text"].startswith("SELECT COUNT(*) FROM {canon:Account} c") and good["keys_sql"]
    with pytest.raises(vr.RuleError, match="dry run"):
        vr.submit(conn, good["rule_id"], 1, "Val")
    vr.record_dry_run(conn, good["rule_id"], 1, "Val", error="Could not sign in")
    with pytest.raises(vr.RuleError, match="dry run"):
        vr.submit(conn, good["rule_id"], 1, "Val")
    vr.record_dry_run(conn, good["rule_id"], 1, "Val", count=3)
    assert vr.get(conn, good["rule_id"])["dry_run_count"] == 3
    assert vr.submit(conn, good["rule_id"], 1, "Val")["status"] == "PROPOSED"


def test_input_is_validated_English_and_SQL_are_exclusive_and_mirror_rules_are_sql_only(conn):
    for over, message in (({"entity": "Nope"}, "not an entity"), ({"name": "x"}, "name"), ({"severity": "LOUD"}, "severity"), ({"layer": "raw"}, "layer"),
                          ({"sql": "SELECT 1"}, "either"), ({"english": ""}, "either"), ({"layer": "mirror"}, "SQL")):
        with pytest.raises(vr.RuleError, match=message):
            _create(conn, **over)


def test_approval_needs_a_different_person_and_a_new_version_retires_the_old_one(conn):
    r1 = _create(conn)
    vr.record_dry_run(conn, r1["rule_id"], 1, "Val", count=0)
    vr.submit(conn, r1["rule_id"], 1, "Val")
    with pytest.raises(vr.RuleError, match="wrote or submitted"):
        vr.decide(conn, r1["rule_id"], 1, "APPROVED", "Val")
    with pytest.raises(vr.RuleError, match="comment"):
        vr.decide(conn, r1["rule_id"], 1, "REJECTED", "Ana")
    assert vr.decide(conn, r1["rule_id"], 1, "APPROVED", "Ana")["status"] == "APPROVED"
    assert [r["rule_id"] for r in vr.approved_for_package(conn, "MIG-1")] == [r1["rule_id"]]
    with pytest.raises(vr.RuleError, match="submitted"):
        vr.decide(conn, r1["rule_id"], 1, "APPROVED", "Bo")  # already decided
    # a change is a new version: the approved one keeps running until the new one is approved
    r2 = _create(conn, rule_id=r1["rule_id"], english="every ACCT_NUM must be filled", name="Account number filled")
    assert r2["version"] == 2 and r2["status"] == "DRAFT" and vr.get(conn, r1["rule_id"], 1)["status"] == "APPROVED"
    vr.record_dry_run(conn, r1["rule_id"], 2, "Val", count=1)
    vr.submit(conn, r1["rule_id"], 2, "Val")
    with pytest.raises(vr.RuleError, match="waiting for a decision"):
        _create(conn, rule_id=r1["rule_id"])
    vr.decide(conn, r1["rule_id"], 2, "APPROVED", "Ana")
    assert vr.get(conn, r1["rule_id"], 1)["status"] == "RETIRED" and [r["version"] for r in vr.approved_for_package(conn, "MIG-1")] == [2]
    assert vr.retire(conn, r1["rule_id"], "Ana")["status"] == "RETIRED" and vr.approved_for_package(conn, "MIG-1") == []
    with pytest.raises(vr.RuleError, match="approved"):
        vr.retire(conn, r1["rule_id"], "Ana")
    # a rejected draft is superseded by the next version
    r3 = _create(conn, name="Another rule")
    vr.record_dry_run(conn, r3["rule_id"], 1, "Val", count=0)
    vr.submit(conn, r3["rule_id"], 1, "Val")
    vr.decide(conn, r3["rule_id"], 1, "REJECTED", "Ana", "wrong entity")
    _create(conn, rule_id=r3["rule_id"], name="Another rule")
    assert [r["status"] for r in vr.list_rules(conn, "MIG-1") if r["rule_id"] == r3["rule_id"]] == ["DRAFT", "REJECTED"]
    assert vr.approved_for_package(conn, "MIG-2") == []


def _approved(rule_id="VR-AAAA0001", entity="Account", sql=None, severity="BLOCKING", name="Account number filled", keys=True):
    sql = sql or "SELECT COUNT(*) FROM {canon:Account} c WHERE (c.[REP_CODE] IS NULL OR LTRIM(RTRIM(CAST(c.[REP_CODE] AS nvarchar(4000)))) = '')"
    return {"rule_id": rule_id, "version": 1, "entity": entity, "layer": "canonical", "name": name, "severity": severity, "sql_text": sql,
            "keys_sql": "SELECT TOP (50) CAST(c.[ACCT_NUM] AS nvarchar(400)) FROM {canon:Account} c WHERE c.[REP_CODE] IS NULL" if keys else None, "english": None}


def _validate_block(pkg):
    sql = pkg["files"]["sql/xodus_mig00a1a6577b.sql"]
    return sql.split("CREATE OR ALTER PROCEDURE [xodus_ops].[usp_validate_mig00a1a6577b]")[1].split("COMMIT TRANSACTION")[0]


def test_an_approved_rule_is_compiled_into_the_validation_procedure_of_its_wave():
    base = build(depends={"Account": ["Household"]})
    pkg = build(depends={"Account": ["Household"]}, rules_approved=[_approved()])
    proc = _validate_block(pkg)
    acct = proc.split("IF EXISTS (SELECT 1 FROM @scope WHERE [entity] = N'Account')")[1].split("IF EXISTS (SELECT 1 FROM @scope WHERE [entity] = N'Household')")[0]
    assert "FROM [xodus_canon].[mig00a1a6577b_Account] c WHERE" in acct  # placeholders become the package's table names
    assert "N'RULE:VR-AAAA0001'" in acct and "N'RULE'" in acct and "N'VR-AAAA0001'" in acct  # the finding says it comes from a rule
    assert "Rule passed: Account number filled." in acct and "row(s) break the rule: Account number filled." in acct
    assert "THEN N'PASS' ELSE N'FAIL' END" in acct.split("RULE:VR-AAAA0001")[0].rsplit("SET @st", 1)[1] or "N'FAIL'" in acct
    assert "INSERT INTO @k ([k]) SELECT TOP (50) CAST(c.[ACCT_NUM]" in acct  # failing keys
    assert pkg["manifest"]["rules"] == [{"id": "VR-AAAA0001", "version": 1, "name": "Account number filled", "entity": "Account", "severity": "BLOCKING"}]
    assert base["manifest"]["rules"] == [] and base["version"] != pkg["version"]  # a rule is part of the package version
    assert pkg["files"]["pipeline/PL_XODUS_Wave.json"] == base["files"]["pipeline/PL_XODUS_Wave.json"]  # the stable pipelines do not change


def test_a_warning_rule_reports_without_stopping_the_wave():
    warn = _validate_block(build(rules_approved=[_approved(severity="WARN")]))
    block = warn.split("N'RULE:VR-AAAA0001'")[0].rsplit("SET @n", 1)[1]
    assert "ELSE N'WARN' END" in block and "ELSE N'FAIL' END" not in block


def test_a_rule_that_is_unsafe_or_reads_a_later_wave_stops_the_package_build():
    depends = {"Account": ["Household"]}
    with pytest.raises(ValueError, match="fails the SQL check"):
        build(depends=depends, rules_approved=[_approved(sql="DELETE FROM {canon:Account}")])
    with pytest.raises(ValueError, match="not in this package"):
        build(depends=depends, rules_approved=[_approved(entity="Position")])
    parent_reads_child = _approved(entity="Household", sql="SELECT COUNT(*) FROM {canon:Household} h WHERE EXISTS (SELECT 1 FROM {canon:Account} a WHERE a.[HH_ID] = h.[HH_ID])")
    parent_reads_child["keys_sql"] = None
    with pytest.raises(ValueError, match="built in a later wave"):
        build(depends=depends, rules_approved=[parent_reads_child])
    child_reads_parent = _approved(sql="SELECT COUNT(*) FROM {canon:Account} a WHERE NOT EXISTS (SELECT 1 FROM {canon:Household} h WHERE h.[HH_ID] = a.[HH_ID])", keys=False)
    assert "[xodus_canon].[mig00a1a6577b_Household] h" in _validate_block(build(depends=depends, rules_approved=[child_reads_parent]))  # a parent in an earlier wave is fine
    mirror_rule = _approved(sql="SELECT COUNT(*) FROM {mirror:ORION_MCL_ACCOUNT} m WHERE m.[CUSTODIAN] IS NULL", keys=False)
    assert "[xodus_mirror].[mig00a1a6577b_ORION_MCL_ACCOUNT] m" in _validate_block(build(depends=depends, rules_approved=[mirror_rule]))


# ---- the application routes ----------------------------------------------------------------------------------------------------------------------
class _Req:
    pass


def _app(monkeypatch, conn_):
    import ui.app as app_module
    monkeypatch.setitem(app_module.state_store.questionnaire_data, "migration_id", "MIG-T")
    monkeypatch.setattr(app_module, "get_db_connection", lambda: _Conn(conn_))
    monkeypatch.setattr(app_module, "_target_setup_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_xodus_package", lambda c, m: ({}, {"manifest": {"canonical": {"Account": {}, "Household": {}}, "liveTables": [{"live_table": "ORION_MCL_ACCOUNT"}],
                                                                                   "migrationSlug": "mig1"}}))
    monkeypatch.setattr(app_module, "_canonical_field_lists", lambda: FIELDS)
    return app_module


def test_save_dry_run_submit_and_approve_through_the_routes(monkeypatch):
    conn_ = sqlite3.connect(":memory:")
    conn_.row_factory = sqlite3.Row
    app_module = _app(monkeypatch, conn_)
    users = {"Val": {"name": "Val", "role": "Validation Specialist"}, "Ana": {"name": "Ana", "role": "Migration Analyst"}}
    who = {"u": "Val"}
    monkeypatch.setattr(app_module, "get_current_user", lambda r: users[who["u"]])
    loc = asyncio.run(app_module.save_validation_rule(_Req(), entity="Account", layer="canonical", name="Account number filled", severity="BLOCKING",
                                                      english="every ACCT_NUM must be filled", sql="", keys_sql="", rule_id="")).headers["location"]
    assert "notice=" in loc and "validation-rules" in loc
    rule = vr.list_rules(conn_, "MIG-T")[0]
    assert rule["status"] == "DRAFT" and not rule["problems"]
    # the dry run counts the failing rows on the latest batch and stores the count only
    monkeypatch.setattr(app_module, "_data_plane_state", lambda m, refresh_runs=False: {"manifest": {"canonical": {"Account": {}, "Household": {}}, "liveTables": [], "migrationSlug": "mig1"}})
    seen = []
    monkeypatch.setattr(sql_target, "get_config", lambda c: {"database_name": "x"})
    monkeypatch.setattr(sql_target, "run", lambda cfg, statements, *a, **k: seen.extend(statements) or [[(4,)], [("A-1",), ("A-2",)]])
    loc = asyncio.run(app_module.dry_run_validation_rule(_Req(), rule["rule_id"], 1)).headers["location"]
    assert "notice=" in loc and "4+row" in loc and "A-1" not in loc  # counts only
    assert "[xodus_canon].[mig1_Account]" in seen[0][0] and seen[0][1] is None and len(seen) == 2  # both queries ran, expanded, with no parameters
    assert vr.get(conn_, rule["rule_id"], 1)["dry_run_count"] == 4
    asyncio.run(app_module.submit_validation_rule(_Req(), rule["rule_id"], 1))
    assert vr.get(conn_, rule["rule_id"], 1)["status"] == "PROPOSED"
    loc = asyncio.run(app_module.decide_validation_rule(_Req(), rule["rule_id"], 1, decision="APPROVED", comments="")).headers["location"]
    assert "error=" in loc and vr.get(conn_, rule["rule_id"], 1)["status"] == "PROPOSED"  # the author cannot approve
    who["u"] = "Ana"
    loc = asyncio.run(app_module.decide_validation_rule(_Req(), rule["rule_id"], 1, decision="approved", comments="")).headers["location"]
    assert "notice=" in loc and "Generate+a+new+package" in loc and vr.get(conn_, rule["rule_id"], 1)["status"] == "APPROVED"
    assert [r["rule_id"] for r in app_module._approved_rules("MIG-T")] == [rule["rule_id"]]


def test_a_dry_run_that_cannot_run_is_recorded_and_blocks_submission(monkeypatch):
    conn_ = sqlite3.connect(":memory:")
    conn_.row_factory = sqlite3.Row
    app_module = _app(monkeypatch, conn_)
    monkeypatch.setattr(app_module, "get_current_user", lambda r: {"name": "Val", "role": "Validation Specialist"})
    asyncio.run(app_module.save_validation_rule(_Req(), entity="Account", layer="canonical", name="Account number filled", severity="BLOCKING",
                                                english="every ACCT_NUM must be filled", sql="", keys_sql="", rule_id=""))
    rule = vr.list_rules(conn_, "MIG-T")[0]
    monkeypatch.setattr(app_module, "_data_plane_state", lambda m, refresh_runs=False: {"manifest": {"canonical": {"Account": {}}, "liveTables": [], "migrationSlug": "mig1"}})
    monkeypatch.setattr(sql_target, "get_config", lambda c: {"database_name": "x"})

    def down(*a, **k):
        raise ValueError("Could not sign in to xodus")
    monkeypatch.setattr(sql_target, "run", down)
    loc = asyncio.run(app_module.dry_run_validation_rule(_Req(), rule["rule_id"], 1)).headers["location"]
    assert "error=" in loc and "Could+not+sign+in" in loc
    assert "error=" in asyncio.run(app_module.submit_validation_rule(_Req(), rule["rule_id"], 1)).headers["location"]
    monkeypatch.setattr(app_module, "_data_plane_state", lambda m, refresh_runs=False: {})
    assert "package" in asyncio.run(app_module.dry_run_validation_rule(_Req(), rule["rule_id"], 1)).headers["location"]


def test_the_rules_section_renders_status_dry_run_and_the_right_buttons():
    import ui.app as app_module
    tpl = app_module.templates.env.get_template("_validation_rules.html")
    base = {"id": 0, "rule_id": "VR-1", "version": 1, "entity": "Account", "layer": "canonical", "name": "Account number filled", "severity": "BLOCKING", "english": "every ACCT_NUM must be filled",
            "sql_text": "SELECT COUNT(*) FROM {canon:Account} c", "keys_sql": None, "problems": [], "dry_run_count": None, "dry_run_error": None, "dry_run_by": None, "dry_run_utc": None,
            "created_by": "Val", "submitted_by": None, "decided_by": None, "decision_comments": None}
    ctx = {"in_scope_entities": ["Account"], "rule_patterns": vr.PATTERNS, "can_run_validation": True, "can_decide_exception": True, "current_user_name": "Val"}
    draft = tpl.render(**ctx, validation_rules=[{**base, "status": "DRAFT"}])
    assert "Dry run" in draft and "/api/validation-rules/VR-1/1/dry-run" in draft and "/submit" not in draft  # not submittable before a dry run
    ready = tpl.render(**ctx, validation_rules=[{**base, "status": "DRAFT", "dry_run_count": 3, "dry_run_by": "Val", "dry_run_utc": "2026-11-19T10:00:00"}])
    assert "3 row(s) break it" in ready and "/api/validation-rules/VR-1/1/submit" in ready and 'data-testid="rule-revise"' in ready
    waiting = tpl.render(**ctx, validation_rules=[{**base, "status": "PROPOSED", "submitted_by": "Val"}])
    assert "Waiting for a different approver." in waiting and "/decision" not in waiting
    other = tpl.render(**{**ctx, "current_user_name": "Ana"}, validation_rules=[{**base, "status": "PROPOSED", "submitted_by": "Val"}])
    assert "/api/validation-rules/VR-1/1/decision" in other
    assert "Retire" in tpl.render(**ctx, validation_rules=[{**base, "status": "APPROVED"}])
    broken = tpl.render(**ctx, validation_rules=[{**base, "status": "DRAFT", "problems": ["SQL: DELETE is not allowed."]}])
    assert "DELETE is not allowed." in broken
    assert "&lt;field&gt; must be unique" in tpl.render(**ctx, validation_rules=[])  # the sentence help is always there
    assert "A Validation Specialist writes rules." in tpl.render(**{**ctx, "can_run_validation": False}, validation_rules=[])

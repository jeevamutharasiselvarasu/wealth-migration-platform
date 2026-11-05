"""Transformation SQL (stage 06): approved mappings and registered rules become T-SQL; nothing else is emitted.
The statements are generated text; they are not executed against SQL Server here."""
import pytest

from control_plane.services import azure_simulated, sql_package as sp
from control_plane.services import transformation_rules as tr


def _rule(kind, params, status="APPROVED"):
    return {"rule_type": kind, "params": tr.validate_params(kind, params), "status": status}


def _spec():
    return {"spec_version": "v1", "target_system": "Orion", "entities": {"Account": {"target_table": "T", "fields": [
        {"name": f"T.{c}", "table": "T", "column": c, "type": ty, "required": req} for c, ty, req in
        (("ACCT_NO", "string", True), ("OPEN_DT", "date", True), ("REG_CD", "string", False), ("REP", "string", True))]}}}


def _maps(**status):
    base = [("T.ACCT_NO", "ACCT_NUM", "DIRECT_COPY"), ("T.OPEN_DT", "OPEN_DATE", "RULE:D@1"), ("T.REG_CD", "REG_TYPE", "RULE:V@1")]
    return [{"entity_name": "Account", "target_field": t, "canonical_field": c, "transformation_expression": e,
             "status": status.get(t, "APPROVED"), "reviewer": "Ann"} for t, c, e in base]


RULES = {"RULE:D@1": _rule("DATE_FORMAT", {"input_formats": ["MM/DD/YYYY", "YYYYMMDD"]}),
         "RULE:V@1": _rule("VALUE_MAP", {"map": {"IND'V": "INDIVIDUAL"}})}


def test_build_emits_staging_tables_one_insert_per_target_table_and_a_procedure():
    pkg = sp.build("MIG-1", _spec(), _maps(**{"T.REG_CD": "PROPOSED"}), RULES, azure_simulated.simulated_environment("MIG-1"), "Ann")
    files, m = pkg["files"], pkg["manifest"]
    assert set(files) == {"sql/00_staging_tables.sql", "sql/01_target_tables.sql", "sql/Account_T.sql", "sql/usp_load_all.sql", "manifest.json"}
    sql = files["sql/Account_T.sql"]
    assert "INSERT INTO [dbo].[T] ([ACCT_NO], [OPEN_DT])" in sql and "FROM [staging].[Account] AS s" in sql
    assert "s.[ACCT_NUM] AS [ACCT_NO]" in sql and "TRY_CONVERT(date, LTRIM(RTRIM(s.[OPEN_DATE])), 101)" in sql and "REG_CD" not in sql
    assert "[OPEN_DATE] nvarchar(4000)" in files["sql/00_staging_tables.sql"] and "REG_TYPE" not in files["sql/00_staging_tables.sql"]
    assert "CREATE OR ALTER PROCEDURE [dbo].[usp_xodus_load_all]" in files["sql/usp_load_all.sql"] and "ROLLBACK" in files["sql/usp_load_all.sql"]
    assert m["carrier"] == "SQL" and m["executed"] is False and m["entities"]["Account"]["approvedMappings"] == 2
    assert any("T.REP" in w for w in m["warnings"])  # a required field without an approved mapping is reported


def test_unapproved_and_unregistered_expressions_are_refused():
    with pytest.raises(ValueError, match="not an approved"):
        sp.translate("RULE:D@1", "OPEN_DATE", _rule("DATE_FORMAT", {"input_formats": ["YYYYMMDD"]}, status="DRAFT"))
    with pytest.raises(ValueError, match="not an approved"):
        sp.translate("DROP TABLE x", "A", None)
    with pytest.raises(ValueError, match="safe column name"):
        sp.translate("DIRECT_COPY", "A]; DROP TABLE x;--", None)
    with pytest.raises(ValueError, match="at least one"):
        sp.build("M", _spec(), _maps(**{"T.ACCT_NO": "PROPOSED", "T.OPEN_DT": "REJECTED", "T.REG_CD": "PROPOSED"}), {}, azure_simulated.simulated_environment("M"), "Ann")


def test_each_rule_type_translates_and_quotes_literals():
    t = lambda kind, params, col="C": sp.translate("RULE:X@1", col, _rule(kind, params))
    assert "N'IND''V'" in t("VALUE_MAP", {"map": {"IND'V": "X"}})  # quotes in values are doubled, never break out
    assert "ELSE NULL END" in t("VALUE_MAP", {"map": {"A": "B"}, "strict": True}) or "ELSE s.[C] END" in t("VALUE_MAP", {"map": {"A": "B"}})
    assert t("DEFAULT_IF_BLANK", {"default": "N/A"}).startswith("CASE WHEN NULLIF(LTRIM(RTRIM(s.[C])), N'') IS NULL THEN N'N/A' ELSE s.[C] END")
    assert t("IF_BLANK", {"when_blank": "Open", "when_filled": "Closed"}) == (
        "CASE WHEN NULLIF(LTRIM(RTRIM(s.[C])), N'') IS NULL THEN N'Open' ELSE N'Closed' END")
    assert "CONCAT_WS(N' ', LTRIM(RTRIM(s.[FIRST])), LTRIM(RTRIM(s.[LAST])))" == t("CONCAT", {"sources": ["FIRST", "LAST"], "separator": " "}, "FIRST")
    assert "STRING_SPLIT" in t("SPLIT_PART", {"delimiter": ",", "index": 1}) and "[ordinal] = 2" in t("SPLIT_PART", {"delimiter": ",", "index": 1})
    assert "TRY_CONVERT(date, REPLACE(LTRIM(RTRIM(s.[C])), N'-', N' '), 106)" in t("DATE_FORMAT", {"input_formats": ["DD-MON-YYYY"]})
    num = t("NUMBER_CLEAN", {"strip_chars": "$,", "decimals": 2, "parentheses_negative": True})
    assert "decimal(38, 2)" in num and "LIKE N'(%)'" in num and "REPLACE(" in num
    assert "= N'Y' THEN N'Yes'" in t("IF_EQUALS", {"when_equals": "Y", "then": "Yes"})


def test_lookup_reads_the_parent_staging_table_and_creates_it():
    rule = _rule("LOOKUP", {"parent_file": "corecap_advisors.csv", "parent_key": "ADVISOR_ID", "return_column": "Name"})
    expr = sp.translate("RULE:L@1", "REP_CD", rule)
    assert "FROM [staging].[lkp_corecap_advisors] AS l" in expr and "l.[Name]" in expr and "= LTRIM(RTRIM(s.[REP_CD]))" in expr
    spec = _spec()
    maps = [{"entity_name": "Account", "target_field": "T.REP", "canonical_field": "REP_CD", "transformation_expression": "RULE:L@1", "status": "APPROVED"}]
    pkg = sp.build("M", spec, maps, {"RULE:L@1": rule}, azure_simulated.simulated_environment("M"), "Ann")
    ddl = pkg["files"]["sql/00_staging_tables.sql"]
    assert "[staging].[lkp_corecap_advisors]" in ddl and "[ADVISOR_ID] nvarchar(4000)" in ddl and "[Name] nvarchar(4000)" in ddl
    assert "lkp_corecap_advisors" in pkg["manifest"]["stagingTables"]


def test_concat_sources_are_created_as_staging_columns():
    rule = _rule("CONCAT", {"sources": ["FIRST", "LAST"], "separator": " "})
    maps = [{"entity_name": "Account", "target_field": "T.REP", "canonical_field": "FIRST", "transformation_expression": "RULE:C@1", "status": "APPROVED"}]
    pkg = sp.build("M", _spec(), maps, {"RULE:C@1": rule}, azure_simulated.simulated_environment("M"), "Ann")
    ddl = pkg["files"]["sql/00_staging_tables.sql"]
    assert "[FIRST] nvarchar(4000)" in ddl and "[LAST] nvarchar(4000)" in ddl

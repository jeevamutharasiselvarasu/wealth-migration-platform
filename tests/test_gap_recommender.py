"""The gap recommender must work from the data and the run problems alone, on any file set and field names."""
import sqlite3

import pytest

from control_plane.services import gap_recommender as gr
from control_plane.services import transformation_rules as tr


def _tables():
    staff = [{"StaffId": f"S{i}", "Label": f"Person {i}", "Codes": f"[\"C{i}a\",\"C{i}b\"]"} for i in range(1, 7)]
    cases = [{"CaseNo": f"K{i}", "StaffId": f"S{i}" if i <= 4 else ""} for i in range(1, 9)]
    items = [{"CaseNo": f"K{i}", "Code": f"c{i - 2}a"} for i in range(5, 9)] + [{"CaseNo": f"K{i}", "Code": f"C{i}a"} for i in range(1, 5)]
    return {"staff.csv": {"entity": "Staff", "rows": staff, "lineage": {}},
            "cases.csv": {"entity": "Case", "rows": cases, "lineage": {"OWNER": "StaffId"}},
            "items.csv": {"entity": "Case", "rows": items, "lineage": {}}}


def test_alternative_route_is_found_from_value_overlap_not_names():
    paths = gr.find_alternative_paths(_tables(), "cases.csv", "StaffId")
    assert paths and paths[0]["goal"] == "staff.csv.StaffId"
    assert paths[0]["blank_rows"] == 4 and paths[0]["covered_rows"] == 4
    assert all("StaffId" != step[1] for step in paths[0]["path"][:1] if step[0] == "cases.csv")


def test_no_route_when_nothing_links():
    t = _tables()
    t["items.csv"]["rows"] = [{"CaseNo": "Z9", "Code": "nothing"}]
    assert gr.find_alternative_paths(t, "cases.csv", "StaffId") == []


def _run(rule=None, rows=None, mapping=None, failures=None, tables=None, lookups=None, keys=None, required=True):
    m = {"entity": "Case", "target_table": "T", "target_column": "OWNER_NAME", "canonical_field": "OWNER", "transformation": "DIRECT_COPY", "required": required}
    if mapping:
        m.update(mapping)
    rules = {m["transformation"]: rule} if rule else {}
    f = failures or [{"entity": "Case", "table": "T", "column": "OWNER_NAME", "rule": m["transformation"], "reason": "required value blank after transformation", "rows": 4}]
    return gr.recommend("MIG", f, [m], rules, {"Case": rows or []}, tables or {}, lookups, {"entity_keys": keys or {}})


def test_blank_field_gets_route_vendor_note_and_relax_proposal():
    rows = [{"OWNER": "S1"}] * 2 + [{"OWNER": ""}] * 6
    kinds = {r["kind"]: r for r in _run(rows=rows, tables=_tables())}
    assert {"ALTERNATIVE_SOURCE", "RELAX_REQUIREMENT", "SOURCE_REQUEST"} <= set(kinds)
    assert "cases.csv.StaffId" in kinds["SOURCE_REQUEST"]["vendor_note"]
    assert kinds["ALTERNATIVE_SOURCE"]["rule_draft"] is None          # never proposes a rule it cannot justify


def test_crosswalk_gap_lists_values_without_entry_and_drafts_only_close_matches():
    rule = {"rule_type": "VALUE_MAP", "params": tr.validate_params("VALUE_MAP", {"map": {"IRA Rollover": "IRA Rollover", "Trust": "Trust"}, "strict": True})}
    rows = [{"OWNER": "IRA Rolover"}, {"OWNER": "Mystery"}, {"OWNER": "Trust"}]
    f = [{"entity": "Case", "table": "T", "column": "OWNER_NAME", "rule": "RULE:MAPX@1", "reason": "unmapped value '…'", "rows": 2}]
    (rec,) = [r for r in _run(rule=rule, rows=rows, mapping={"transformation": "RULE:MAPX@1"}, failures=f) if r["kind"] == "CROSSWALK_GAP"]
    assert rec["detail"]["values_without_entry"] == 2 and rec["detail"]["suggested_pairs"] == 1
    assert rec["rule_draft"]["rule_id"] == "MAPX" and "IRA ROLOVER" in rec["rule_draft"]["params"]["map"]
    assert any("'Mystery'" in line for line in rec["evidence"])      # a lookup column shows its coded values so the reviewer can decide


def test_date_values_in_another_layout_propose_the_matching_format():
    rule = {"rule_type": "DATE_FORMAT", "params": tr.validate_params("DATE_FORMAT", {"input_formats": ["YYYY-MM-DD"]})}
    rows = [{"OWNER": "31/12/2020"}, {"OWNER": "25/01/2021"}]
    f = [{"entity": "Case", "table": "T", "column": "OWNER_NAME", "rule": "RULE:D@1", "reason": "date '…' matches none of the declared formats", "rows": 2}]
    (rec,) = [r for r in _run(rule=rule, rows=rows, mapping={"transformation": "RULE:D@1"}, failures=f) if r["kind"] == "FORMAT_FIX"]
    assert "DD/MM/YYYY" in rec["rule_draft"]["params"]["input_formats"] and "YYYY-MM-DD" in rec["rule_draft"]["params"]["input_formats"]


def test_split_failures_report_the_delimiter_the_values_use():
    rule = {"rule_type": "SPLIT_PART", "params": tr.validate_params("SPLIT_PART", {"delimiter": ",", "index": 1})}
    rows = [{"OWNER": "Smith Family"}, {"OWNER": "Jones Family"}]
    f = [{"entity": "Case", "table": "T", "column": "OWNER_NAME", "rule": "RULE:S@1", "reason": "no part 2 after splitting '…'", "rows": 2}]
    (rec,) = [r for r in _run(rule=rule, rows=rows, mapping={"transformation": "RULE:S@1"}, failures=f) if r["kind"] == "FORMAT_FIX"]
    assert rec["detail"]["alternative_delimiter"] == " "


def test_lookup_keys_that_match_after_leading_zeros_and_orphans_are_separated():
    params = tr.validate_params("LOOKUP", {"parent_file": "p.csv", "parent_key": "Id", "return_column": "Name"})
    rule = {"rule_type": "LOOKUP", "params": params}
    lookups = {tr.lookup_key(params): {"0012": "A", "77": "B"}}
    rows = [{"OWNER": "12"}, {"OWNER": "999"}]
    f = [{"entity": "Case", "table": "T", "column": "OWNER_NAME", "rule": "RULE:L@1", "reason": "no matching row in the lookup file", "rows": 2}]
    (rec,) = [r for r in _run(rule=rule, rows=rows, mapping={"transformation": "RULE:L@1"}, failures=f, lookups=lookups) if r["kind"] == "KEY_MISMATCH"]
    assert "1 key(s) would match" in rec["evidence"][0] and "1 key(s) have no row" in rec["evidence"][1] and "p.csv" in rec["vendor_note"]


def test_rows_without_key_and_dangling_references_are_reported_across_entities():
    canon = {"Parent": [{"PID": "1"}, {"PID": ""}, {"PID": ""}], "Child": [{"PID": "1"}, {"PID": "9"}, {"PID": "8"}]}
    fails = [{"entity": "Parent", "table": "T", "column": "X", "rule": "DIRECT_COPY", "reason": "required value blank after transformation", "rows": 2},
             {"entity": "Child", "table": "T", "column": "Y", "rule": "DIRECT_COPY", "reason": "required value blank after transformation", "rows": 1}]
    recs = gr._structural("M", fails, canon, {"Parent": "PID"})
    assert {r["kind"] for r in recs} == {"ROWS_WITHOUT_KEY", "REFERENCE_GAP"}
    assert any("2 of 3 Parent rows" in r["title"] for r in recs) and any("2 of 3 Child rows" in r["title"] for r in recs)


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    tr.ensure_table(c)
    return c


def test_decisions_are_recorded_accept_makes_only_a_draft_and_dismiss_needs_a_reason():
    c = _conn()
    rule = {"rule_type": "VALUE_MAP", "params": tr.validate_params("VALUE_MAP", {"map": {"Trust": "Trust"}})}
    # a rule must exist as a version before a draft of the same id is added
    tr.create_rule_version(c, "MAPX", "VALUE_MAP", "base", rule["params"], "analyst")
    f = [{"entity": "Case", "table": "T", "column": "OWNER_NAME", "rule": "RULE:MAPX@1", "reason": "unmapped value '…'", "rows": 1}]
    recs = _run(rule=rule, rows=[{"OWNER": "Trusts"}], mapping={"transformation": "RULE:MAPX@1"}, failures=f)
    gr.save(c, "MIG", "RUN-1", recs)
    gr.save(c, "MIG", "RUN-2", recs)                                   # a later run keeps the earlier id
    listed = gr.list_for_run(c, "MIG", "RUN-2")
    rid = next(r["rec_id"] for r in listed if r["kind"] == "CROSSWALK_GAP")
    with pytest.raises(ValueError):
        gr.decide(c, "MIG", rid, "DISMISSED", " ", "u")
    with pytest.raises(ValueError):
        gr.decide(c, "MIG", rid, "VENDOR_REQUESTED", "", "u")           # no provider note on a crosswalk proposal
    res = gr.decide(c, "MIG", rid, "ACCEPTED", "ok", "analyst2")
    assert res["draft"] == "RULE:MAPX@2"
    assert [v["status"] for v in tr.list_rules(c) if v["rule_id"] == "MAPX"][-1] == "DRAFT"


def test_rows_without_the_entity_key_are_not_counted_again_as_gaps_in_every_field():
    rows = [{"ID": "1", "OWNER": ""}] + [{"ID": "", "OWNER": ""}] * 9
    recs = _run(rows=rows, keys={"Case": "ID"})
    blank = [r for r in recs if r["kind"] in ("RELAX_REQUIREMENT", "SOURCE_REQUEST")]
    assert all(r["rows"] == 1 for r in blank)                      # only the one real row, not the 9 padding lines
    assert [r["kind"] for r in recs].count("ROWS_WITHOUT_KEY") == 1


def test_home_file_is_the_one_with_most_rows_and_repeated_titles_are_dropped():
    t = {"small.csv": {"entity": "Case", "rows": [{"Other": ""}] * 2, "lineage": {"OWNER": "Other"}},
         "big.csv": {"entity": "Case", "rows": [{"Own": ""}] * 6, "lineage": {"OWNER": "Own"}}}
    recs = _run(rows=[{"OWNER": ""}] * 6, tables=t)
    src = [r for r in recs if r["kind"] == "SOURCE_REQUEST"]
    assert len(src) == 1 and "big.csv.Own" in src[0]["title"]

"""The rule language: a sentence may carry a condition ("where HouseholdOrder = 1"); it holds in memory and in the generated SQL."""
from control_plane.services import rule_inference as ri
from control_plane.services import sql_package as sp
from control_plane.services import transformation_rules as tr


def _one(sentence):
    hits = [r for r in ri.infer(sentence) if r["recognised"]]
    assert len(hits) == 1, sentence
    return hits[0]


def test_where_clause_becomes_a_condition_on_any_rule_type():
    copy = _one("Copy the value where HouseholdOrder = 1")
    assert copy["rule_type"] == "COPY" and copy["params"]["condition"] == {"field": "HouseholdOrder", "op": "=", "value": "1"}
    split = _one("Household first name is the part after the comma in the combined name where HouseholdOrder = 1")
    assert split["rule_type"] == "SPLIT_PART" and split["params"]["index"] == 1 and split["params"]["condition"]["value"] == "1"
    assert _one("Copy the value where Status is not 'Deleted'")["params"]["condition"] == {"field": "Status", "op": "<>", "value": "Deleted"}
    assert _one("Copy the value where Status is blank")["params"]["condition"] == {"field": "Status", "op": "IS_BLANK"}
    assert "only for rows where HouseholdOrder = 1" in copy["meaning"]
    # the same sentence without the clause is a different rule
    assert ri.rule_id_for("COPY", copy["params"]) != ri.rule_id_for("COPY", {})


def test_a_rule_applies_only_to_the_rows_that_meet_its_condition():
    params = tr.validate_params("COPY", {"condition": {"field": "HouseholdOrder", "op": "=", "value": "1"}})
    assert tr.apply_rule("COPY", params, "123-45", {"HouseholdOrder": "1"}) == ("123-45", None)
    assert tr.apply_rule("COPY", params, "999-99", {"householdorder": 2}) == (None, None)  # names compared without case
    value_map = tr.validate_params("VALUE_MAP", {"map": {"A": "B"}, "condition": {"field": "K", "op": "NOT_BLANK"}})
    assert tr.apply_rule("VALUE_MAP", value_map, "A", {"K": "x"}) == ("B", None) and tr.apply_rule("VALUE_MAP", value_map, "A", {"K": ""}) == (None, None)


def test_the_condition_is_in_the_generated_sql_and_its_column_is_read():
    rule = {"status": "APPROVED", "rule_type": "COPY", "params": tr.validate_params("COPY", {"condition": {"field": "HouseholdOrder", "op": "=", "value": "1"}})}
    sql = sp.translate("RULE:X@1", "TAX_ID", rule)
    assert sql == "CASE WHEN UPPER(LTRIM(RTRIM(s.[HouseholdOrder]))) = N'1' THEN s.[TAX_ID] END"
    assert sp.columns_used("RULE:X@1", "TAX_ID", rule) == {"TAX_ID", "HouseholdOrder"}


def test_bad_conditions_are_refused():
    import pytest
    for bad in ({"field": "a;drop", "op": "=", "value": "1"}, {"field": "A", "op": "LIKE", "value": "1"}, {"field": "A", "op": "=", "value": ""}):
        with pytest.raises(ValueError):
            tr.validate_params("COPY", {"condition": bad})

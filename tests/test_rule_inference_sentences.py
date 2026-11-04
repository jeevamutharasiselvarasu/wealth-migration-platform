"""Business-rule sentences as a target's mapping sheet words them must become governed rule proposals."""
import pytest

from control_plane.services import rule_inference


def _one(text):
    (res,) = rule_inference.infer(text)
    return res


def test_if_blank_sentence_becomes_if_blank_rule():
    res = _one("If ClosedDate is NULL, then Open Else Closed")
    assert res["recognised"] and res["rule_type"] == "IF_BLANK"
    assert res["params"] == {"when_blank": "Open", "when_filled": "Closed"}


def test_crosswalk_with_reject_clause_is_strict():
    res = _one("Map 'Rollover IRA' -> 'IRA Rollover'; anything else is rejected")
    assert res["rule_type"] == "VALUE_MAP" and res["params"]["strict"] is True


def test_crosswalk_without_reject_clause_is_not_strict():
    assert "strict" not in _one("Map SCH to SCHWAB, FID to FIDELITY")["params"]


@pytest.mark.parametrize("text,delim,index", [
    ("Household first name is the part after the comma in the combined name", ",", 1),
    ("Household last name is the part before the comma in the combined name", ",", 0),
    ("Registration Name: remove everything after ' - ' in AccountName", " - ", 0),
])
def test_split_wordings(text, delim, index):
    res = _one(text)
    assert res["rule_type"] == "SPLIT_PART" and res["params"] == {"delimiter": delim, "index": index}


def test_unrelated_sentence_is_still_not_recognised():
    assert not _one("Custodian and Account Number are copied as-is")["recognised"]

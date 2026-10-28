from ui.work_queue import build_queue

BASE = {"scope": ["Account"], "files_total": 3, "files_pending": 0, "proposals_pending": 0, "rfi_open": 0, "changes_open": 0,
        "mappings_approved": 0, "validation_status": "", "target_provisioned": True, "target_pending": 0}


def labels(role, **kw):
    return [i["label"] for i in build_queue(role, {**BASE, **kw})]


def test_empty_queue_when_nothing_is_waiting():
    assert build_queue("Migration Analyst", BASE) == []


def test_analyst_sees_actions_they_can_perform_with_counts():
    q = build_queue("Migration Analyst", {**BASE, "files_pending": 2, "proposals_pending": 1, "rfi_open": 3, "target_pending": 4})
    assert [(i["label"], i["count"]) for i in q] == [("Confirm 2 source files", 2), ("Review 1 mapping proposal", 1),
                                                    ("Answer 3 information requests", 3), ("Approve 4 target mappings", 4)]
    assert all(i["actionable"] for i in q)


def test_change_requests_are_actionable_for_sme_and_informational_for_analyst():
    sme = build_queue("Domain SME", {**BASE, "changes_open": 2})
    analyst = build_queue("Migration Analyst", {**BASE, "changes_open": 2})
    assert sme[0]["label"] == "Decide 2 change requests" and sme[0]["actionable"]
    assert analyst[0]["label"].endswith("awaiting the Domain SME") and not analyst[0]["actionable"]
    assert build_queue("Validation Specialist", {**BASE, "changes_open": 2}) == []


def test_roles_never_get_items_they_may_not_act_on():
    busy = {**BASE, "files_pending": 1, "proposals_pending": 1, "rfi_open": 1, "target_pending": 1, "mappings_approved": 5, "target_provisioned": False}
    assert labels("Domain SME", **busy) == ["Answer 1 information request"]
    assert labels("Validation Specialist", **busy) == ["Run validation"]
    assert "Run validation" not in labels("Migration Analyst", **busy)


def test_validation_item_changes_with_last_result():
    assert labels("Validation Specialist", mappings_approved=3, validation_status="PASS") == []
    assert labels("Validation Specialist", mappings_approved=3, validation_status="FAIL") == ["Review failed validation"]


def test_setup_and_target_provisioning_prompts():
    assert labels("Migration Analyst", scope=[]) == ["Set up the migration"]
    assert labels("Platform Admin", target_provisioned=False)[0] == "Complete the one-time target setup"
    assert labels("Migration Analyst", target_provisioned=False) == ["Target setup awaits a Platform Admin"]
    assert labels("Domain SME", scope=[], target_provisioned=False) == []

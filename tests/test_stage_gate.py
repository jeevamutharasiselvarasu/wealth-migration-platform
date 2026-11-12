import asyncio

import httpx

import ui.app as app_module
from ui.role_policy import gated_stage_for


def test_later_phase_routes_are_classified_by_stage():
    assert gated_stage_for("POST", "/api/target-mapping/adf-package") == "07"
    assert gated_stage_for("POST", "/api/git/publish") == "08"
    assert gated_stage_for("POST", "/api/data-plane/start") == "09"
    assert gated_stage_for("POST", "/api/data-plane/promote") == "10"
    assert gated_stage_for("POST", "/api/transformation-rules") is None
    assert gated_stage_for("GET", "/api/git/publish") is None


def test_blocker_names_the_first_unfinished_stage(monkeypatch):
    stages = [{"number": "01", "title": "Loader", "state": "done"}, {"number": "02", "title": "Discovery", "state": "done"},
              {"number": "03", "title": "Business Rules", "state": "current"}, {"number": "04", "title": "Schema Mapper", "state": "todo"}]
    monkeypatch.setattr(app_module, "workflow_progress", lambda: {"stages": stages})
    assert "Stage 03 Business Rules" in app_module._stage_blocker("07")
    assert app_module._stage_blocker("03") == ""
    stages[2]["state"] = "done"
    assert app_module._stage_blocker("04") == ""


def test_generate_package_is_blocked_until_business_rules_are_confirmed(monkeypatch):
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "s1", {"email": "a@example.invalid", "name": "A", "role": "IMO Lead"})
    monkeypatch.setattr(app_module, "workflow_progress", lambda: {"stages": [
        {"number": "01", "title": "Loader", "state": "done"}, {"number": "02", "title": "Discovery", "state": "done"},
        {"number": "03", "title": "Business Rules", "state": "current"}]})

    async def call(path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://t", follow_redirects=False,
                                     cookies={app_module.session_mgr.COOKIE_NAME: "s1"} if hasattr(app_module.session_mgr, "COOKIE_NAME") else {}) as c:
            return await c.post(path, headers={"accept": "application/json"})

    r = asyncio.run(call("/api/packages/generate"))
    assert r.status_code in (409, 401, 303)

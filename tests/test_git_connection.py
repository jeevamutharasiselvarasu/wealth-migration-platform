import sqlite3

import httpx
import pytest

from control_plane.services import git_connection as gc

GOOD = {"repository": "https://github.com/acme/xodus-adf-artifacts.git", "branch": "", "adf_folder": "", "token_env_var": ""}
PKG = {"pipeline/PL_X.json": "{}", "dataset/DS_A.json": "{}", "linkedService/LS_A.json": "{}", "sql/usp_load_all.sql": "select 1", "arm/ARMTemplateForFactory.json": "{}", "manifest.json": "{}"}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def _resp(status, body=None):
    return httpx.Response(status, json=body or {})


def test_validation_normalises_the_repository_and_rejects_typed_tokens_and_bad_names():
    v = gc.validate(GOOD)
    assert v == {"repository": "acme/xodus-adf-artifacts", "branch": "main", "adf_folder": "adf", "token_env_var": "GITHUB_ADF_TOKEN"}
    for key, bad in [("repository", "not a repo"), ("repository", "a/b/c"), ("branch", "../x"), ("adf_folder", "a/b"),
                     ("token_env_var", "ghp_realtokenpastedbymistake"), ("token_env_var", "POSTGRES_PASSWORD")]:
        with pytest.raises(ValueError):
            gc.validate({**GOOD, key: bad})


def test_save_never_stores_the_token_and_reports_only_that_it_is_present(conn, monkeypatch):
    monkeypatch.delenv("GITHUB_ADF_TOKEN", raising=False)
    assert gc.save(conn, "Ann", GOOD)["token_present"] is False
    monkeypatch.setenv("GITHUB_ADF_TOKEN", "ghp_secret_value")
    cfg = gc.get_config(conn)
    assert cfg["token_present"] is True and "ghp_secret_value" not in str(cfg)


def _api(push=True, branch=200, seen=None):
    def http(method, url, **kw):
        if seen is not None:
            seen.append((method, url.replace(gc.API, ""), kw))
        assert "Bearer tok" in kw["headers"]["Authorization"]
        path = url.replace(gc.API, "")
        if method == "GET" and path == "/repos/acme/xodus-adf-artifacts":
            return _resp(200, {"permissions": {"push": push}})
        if method == "GET" and "/branches/" in path:
            return _resp(branch)
        if method == "GET" and "/git/ref/heads/" in path:
            return _resp(200, {"object": {"sha": "head1"}})
        if method == "GET" and "/git/commits/head1" in path:
            return _resp(200, {"tree": {"sha": "tree0"}})
        if method == "GET" and "/git/trees/tree0" in path:
            return _resp(200, {"truncated": False, "tree": [
                {"path": "adf/pipeline/OLD.json", "type": "blob"}, {"path": "adf/pipeline/PL_X.json", "type": "blob"},
                {"path": "adf/linkedService/LS_BLOB.json", "type": "blob"}, {"path": "adf/linkedService/LS_ADLS_Staging.json", "type": "blob"},
                {"path": "adf/linkedService/LS_KeyVault.json", "type": "blob"}, {"path": "adf/linkedService/LS_SQL_Target.json", "type": "blob"}, {"path": "adf/factory/f.json", "type": "blob"},
                {"path": "xodus/MIG-0/manifest.json", "type": "blob"}, {"path": "README.md", "type": "blob"}, {"path": "adf/dataset", "type": "tree"}]})
        if method == "POST" and path.endswith("/git/trees"):
            return _resp(201, {"sha": "tree1"})
        if method == "POST" and path.endswith("/git/commits"):
            return _resp(201, {"sha": "commit1234567"})
        if method == "PATCH":
            return _resp(200, {})
        return _resp(500)
    return http


def test_connection_statuses(conn, monkeypatch):
    monkeypatch.setenv("GITHUB_ADF_TOKEN", "tok")
    gc.save(conn, "Ann", GOOD)
    assert gc.test_connection(conn, _api())["status"] == "CONNECTED"
    assert "cannot write" in gc.test_connection(conn, _api(push=False))["check_detail"]
    assert "Branch main does not exist" in gc.test_connection(conn, _api(branch=404))["check_detail"]
    monkeypatch.delenv("GITHUB_ADF_TOKEN")
    assert "no value for GITHUB_ADF_TOKEN" in gc.test_connection(conn, _api())["check_detail"]


def test_layout_puts_native_adf_json_under_the_root_folder_and_the_rest_beside_it():
    out = gc.layout({"adf_folder": "adf"}, "MIG-1", PKG)
    assert set(out) == {"adf/pipeline/PL_X.json", "adf/dataset/DS_A.json", "adf/linkedService/LS_A.json", "xodus/MIG-1/sql/usp_load_all.sql",
                        "xodus/MIG-1/arm/ARMTemplateForFactory.json", "xodus/MIG-1/manifest.json"}
    for bad in ("../evil.json", "/abs.json", "pipeline/../../x.json"):
        with pytest.raises(ValueError):
            gc.layout({"adf_folder": "adf"}, "MIG-1", {bad: "x"})


def test_publish_adds_one_non_forced_commit_and_requires_a_successful_test(conn, monkeypatch):
    monkeypatch.setenv("GITHUB_ADF_TOKEN", "tok")
    gc.save(conn, "Ann", GOOD)
    with pytest.raises(ValueError, match="Test the GitHub connection"):
        gc.publish(conn, "Ann", "MIG-1", PKG, "msg", _api())
    gc.test_connection(conn, _api())
    seen = []
    out = gc.publish(conn, "Ann", "MIG-1", PKG, "msg", _api(seen=seen))
    assert out["commit_sha"] == "commit1234567" and out["files"] == 6 and out["url"].endswith("/commit/commit1234567")
    methods = [(m, p.split("/git/")[-1] if "/git/" in p else p) for m, p, _ in seen if m != "GET" or "/git/" in p]
    assert [m for m, _ in methods] == ["GET", "GET", "POST", "POST", "PATCH"]
    patch = [kw for m, p, kw in seen if m == "PATCH"][0]
    assert patch["json"]["force"] is False
    tree = [kw for m, p, kw in seen if p.endswith("/git/trees")][0]["json"]
    assert tree["base_tree"] == "tree0" and len(tree["tree"]) == 6
    assert gc.get_config(conn)["last_commit_sha"] == "commit1234567"


def test_publish_reports_github_refusals_without_leaking_the_token(conn, monkeypatch):
    monkeypatch.setenv("GITHUB_ADF_TOKEN", "tok")
    gc.save(conn, "Ann", GOOD)
    gc.test_connection(conn, _api())
    def refuse(method, url, **kw):
        return _resp(403)
    with pytest.raises(ValueError, match="Contents: read and write") as e:
        gc.publish(conn, "Ann", "MIG-1", PKG, "msg", refuse)
    assert "tok" not in str(e.value).replace("token", "")


def test_git_routes_are_role_gated_and_the_save_route_rejects_a_bad_repository():
    import asyncio
    from ui import app as app_module
    from ui.role_policy import allowed_roles_for
    assert allowed_roles_for("POST", "/api/git/connection") == allowed_roles_for("POST", "/api/git/connection/test") == {"Platform Admin"}
    assert allowed_roles_for("POST", "/api/git/publish") == {"Platform Admin", "Migration Analyst"}
    sessions = app_module.session_mgr.active_sessions
    sessions["sid"] = dict(sessions.get("sid", {}), role="Platform Admin", name="Ann")

    async def call(data):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.post("/api/git/connection", data=data, follow_redirects=False)

    r = asyncio.run(call({"repository": "not a repo", "branch": "main", "adf_folder": "adf", "token_env_var": "GITHUB_ADF_TOKEN"}))
    assert r.status_code == 303 and "error=Repository+must+look+like" in r.headers["location"] and r.headers["location"].startswith("/github-setup?")


def test_publish_records_the_artifact_so_the_stage_knows_which_package_went_out(conn, monkeypatch):
    monkeypatch.setenv("GITHUB_ADF_TOKEN", "tok")
    gc.save(conn, "Ann", GOOD)
    gc.test_connection(conn, _api())
    assert gc.get_config(conn)["last_artifact_id"] is None
    gc.publish(conn, "Ann", "MIG-1", PKG, "msg", _api(), artifact_id="ART-1")
    assert gc.get_config(conn)["last_artifact_id"] == "ART-1"


def test_rail_has_a_publish_stage_between_the_sql_and_the_adf_run():
    from ui.app import build_stage_view
    p = {"files_total": 3, "files_pending": 0, "mappings_approved": 38, "proposals_pending": 0, "rfi_open": 0,
         "validation_status": "PASS", "target_provisioned": True, "target_pending": 0}
    v = build_stage_view(p, rules_approved=8, rules_draft=0, target_approved=5, sql_ready=True, runs_n=1)
    assert [s["number"] + " " + s["title"] for s in v["stages"][6:]] == ["07 Transformation SQL", "08 Publish artifacts", "09 ADF Execution", "10 Target validation"]
    assert v["stage_current"] == "08 Publish artifacts"  # not published yet, so the run and the validation after it are not started
    v = build_stage_view(p, rules_approved=8, rules_draft=0, target_approved=5, sql_ready=True, runs_n=1, published=True)
    assert v["stages_done"] == 10


def test_publish_stage_page_renders_with_the_button_disabled_until_a_package_and_connection_exist():
    import asyncio
    from ui import app as app_module
    sessions = app_module.session_mgr.active_sessions
    sessions["sid"] = dict(sessions.get("sid", {}), role="Migration Analyst", name="Ann")

    async def get():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.get("/publish-artifacts")

    r = asyncio.run(get())
    assert r.status_code == 200 and "Stage 08 · Data plane · Publish artifacts" in r.text and 'data-testid="publish-button"' in r.text
    assert "<fieldset disabled" in r.text.split('data-testid="publish-card"')[1]  # nothing to publish and no tested connection in a fresh test database


def test_replace_deletes_only_old_xodus_files_never_linked_services_or_other_paths(conn, monkeypatch):
    monkeypatch.setenv("GITHUB_ADF_TOKEN", "tok")
    gc.save(conn, "Ann", GOOD)
    gc.test_connection(conn, _api())
    seen = []
    out = gc.publish(conn, "Ann", "MIG-1", PKG, "msg", _api(seen=seen), replace=True)
    tree = [kw for m, p, kw in seen if p.endswith("/git/trees") and m == "POST"][0]["json"]["tree"]
    deleted = sorted(e["path"] for e in tree if "sha" in e and e["sha"] is None)
    assert deleted == ["adf/pipeline/OLD.json", "xodus/MIG-0/manifest.json"] and out["removed"] == 2
    patch = [kw for m, p, kw in seen if m == "PATCH"][0]
    assert patch["json"]["force"] is False
    assert gc.publish(conn, "Ann", "MIG-1", PKG, "msg", _api())["removed"] == 0


def test_prune_linked_removes_only_generated_linked_services(conn, monkeypatch):
    monkeypatch.setenv("GITHUB_ADF_TOKEN", "tok")
    gc.save(conn, "Ann", GOOD)
    gc.test_connection(conn, _api())
    seen = []
    out = gc.publish(conn, "Ann", "MIG-1", PKG, "msg", _api(seen=seen), replace=True, prune_linked=True)
    tree = [kw for m, p, kw in seen if p.endswith("/git/trees") and m == "POST"][0]["json"]["tree"]
    deleted = sorted(e["path"] for e in tree if "sha" in e and e["sha"] is None)
    assert "adf/linkedService/LS_ADLS_Staging.json" in deleted and "adf/linkedService/LS_KeyVault.json" in deleted
    assert not [d for d in deleted if d.endswith(("LS_BLOB.json", "LS_SQL_Target.json"))] and out["removed"] == len(deleted)

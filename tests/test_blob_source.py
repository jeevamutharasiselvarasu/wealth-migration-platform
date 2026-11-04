import asyncio
import httpx
import pytest

from control_plane.services import blob_source as bs
from ui.role_policy import allowed_roles_for

EMU = "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=k;BlobEndpoint=http://blob:10000/devstoreaccount1;"
PROD = "DefaultEndpointsProtocol=https;AccountName=prodacct;AccountKey=k;EndpointSuffix=core.windows.net"


def test_only_the_local_emulator_is_recognised_and_seeding_is_refused_elsewhere():
    assert bs.is_emulator(EMU) and bs.is_emulator(EMU.replace("//blob:", "//127.0.0.1:"))
    assert not bs.is_emulator(PROD) and not bs.is_emulator("")
    with pytest.raises(ValueError):
        bs.seed("c", {"a.csv": b"1"}, PROD)


def test_blob_names_cannot_escape_the_container():
    for bad in ("", "/etc/passwd", "a/../b"):
        with pytest.raises(ValueError):
            bs.read_blob("c", bad, EMU)


def test_status_never_raises_and_says_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv(bs.ENV_VAR, raising=False)
    st = bs.status("c")
    assert st["configured"] is False and st["reachable"] is False and bs.ENV_VAR in st["detail"]


def test_blob_routes_are_role_gated():
    assert allowed_roles_for("POST", "/api/blob/load") == {"Platform Admin", "Migration Analyst"}
    assert allowed_roles_for("POST", "/api/blob/seed-demo") == {"Platform Admin"}


def test_loading_with_nothing_ticked_is_an_error_not_a_crash():
    from ui import app as app_module
    app_module.session_mgr.active_sessions["sid"] = {"role": "Migration Analyst", "name": "Ann", "email": "a@b.c"}

    async def go():
        t = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=t, base_url="http://testserver", cookies={"session_id": "sid"}) as c:
            return await c.post("/api/blob/load", data={}, follow_redirects=False)
    r = asyncio.run(go())
    assert r.status_code == 303 and "Tick+at+least+one" in r.headers["location"]


class _FakeContainer:
    def __init__(self):
        self.blobs = {}
        self.exists_flag = False

    def exists(self): return self.exists_flag
    def create_container(self): self.exists_flag = True
    def upload_blob(self, name, data, overwrite=True): self.blobs[name] = data
    def delete_blob(self, name): del self.blobs[name]
    def download_blob(self, name):
        from types import SimpleNamespace
        return SimpleNamespace(readall=lambda: self.blobs[name])

    def list_blobs(self, name_starts_with=None):
        from types import SimpleNamespace
        return [SimpleNamespace(name=n, size=len(d), last_modified=None) for n, d in sorted(self.blobs.items()) if not name_starts_with or n.startswith(name_starts_with)]


def _fake_service(monkeypatch):
    fc = _FakeContainer()
    from types import SimpleNamespace
    monkeypatch.setattr(bs, "_service", lambda cs=None: SimpleNamespace(get_container_client=lambda name: fc))
    return fc


def test_layout_creates_raw_stage_curated_and_the_source_firm_folder_without_duplicating(monkeypatch):
    fc = _fake_service(monkeypatch)
    first = bs.ensure_layout("lake", "CoreCap")
    assert first["container_created"] and set(first["folders_created"]) == {"raw/", "stage/", "curated/", "raw/corecap/"}
    assert bs.ensure_layout("lake", "CoreCap")["folders_created"] == []  # running it again changes nothing
    assert bs.source_prefix("Core Cap!") == "raw/core-cap/"
    with pytest.raises(ValueError):
        bs.ensure_layout("Bad_Name", "CoreCap")


def test_tree_shows_folders_hides_markers_and_source_list_reads_only_the_firm_folder(monkeypatch):
    fc = _fake_service(monkeypatch)
    bs.ensure_layout("lake", "CoreCap")
    fc.blobs["raw/corecap/accounts.csv"] = b"a,b\n1,2\n"
    fc.blobs["raw/other/x.csv"] = b"1"
    rows = bs.tree("lake")
    paths = [(r["path"], r["kind"]) for r in rows]
    assert ("raw/", "folder") in paths and ("stage/", "folder") in paths and ("curated/", "folder") in paths
    assert ("raw/corecap/accounts.csv", "file") in paths and not any(p.endswith(bs.MARKER) for p, _ in paths)
    tops = [p for p, k in paths if k == "folder" and p.count("/") == 1]
    assert tops[:3] == ["raw/", "stage/", "curated/"]  # pipeline order, not alphabetical
    names = [b["name"] for b in bs.list_source_blobs("lake", prefix="raw/corecap/")]
    assert names == ["raw/corecap/accounts.csv"]


def test_seeding_resets_only_the_source_folder(monkeypatch):
    fc = _fake_service(monkeypatch)
    monkeypatch.setenv(bs.ENV_VAR, EMU)
    bs.seed("lake", {"a.csv": b"1", "b.csv": b"2"}, prefix="raw/corecap/", source_firm="CoreCap")
    fc.blobs["stage/keep.csv"] = b"x"
    bs.seed("lake", {"a.csv": b"9"}, prefix="raw/corecap/", source_firm="CoreCap")
    assert "raw/corecap/b.csv" not in fc.blobs and fc.blobs["raw/corecap/a.csv"] == b"9"
    assert "stage/keep.csv" in fc.blobs and "raw/corecap/.xodus-folder" in fc.blobs


def test_stage_run_writes_tables_and_support_files_under_the_run_folder_only(monkeypatch):
    fc = _fake_service(monkeypatch)
    bs.ensure_layout("lake", "CoreCap")
    fc.blobs["raw/corecap/a.csv"] = b"keep"
    out = {"ORION_MCL_ACCOUNT": [{"ACCT": "1", "NAME": None}, {"ACCT": "2", "NAME": "x"}]}
    res = bs.write_stage_run("lake", "RUN-ABC12345", out, {"run": {"status": "SUCCEEDED"}, "problems": []})
    assert res["written"] and res["path"] == "stage/run-abc12345/"
    assert fc.blobs["stage/run-abc12345/ORION_MCL_ACCOUNT.csv"].decode() == "ACCT,NAME\n1,\n2,x\n"
    assert "stage/run-abc12345/_run/run.json" in fc.blobs and "stage/run-abc12345/_run/problems.json" in fc.blobs
    assert fc.blobs["raw/corecap/a.csv"] == b"keep" and not any(k.startswith("curated/") and not k.endswith(bs.MARKER) for k in fc.blobs)


def test_simulator_collects_loaded_rows_per_table_without_putting_values_in_telemetry():
    import json
    from control_plane.services import execution_simulator as ex
    pkg = {"meta": {"package_id": "P", "package_version": 1},
           "transformation_rules": [],
           "canonical_to_target": [{"entity": "Account", "target_table": "T", "canonical_field": "ID", "target_column": "ACCT_ID", "transformation": "DIRECT_COPY", "required": True}],
           "validation": {"scenarios": []}}
    outputs = {}
    tele = ex.simulate(pkg, {"Account": [{"ID": "SECRET-1"}, {"ID": ""}]}, outputs=outputs)
    assert outputs == {"T": [{"ACCT_ID": "SECRET-1"}]}
    assert tele["totals"]["loaded"] == 1 and tele["totals"]["skipped"] == 1
    assert "SECRET-1" not in json.dumps(tele)


def test_container_name_is_published_so_it_survives_a_restart():
    import pathlib
    app = pathlib.Path(__file__).resolve().parent.parent.joinpath("ui/app.py").read_text()
    body = app[app.index("async def create_blob_layout"):app.index("async def load_blob_source_files")]
    assert 'setup["blob_container"] = container' in body and 'publish_event("MIGRATION_CONTEXT_UPDATED"' in body


def test_promote_copies_only_the_runs_tables_to_curated_and_writes_lineage(monkeypatch):
    import json
    fc = _fake_service(monkeypatch)
    bs.ensure_layout("lake", "CoreCap")
    bs.write_stage_run("lake", "RUN-A", {"T1": [{"A": "1"}, {"A": "2"}]}, {"run": {"status": "SUCCEEDED"}})
    bs.write_stage_run("lake", "RUN-B", {"T1": [{"A": "9"}]}, {})
    res = bs.promote_to_curated("lake", "RUN-A", "Analyst One", {"ACKNOWLEDGED": 1})
    assert fc.blobs["curated/T1.csv"].decode() == "A\n1\n2\n" and res["files"][0]["rows"] == 2
    lin = json.loads(fc.blobs["curated/_lineage/run-a.json"])
    assert lin["promoted_by"] == "Analyst One" and lin["files"][0]["from"] == "stage/run-a/T1.csv"
    assert not any(k.startswith("curated/") and "run-b" in k for k in fc.blobs)
    with pytest.raises(ValueError):
        bs.promote_to_curated("lake", "RUN-NONE", "x", {})


def test_curation_gate_needs_reviewed_problems_and_passing_checks():
    import json, sqlite3
    from control_plane.services import execution_simulator as ex
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    ex.ensure_table(conn)
    pkg = {"meta": {"package_id": "P", "package_version": 1}}
    tele = {"status": "PARTIAL_SUCCESS", "failures": [{"entity": "E", "table": "T", "column": "C", "rule": "R", "rows": 1}], "post_checks": [{"scenario_id": "S1", "status": "PASS"}],
            "totals": {}, "entities": {}, "stage": {"written": True}}
    from datetime import datetime, timezone
    rid = ex.record_run(conn, "MIG", pkg, "a", tele, datetime.now(timezone.utc), 1)
    g = ex.curation_gate(conn, rid)
    assert not g["ok"] and "1 problem(s) have not been reviewed" in g["reasons"][0]
    ex.review_failure(conn, rid, 0, "ACKNOWLEDGED", "ok", "a")
    assert ex.curation_gate(conn, rid)["ok"]
    tele["post_checks"] = [{"scenario_id": "S1", "status": "FAIL"}]
    conn.execute("UPDATE execution_runs SET telemetry_json=?", (json.dumps(tele),))
    assert "target validation" in ex.curation_gate(conn, rid)["reasons"][0]
    ex.attach_curated(conn, rid, {"promoted": True})
    assert any("already curated" in r for r in ex.curation_gate(conn, rid)["reasons"])


def test_preview_shows_the_start_of_text_files_only(monkeypatch):
    fc = _fake_service(monkeypatch)
    fc.blobs["curated/T.csv"] = ("h\n" + "\n".join(str(i) for i in range(100))).encode()
    fc.blobs["raw/x.xlsx"] = b"PK"
    fc.blobs["raw/.xodus-folder"] = b""
    p = bs.preview("lake", "curated/T.csv", max_lines=5)
    assert p["shown_lines"] == 5 and p["truncated"] and p["text"].startswith("h\n0")
    for bad in ("raw/x.xlsx", "raw/.xodus-folder", "../x.csv"):
        with pytest.raises(ValueError):
            bs.preview("lake", bad)


def _scenario(name):
    import csv
    from pathlib import Path
    d = Path(__file__).resolve().parents[1] / "demo_data" / "scenarios" / name
    return {f: list(csv.DictReader(open(d / f, newline=""))) for f in ("corecap_households.csv", "corecap_household_contacts.csv", "corecap_accounts.csv", "corecap_advisors.csv")}


def test_clean_scenario_has_no_gaps_the_rules_would_report():
    s = _scenario("clean")
    hh = {r["Id"]: r for r in s["corecap_households.csv"]}
    adv = {r["Id"] for r in s["corecap_advisors.csv"]}
    assert "" not in hh and all("," in r["HouseholdName"] and r["AdvisorId"] in adv for r in hh.values())
    assert all(a["HouseholdId"] in hh and a["RegistrationType"] in ("IRA", "Rollover IRA") and a["AccountNumber"] and a["CustodianName"] for a in s["corecap_accounts.csv"])
    assert all(c["HouseholdId"] in hh and all(c[k] for k in ("Address1", "City", "State", "Zip")) for c in s["corecap_household_contacts.csv"])
    assert len({a["AccountId"] for a in s["corecap_accounts.csv"]}) == len(s["corecap_accounts.csv"])


def test_gaps_b_scenario_contains_each_kind_of_gap():
    s = _scenario("gaps_b")
    hh = {r["Id"] for r in s["corecap_households.csv"]}
    adv = {r["Id"] for r in s["corecap_advisors.csv"]}
    accs, houses, cons = s["corecap_accounts.csv"], s["corecap_households.csv"], s["corecap_household_contacts.csv"]
    assert any(a["HouseholdId"] not in hh for a in accs) and any(a["RegistrationType"] not in ("IRA", "Rollover IRA") for a in accs)
    assert any("," not in r["HouseholdName"] for r in houses) and any(not r["Id"] for r in houses) and any(r["AdvisorId"] not in adv for r in houses)
    assert any(not c["Zip"] for c in cons) and any(c["HouseholdId"] not in hh for c in cons)


def test_committed_scenarios_match_the_generator():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "demo_data" / "scenarios" / "generate.py"
    spec = importlib.util.spec_from_file_location("gen_scn", p); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
    hh, con, acc = g.build("gaps_b", 45, True)
    assert [r["Id"] for r in _scenario("gaps_b")["corecap_households.csv"]] == [r["Id"] for r in hh]


def test_seed_route_places_the_chosen_scenario(monkeypatch):
    from ui import app as app_module
    fc = _fake_service(monkeypatch)
    monkeypatch.setenv(bs.ENV_VAR, EMU)
    app_module.session_mgr.active_sessions["sid"] = {"role": "Platform Admin", "name": "Pat", "email": "p@b.c"}

    async def go(scn):
        t = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=t, base_url="http://testserver", cookies={"session_id": "sid"}) as c:
            return await c.post("/api/blob/seed-demo", data={"scenario": scn}, follow_redirects=False)
    r = asyncio.run(go("clean"))
    assert r.status_code == 303 and "notice=" in r.headers["location"], r.headers["location"]
    assert b"20000000" in fc.blobs["raw/corecap/corecap_accounts.csv"] or any(k.startswith("raw/") and k.endswith("corecap_accounts.csv") for k in fc.blobs)
    assert "error=" in asyncio.run(go("nope")).headers["location"]

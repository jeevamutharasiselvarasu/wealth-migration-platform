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

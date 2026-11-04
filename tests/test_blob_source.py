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

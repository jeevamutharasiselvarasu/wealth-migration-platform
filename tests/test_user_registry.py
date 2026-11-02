import asyncio
import sqlite3

import httpx
import pytest

from control_plane.services import user_registry as ur

PW = "correct-horse-battery"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_passwords_are_salted_scrypt_hashes_and_never_stored_in_clear():
    a, b = ur.hash_password(PW), ur.hash_password(PW)
    assert a != b and a.startswith("scrypt$") and PW not in a  # same password, different salt
    assert ur._check_password(PW, a) and not ur._check_password("wrong-password-1", a)
    assert not ur._check_password(PW, "garbage")


def test_registration_rules_pending_by_default_and_bootstrap_admin():
    c = _conn()
    for bad in (("A", "a@b.co", PW), ("Ann Lee", "not-an-email", PW), ("Ann Lee", "ann@b.co", "short"), ("Ann Lee", "ann@b.co", "aaaaaaaaaaaa"),
                ("Ann Lee", "annlee@b.co", "xx-annlee-xx")):
        with pytest.raises(ValueError):
            ur.register(c, *bad, set())
    first = ur.register(c, "Root User", "root@example.com", PW, set())
    assert first == {"status": "ACTIVE", "bootstrap": True} and ur.get_user(c, "root@example.com")["roles"] == ["Platform Admin"]
    second = ur.register(c, "Ann Lee", "Ann@Example.com", PW, set())
    assert second["status"] == "PENDING" and ur.get_user(c, "ann@example.com")["roles"] == []
    with pytest.raises(ValueError):
        ur.register(c, "Ann Again", "ann@example.com", PW, set())  # duplicate, case-insensitive
    with pytest.raises(ValueError):
        ur.register(c, "Demo Person", "demo@x.co", PW, {"demo@x.co"})  # reserved demo email
    d = _conn()
    assert ur.register(d, "Someone", "someone@example.com", PW, set(), bootstrap_admin_email="owner@example.com")["status"] == "PENDING"  # env set: first user is NOT admin
    assert ur.register(d, "Owner", "owner@example.com", PW, set(), bootstrap_admin_email="owner@example.com")["status"] == "ACTIVE"


def test_verify_states_lockout_and_no_enumeration():
    c = _conn()
    ur.register(c, "Root User", "root@example.com", PW, set())
    ur.register(c, "Ann Lee", "ann@example.com", PW, set())
    assert ur.verify(c, "nobody@example.com", PW) == {"error": "Invalid credentials provided"}
    assert ur.verify(c, "ann@example.com", "wrong-password-1") == {"error": "Invalid credentials provided"}  # wrong password reveals nothing
    assert "waiting" in ur.verify(c, "ann@example.com", PW)["error"]
    assert ur.verify(c, "root@example.com", PW)["user"]["name"] == "Root User" and "password_hash" in ur.verify(c, "root@example.com", PW)["user"]
    for _ in range(ur.MAX_FAILED):
        ur.verify(c, "root@example.com", "wrong-password-1")
    assert "Too many" in ur.verify(c, "root@example.com", PW)["error"]  # locked even with the right password


def test_decide_assigns_roles_and_protects_the_last_admin():
    c = _conn()
    ur.register(c, "Root User", "root@example.com", PW, set())
    ur.register(c, "Ann Lee", "ann@example.com", PW, set())
    with pytest.raises(ValueError):
        ur.decide(c, "root@example.com", "ann@example.com", "APPROVE", [])  # no roles
    with pytest.raises(ValueError):
        ur.decide(c, "root@example.com", "ann@example.com", "APPROVE", ["Not A Role"])
    ann = ur.decide(c, "root@example.com", "ann@example.com", "APPROVE", ["Migration Analyst", "Domain SME"])
    assert ann["status"] == "ACTIVE" and ann["default_role"] == "Migration Analyst" and ann["approved_by"] == "root@example.com"
    with pytest.raises(ValueError):
        ur.decide(c, "root@example.com", "root@example.com", "DISABLE")  # last active admin
    with pytest.raises(ValueError):
        ur.decide(c, "root@example.com", "root@example.com", "SET_ROLES", ["Migration Analyst"])  # cannot demote the last admin
    ur.decide(c, "root@example.com", "ann@example.com", "SET_ROLES", ["Platform Admin"])
    assert ur.decide(c, "ann@example.com", "root@example.com", "DISABLE")["status"] == "DISABLED"  # a second admin exists now
    assert "disabled" in ur.verify(c, "root@example.com", PW)["error"]
    assert ur.decide(c, "ann@example.com", "root@example.com", "ENABLE")["status"] == "ACTIVE"
    assert all("password_hash" not in u for u in ur.list_users(c))


def test_registration_and_approval_flow_through_the_real_routes(tmp_path, monkeypatch):
    from control_plane import db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore
    monkeypatch.delenv("XODUS_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "u.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    monkeypatch.setattr(app_module.session_mgr, "active_sessions", {})
    db.init_db()

    async def go(client, method, path, data=None):
        return await client.request(method, path, data=data, follow_redirects=False)

    async def scenario():
        t = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=t, base_url="http://testserver") as admin, httpx.AsyncClient(transport=t, base_url="http://testserver") as ann, \
                httpx.AsyncClient(transport=t, base_url="http://testserver") as anon:
            assert "Request an account" in (await go(anon, "GET", "/login")).text and (await go(anon, "GET", "/register")).status_code == 200
            f = {"name": "Root User", "email": "root@example.com", "password": PW, "confirm": PW}
            assert "error=" in (await go(anon, "POST", "/register", {**f, "confirm": "different-password"})).headers["location"]
            assert "Administrator+account+created" in (await go(anon, "POST", "/register", f)).headers["location"]
            assert "do+not+match" not in (await go(anon, "POST", "/register", {**f, "email": "bot@example.com", "website": "spam"})).headers["location"]  # honeypot: silently dropped
            assert (await go(anon, "POST", "/register", {"name": "Ann Lee", "email": "ann@example.com", "password": PW, "confirm": PW})).headers["location"].startswith("/register?notice=")
            assert "waiting" in (await go(ann, "POST", "/login", {"email": "ann@example.com", "password": PW})).headers["location"]
            assert "Invalid" in (await go(ann, "POST", "/login", {"email": "ann@example.com", "password": "wrong-password-9"})).headers["location"]
            assert "Invalid" in (await go(anon, "POST", "/login", {"email": "bot@example.com", "password": PW})).headers["location"]  # honeypot account was never created
            login = await go(admin, "POST", "/login", {"email": "root@example.com", "password": PW})
            assert login.headers["location"] == "/persona-selection"
            assert set(app_module.session_mgr.assigned_roles("root@example.com")) == set(app_module.session_mgr.ALL_ROLES)
            assert app_module.session_mgr.can_load_demo("root@example.com") is True  # bootstrap admin can load the demo data set
            page = await go(admin, "GET", "/admin/users")
            assert page.status_code == 200 and "ann@example.com" in page.text and "PENDING" in page.text and "password_hash" not in page.text and "scrypt" not in page.text
            await go(admin, "POST", "/persona-selection", {"role": "Platform Admin"})
            assert (await go(ann, "POST", "/api/users/ann@example.com/decision", {"action": "APPROVE", "roles": "Platform Admin"})).headers["location"] == "/login"  # not signed in
            r = await go(admin, "POST", "/api/users/ann@example.com/decision", {"action": "APPROVE", "roles": "Migration Analyst"})
            assert "notice=" in r.headers["location"] and "ACTIVE" in r.headers["location"]
            assert (await go(ann, "POST", "/login", {"email": "ann@example.com", "password": PW})).headers["location"] == "/persona-selection"
            assert app_module.session_mgr.assigned_roles("ann@example.com") == {"Migration Analyst"}
            assert app_module.session_mgr.can_load_demo("ann@example.com") is False
            await go(ann, "POST", "/persona-selection", {"role": "Migration Analyst"})
            assert (await go(ann, "POST", "/api/users/root@example.com/decision", {"action": "DISABLE"})).status_code in (303, 403)
            assert "root@example.com" in [u["email"] for u in __import__("control_plane.services.user_registry", fromlist=["x"]).list_users(db.get_db_connection())]
            assert "Only+a+Platform+Admin" in (await go(ann, "GET", "/admin/users")).headers["location"]
            gone = await go(admin, "POST", "/api/users/ann@example.com/decision", {"action": "DISABLE"})
            assert "DISABLED" in gone.headers["location"] and "ann@example.com" not in {s["email"] for s in app_module.session_mgr.active_sessions.values()}  # sessions ended
            assert "disabled" in (await go(ann, "POST", "/login", {"email": "ann@example.com", "password": PW})).headers["location"]
            assert "error=" in (await go(admin, "POST", "/api/users/root@example.com/decision", {"action": "DISABLE"})).headers["location"]  # last admin
    asyncio.run(scenario())
    reg = __import__("control_plane.services.user_registry", fromlist=["x"])
    conn = db.get_db_connection()
    assert reg.get_user(conn, "root@example.com")["status"] == "ACTIVE" and reg.get_user(conn, "ann@example.com")["status"] == "DISABLED"

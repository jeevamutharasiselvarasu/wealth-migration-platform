"""Self-registered users for the workbench: salted scrypt password hashes in the control-plane database.

Rules (security first, because the site can be public):
- Passwords are never stored or logged; only a salted scrypt hash is kept.
- A new registration is PENDING with no roles until a Platform Admin approves it and assigns roles.
  The only exception is the bootstrap administrator: the email in XODUS_BOOTSTRAP_ADMIN_EMAIL, or, when that is not set, the very
  first registration on an empty user table.
- Repeated wrong passwords lock the account for a while; unknown emails cost the same time as known ones.
- Emails of the built-in demo users are reserved and cannot be registered.
- The last active Platform Admin cannot be disabled or demoted.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

ALL_ROLES = ("Platform Admin", "IMO Lead", "Migration Analyst", "Domain SME", "Validation Specialist", "Compliance Reviewer")
MAX_PENDING = 50
MAX_FAILED = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_N, _R, _P = 2 ** 14, 8, 1

_DDL = """CREATE TABLE IF NOT EXISTS app_users (
    email TEXT PRIMARY KEY, name TEXT NOT NULL, password_hash TEXT NOT NULL, roles_json TEXT NOT NULL DEFAULT '[]',
    default_role TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'PENDING', can_load_demo INTEGER NOT NULL DEFAULT 0,
    failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until_utc TEXT, created_utc TEXT NOT NULL, approved_by TEXT, approved_utc TEXT)"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=len(digest_hex) // 2)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


_DUMMY = hash_password("not-a-real-password", b"\x00" * 16)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(cur: sqlite3.Cursor) -> Optional[Dict[str, Any]]:
    r = cur.fetchone()
    if not r:
        return None
    d = dict(zip([c[0] for c in cur.description], r))
    d["roles"] = json.loads(d.pop("roles_json") or "[]")
    return d


def get_user(conn: sqlite3.Connection, email: str) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    return _row(conn.execute("SELECT * FROM app_users WHERE email = ?", (email.strip().lower(),)))


def _active_admins(conn: sqlite3.Connection) -> int:
    return sum(1 for u in list_users(conn) if u["status"] == "ACTIVE" and "Platform Admin" in u["roles"])


def validate_registration(name: str, email: str, password: str, confirm: Optional[str] = None) -> Optional[str]:
    name, email = " ".join(str(name or "").split()), str(email or "").strip()
    if not 2 <= len(name) <= 80:
        return "Enter your name (2 to 80 characters)."
    if not _EMAIL.match(email) or len(email) > 120:
        return "Enter a valid email address."
    if confirm is not None and password != confirm:
        return "The two passwords do not match."
    if len(password) < MIN_PASSWORD:
        return f"Choose a password of at least {MIN_PASSWORD} characters."
    if len(set(password)) < 4 or password.lower() == email.lower() or email.split("@")[0].lower() in password.lower() and len(email.split("@")[0]) > 3:
        return "Choose a less guessable password (not your email or a repeated character)."
    return None


def register(conn: sqlite3.Connection, name: str, email: str, password: str, reserved_emails: Set[str],
             bootstrap_admin_email: str = "") -> Dict[str, Any]:
    """Create an account. Returns {'status': 'ACTIVE'|'PENDING', 'bootstrap': bool}. Raises ValueError with a user-safe message."""
    ensure_table(conn)
    problem = validate_registration(name, email, password)
    if problem:
        raise ValueError(problem)
    email = email.strip().lower()
    name = " ".join(name.split())
    bootstrap_email = (bootstrap_admin_email or "").strip().lower()
    # Built-in demo emails are reserved, except the one the host owner names as the bootstrap administrator (environment variable).
    reserved = {e.lower() for e in reserved_emails} - ({bootstrap_email} if bootstrap_email else set())
    if email in reserved or get_user(conn, email):
        raise ValueError("That email cannot be registered. If it is yours, sign in or ask an administrator.")
    total = conn.execute("SELECT COUNT(*) FROM app_users").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM app_users WHERE status = 'PENDING'").fetchone()[0]
    bootstrap = (email == bootstrap_email) if bootstrap_email else total == 0
    if not bootstrap and pending >= MAX_PENDING:
        raise ValueError("Registration is full for now. Ask an administrator to clear pending requests.")
    roles = ["Platform Admin"] if bootstrap else []
    conn.execute("INSERT INTO app_users (email, name, password_hash, roles_json, default_role, status, can_load_demo, created_utc, approved_by, approved_utc) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (email, name, hash_password(password), json.dumps(roles), roles[0] if roles else "", "ACTIVE" if bootstrap else "PENDING",
                  1 if bootstrap else 0, _now().isoformat(timespec="seconds"), "bootstrap" if bootstrap else None, _now().isoformat(timespec="seconds") if bootstrap else None))
    return {"status": "ACTIVE" if bootstrap else "PENDING", "bootstrap": bootstrap}


def verify(conn: sqlite3.Connection, email: str, password: str) -> Dict[str, Any]:
    """Returns {'user': {...}} on success or {'error': message}. Messages never reveal whether an email exists before the password is right."""
    ensure_table(conn)
    user = get_user(conn, email)
    generic = {"error": "Invalid credentials provided"}
    if not user:
        _check_password(password, _DUMMY)
        return generic
    locked = user["locked_until_utc"]
    if locked and datetime.fromisoformat(locked) > _now():
        _check_password(password, _DUMMY)
        return {"error": "Too many attempts. Try again in a few minutes."}
    if not _check_password(password, user["password_hash"]):
        failed = user["failed_attempts"] + 1
        until = (_now() + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds") if failed >= MAX_FAILED else None
        conn.execute("UPDATE app_users SET failed_attempts = ?, locked_until_utc = ? WHERE email = ?", (0 if until else failed, until, user["email"]))
        return generic
    conn.execute("UPDATE app_users SET failed_attempts = 0, locked_until_utc = NULL WHERE email = ?", (user["email"],))
    if user["status"] == "PENDING":
        return {"error": "Your registration is waiting for an administrator to approve it."}
    if user["status"] != "ACTIVE":
        return {"error": "This account is disabled. Ask an administrator."}
    return {"user": user}


def list_users(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_table(conn)
    cur = conn.execute("SELECT * FROM app_users ORDER BY CASE status WHEN 'PENDING' THEN 0 WHEN 'ACTIVE' THEN 1 ELSE 2 END, created_utc")
    out = []
    for r in cur.fetchall():
        d = dict(zip([c[0] for c in cur.description], r))
        d["roles"] = json.loads(d.pop("roles_json") or "[]")
        d.pop("password_hash", None)
        out.append(d)
    return out


def decide(conn: sqlite3.Connection, actor_email: str, target_email: str, action: str, roles: Optional[List[str]] = None) -> Dict[str, Any]:
    """APPROVE (needs at least one role), SET_ROLES, DISABLE or ENABLE. The last active Platform Admin is protected."""
    ensure_table(conn)
    user = get_user(conn, target_email)
    if not user:
        raise ValueError("User not found.")
    action = action.strip().upper()
    roles = [r for r in (roles or []) if r in ALL_ROLES]
    admins_after = _active_admins(conn) - (1 if user["status"] == "ACTIVE" and "Platform Admin" in user["roles"] else 0)
    now = _now().isoformat(timespec="seconds")
    if action in {"APPROVE", "SET_ROLES"}:
        if not roles:
            raise ValueError("Pick at least one role.")
        if user["status"] == "DISABLED":
            raise ValueError("Enable the account before changing its roles.")
        if action == "SET_ROLES" and user["status"] != "ACTIVE":
            raise ValueError("Approve the account first.")
        if action == "APPROVE" and user["status"] != "PENDING":
            raise ValueError("This account is not waiting for approval.")
        if "Platform Admin" in user["roles"] and "Platform Admin" not in roles and admins_after < 1:
            raise ValueError("At least one active Platform Admin must remain.")
        default = "Platform Admin" if "Platform Admin" in roles else roles[0]
        conn.execute("UPDATE app_users SET roles_json = ?, default_role = ?, status = 'ACTIVE', approved_by = ?, approved_utc = ? WHERE email = ?",
                     (json.dumps(roles), default, actor_email, now, user["email"]))
    elif action == "DISABLE":
        if user["status"] == "ACTIVE" and "Platform Admin" in user["roles"] and admins_after < 1:
            raise ValueError("At least one active Platform Admin must remain.")
        conn.execute("UPDATE app_users SET status = 'DISABLED', approved_by = ?, approved_utc = ? WHERE email = ?", (actor_email, now, user["email"]))
    elif action == "ENABLE":
        if user["status"] != "DISABLED":
            raise ValueError("This account is not disabled.")
        conn.execute("UPDATE app_users SET status = ? WHERE email = ?", ("ACTIVE" if user["roles"] else "PENDING", user["email"]))
    else:
        raise ValueError("Unknown action.")
    return get_user(conn, user["email"])


def bootstrap_admin_email() -> str:
    return os.getenv("XODUS_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()

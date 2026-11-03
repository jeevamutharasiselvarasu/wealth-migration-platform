import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path
from typing import Dict, Optional

from fastapi import Request

from control_plane import db as _cp_db
from control_plane.services import user_registry


class SessionManager:
    """Manages local demo sessions and role permissions.

    Demo accounts (email, name, roles) are data-driven from demo_data/demo_users.json.
    Password hashes are never committed: they come from the git-ignored file
    demo_data/demo_passwords.local.json or the XODUS_DEMO_PASSWORD_HASHES environment
    variable (JSON map of email to SHA-256 hex). Accounts without a hash cannot sign in. Disable demo
    authentication outside local/demo environments with XODUS_DEMO_MODE=false.
    Production deployments should replace this provider with the organisation's
    approved identity provider.
    """

    DEMO_DIR = Path(__file__).resolve().parents[1] / "demo_data"

    def __init__(self):
        self.active_sessions: Dict[str, Dict[str, str]] = {}
        self.last_error = "Invalid credentials provided"
        self.demo_mode = os.getenv("XODUS_DEMO_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.authorized_users = {}
        if self.demo_mode:
            demo_dir = self.DEMO_DIR
            local_hashes = self._load_local_password_hashes(demo_dir / "demo_passwords.local.json")
            try:
                # demo_users.json is the tracked, reviewed list. demo_users.local.json (git-ignored) may add extra rehearsal
                # accounts on this machine only; it can never replace a tracked user.
                for name, extra in (("demo_users.json", False), ("demo_users.local.json", True)):
                    path = demo_dir / name
                    if extra and not path.exists():
                        continue
                    config = json.loads(path.read_text(encoding="utf-8"))
                    if not config.get("demo_mode", extra):
                        continue
                    for item in config.get("users", []):
                        email = str(item.get("email", "")).strip().lower()
                        if not email or (extra and email in self.authorized_users):
                            continue
                        self.authorized_users[email] = {
                            "password_sha256": local_hashes.get(email, ""),
                            "name": str(item.get("name", email)),
                            "default_role": str(item.get("default_role", "Migration Analyst")),
                            "roles": [str(r) for r in item.get("roles", [])] or [str(item.get("default_role", "Migration Analyst"))],
                            "can_load_demo": bool(item.get("can_load_demo", False)) and not extra,
                        }
            except (OSError, ValueError, TypeError):
                # A missing or invalid demo-user configuration fails closed.
                self.authorized_users = {}

    @staticmethod
    def _load_local_password_hashes(local_file: Path) -> Dict[str, str]:
        """Read email -> SHA-256 hex from the environment or the git-ignored local file."""
        raw = os.getenv("XODUS_DEMO_PASSWORD_HASHES", "").strip()
        try:
            data = json.loads(raw) if raw else (json.loads(local_file.read_text(encoding="utf-8")) if local_file.exists() else {})
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k).strip().lower(): str(v).strip().lower() for k, v in data.items()}

    def _registry_user(self, email: str) -> Optional[dict]:
        """Active self-registered user (control-plane database), or None. Failures to read the database fail closed."""
        try:
            conn = _cp_db.get_db_connection()
            try:
                user = user_registry.get_user(conn, email)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            return None
        return user if user and user["status"] == "ACTIVE" else None

    def default_role_for(self, email: str) -> str:
        email = email.strip().lower()
        if email in self.authorized_users:
            return self.authorized_users[email]["default_role"]
        reg = self._registry_user(email)
        return (reg or {}).get("default_role") or "Migration Analyst"

    def authenticate(self, email: str, password: str, role: str) -> Optional[str]:
        """Returns a session id, or None. self.last_error carries a user-safe reason for the login page."""
        self.last_error = "Invalid credentials provided"
        email = email.strip().lower()
        user = self.authorized_users.get(email)
        supplied_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        name = None
        if user and len(user["password_sha256"]) == 64 and hmac.compare_digest(user["password_sha256"], supplied_hash):
            name = user["name"]
        elif not user or not user["password_sha256"]:
            conn = _cp_db.get_db_connection()
            try:
                result = user_registry.verify(conn, email, password)
                conn.commit()
            finally:
                conn.close()
            if "user" in result:
                name, role = result["user"]["name"], result["user"]["default_role"] or role
            else:
                self.last_error = result["error"]
        if name is None:
            return None
        session_id = str(uuid.uuid4())
        self.active_sessions[session_id] = {"email": email, "name": name, "role": role}
        return session_id

    ALL_ROLES = ("Platform Admin", "IMO Lead", "Migration Analyst", "Domain SME", "Validation Specialist", "Compliance Reviewer")

    def can_load_demo(self, email: str) -> bool:
        """One-click demo load is granted per account in demo_data/demo_users.json (never by role alone)."""
        email = str(email or "").strip().lower()
        if self.demo_mode and self.authorized_users.get(email, {}).get("can_load_demo"):
            return True
        reg = self._registry_user(email) if email and email not in self.authorized_users else None
        return bool(self.demo_mode and reg and reg.get("can_load_demo"))

    def assigned_roles(self, email: str) -> set:
        """Personas a user may act under: Platform Admin holds all; others hold their configured roles."""
        email = email.strip().lower()
        user = self.authorized_users.get(email) or (self._registry_user(email) or {})
        roles = set(user.get("roles", []))
        if user.get("default_role") == "Platform Admin" or "Platform Admin" in roles:
            return set(self.ALL_ROLES)
        return roles & set(self.ALL_ROLES)

    def select_role(self, session_id: str, role: str) -> bool:
        if role not in self.ALL_ROLES or session_id not in self.active_sessions:
            return False
        self.active_sessions[session_id]["role"] = role
        return True

    def get_session(self, request: Request) -> Optional[Dict[str, str]]:
        session_id = request.cookies.get("session_id")
        if session_id and session_id in self.active_sessions:
            return self.active_sessions[session_id]
        return None

    def logout(self, session_id: str):
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]


session_mgr = SessionManager()

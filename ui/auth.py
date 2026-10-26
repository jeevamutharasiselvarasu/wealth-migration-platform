import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path
from typing import Dict, Optional

from fastapi import Request


class SessionManager:
    """Manages local demo sessions and role permissions.

    Demo accounts are data-driven from demo_data/demo_users.json. Disable demo
    authentication outside local/demo environments with XODUS_DEMO_MODE=false.
    Production deployments should replace this provider with the organisation's
    approved identity provider.
    """

    def __init__(self):
        self.active_sessions: Dict[str, Dict[str, str]] = {}
        self.demo_mode = os.getenv("XODUS_DEMO_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.authorized_users = {}
        if self.demo_mode:
            config_path = Path(__file__).resolve().parents[1] / "demo_data" / "demo_users.json"
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if config.get("demo_mode", False):
                    for item in config.get("users", []):
                        email = str(item.get("email", "")).strip().lower()
                        password_hash = str(item.get("password_sha256", "")).strip().lower()
                        if email and len(password_hash) == 64:
                            self.authorized_users[email] = {
                                "password_sha256": password_hash,
                                "name": str(item.get("name", email)),
                                "default_role": str(item.get("default_role", "Migration Analyst")),
                                "roles": [str(r) for r in item.get("roles", [])] or [str(item.get("default_role", "Migration Analyst"))],
                            }
            except (OSError, ValueError, TypeError):
                # A missing or invalid demo-user configuration fails closed.
                self.authorized_users = {}

    def authenticate(self, email: str, password: str, role: str) -> Optional[str]:
        user = self.authorized_users.get(email.strip().lower())
        supplied_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        if user and hmac.compare_digest(user["password_sha256"], supplied_hash):
            session_id = str(uuid.uuid4())
            self.active_sessions[session_id] = {
                "email": email.strip().lower(),
                "name": user["name"],
                "role": role,
            }
            return session_id
        return None

    ALL_ROLES = ("Platform Admin", "Migration Analyst", "Domain SME", "Validation Specialist")

    def assigned_roles(self, email: str) -> set:
        """Personas a user may act under: Platform Admin holds all; others hold their configured roles."""
        user = self.authorized_users.get(email.strip().lower(), {})
        roles = set(user.get("roles", []))
        if user.get("default_role") == "Platform Admin" or "Platform Admin" in roles:
            return set(self.ALL_ROLES)
        return roles & set(self.ALL_ROLES)

    def select_role(self, session_id: str, role: str) -> bool:
        allowed_roles = {"Platform Admin", "Migration Analyst", "Domain SME", "Validation Specialist"}
        if role not in allowed_roles or session_id not in self.active_sessions:
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

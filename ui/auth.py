import uuid
from typing import Dict, Optional
from fastapi import Request, HTTPException, status

class SessionManager:
    """Manages active user sessions, role permissions, and logins across RBAC personas."""

    def __init__(self):
        self.active_sessions: Dict[str, Dict[str, str]] = {}
        # Pre-seeded authorized users for the Migration Workbench
        self.authorized_users = {
            "jeeva@simplicitywealth.com": {
                "password": "password123",
                "name": "Jeeva S.",
                "default_role": "Platform Admin"
            },
            "analyst@simplicitywealth.com": {
                "password": "password123",
                "name": "Alex Migration Analyst",
                "default_role": "Migration Analyst"
            },
            "sme@simplicitywealth.com": {
                "password": "password123",
                "name": "Sarah Domain SME",
                "default_role": "Domain SME"
            },
            "specialist@simplicitywealth.com": {
                "password": "password123",
                "name": "Marcus Validation Specialist",
                "default_role": "Validation Specialist"
            }
        }

    def authenticate(self, email: str, password: str, role: str) -> Optional[str]:
        user = self.authorized_users.get(email.lower())
        if user and user["password"] == password:
            session_id = str(uuid.uuid4())
            self.active_sessions[session_id] = {
                "email": email,
                "name": user["name"],
                "role": role
            }
            return session_id
        return None

    def get_session(self, request: Request) -> Optional[Dict[str, str]]:
        session_id = request.cookies.get("session_id")
        if session_id and session_id in self.active_sessions:
            return self.active_sessions[session_id]
        return None

    def logout(self, session_id: str):
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]

session_mgr = SessionManager()
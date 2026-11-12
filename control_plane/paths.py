"""Where the platform's editable knowledge lives. A deployment for another firm, source or target points these at its own folders
(XODUS_PLATFORM_KNOWLEDGE_DIR, XODUS_KNOWLEDGE_DIR) and the firm configuration (XODUS_FIRM_CONFIG); no code changes."""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def platform_knowledge_dir() -> Path:
    return Path(os.environ.get("XODUS_PLATFORM_KNOWLEDGE_DIR") or REPO / "platform_knowledge")


def knowledge_dir() -> Path:
    return Path(os.environ.get("XODUS_KNOWLEDGE_DIR") or REPO / "knowledge")

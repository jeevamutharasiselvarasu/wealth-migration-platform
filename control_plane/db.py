import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path("App_Data/controlplane.db")


def get_db_connection() -> sqlite3.Connection:
    """Provides a thread-safe connection to the Control Plane database[cite: 78, 80]."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Creates control-plane metadata tables using idempotent DDL[cite: 78, 80]."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Systems Inventory
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS systems (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            description TEXT,
            owner TEXT,
            created_utc TEXT NOT NULL
        );
    """)

    # Migrations Boundary
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS migrations (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            category TEXT,
            description TEXT,
            source_system TEXT,
            target_system TEXT,
            entities_json TEXT,
            template_id TEXT,
            created_by TEXT,
            created_utc TEXT NOT NULL
        );
    """)

    # Migration Executable Rulebook Overrides
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS migration_rulebooks (
            migration_id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            updated_by TEXT,
            updated_utc TEXT NOT NULL,
            FOREIGN KEY (migration_id) REFERENCES migrations (id)
        );
    """)

    # Knowledge Base Assets
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_assets (
            id TEXT PRIMARY KEY,
            system_id TEXT,
            migration_id TEXT,
            asset_type TEXT NOT NULL,
            file_name TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER,
            sha256 TEXT,
            version INTEGER DEFAULT 1,
            created_utc TEXT NOT NULL
        );
    """)

    # Pipeline Runs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            source_system_id TEXT,
            target_system_id TEXT,
            migration_id TEXT NOT NULL,
            status TEXT NOT NULL,
            canonical_model_version TEXT,
            created_by TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        );
    """)

    # Input Files & Checksums
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS run_input_files (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            file_order INTEGER,
            file_name TEXT NOT NULL,
            entity_name TEXT,
            dataset_id TEXT,
            processing_id TEXT,
            feed_instance_id TEXT,
            relative_path TEXT NOT NULL,
            content_type TEXT,
            layer TEXT DEFAULT 'RAW',
            size_bytes INTEGER,
            sha256 TEXT,
            created_utc TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs (id)
        );
    """)

    # Transformation Artifacts
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_name TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            sha256 TEXT,
            version INTEGER DEFAULT 1,
            status TEXT DEFAULT 'DRAFT',
            created_utc TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs (id)
        );
    """)

    # Stage Approvals & Audit Trail
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            artifact_id TEXT,
            stage TEXT NOT NULL,
            decision TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            comments TEXT,
            created_utc TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs (id)
        );
    """)

    conn.commit()
    conn.close()
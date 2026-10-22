import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path("App_Data/controlplane.db")


def get_db_connection() -> sqlite3.Connection:
    """Provides a thread-safe connection to the Control Plane database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Creates control-plane metadata tables using idempotent DDL."""
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

    # 1. Migration Run Execution Summaries
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS migration_runs (
            run_id TEXT PRIMARY KEY,
            migration_id TEXT NOT NULL,
            wave_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            source_record_count INTEGER,
            mapped_record_count INTEGER,
            confidence_score REAL,
            status TEXT NOT NULL,
            executed_by TEXT NOT NULL,
            executed_utc TEXT NOT NULL
        );
    """)

    # 2. Source File Profiling Metadata
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS source_file_profiles (
            profile_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            file_name TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            column_count INTEGER NOT NULL,
            profile_summary_json TEXT NOT NULL,
            created_utc TEXT NOT NULL
        );
    """)

    # 3. Canonical Field Mapping Rules
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS canonical_mappings (
            mapping_id TEXT PRIMARY KEY,
            migration_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            source_field TEXT NOT NULL,
            canonical_field TEXT NOT NULL,
            mapping_type TEXT NOT NULL,
            transformation_expression TEXT,
            confidence_score REAL NOT NULL,
            is_confirmed INTEGER DEFAULT 0,
            created_by TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        );
    """)

    # Mapping proposals are persisted as unapproved until a reviewer explicitly approves them.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mapping_suggestions (
            suggestion_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            migration_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            source_field TEXT NOT NULL,
            canonical_field TEXT,
            proposed_rule TEXT,
            confidence_score REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'PENDING',
            reviewer TEXT,
            review_comments TEXT,
            created_utc TEXT NOT NULL,
            reviewed_utc TEXT
        );
    """)

    # 4. Field Lineage Audit Trail
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS field_lineage_audit (
            lineage_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            source_column TEXT NOT NULL,
            canonical_column TEXT,
            applied_rule TEXT,
            validation_status TEXT NOT NULL,
            actor TEXT NOT NULL,
            timestamp_utc TEXT NOT NULL
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
            created_utc TEXT NOT NULL
        );
    """)

    # MCL batch execution metadata; summaries only, never mapped source records.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mcl_execution_runs (
            mcl_run_id TEXT PRIMARY KEY,
            migration_id TEXT NOT NULL,
            status TEXT NOT NULL,
            entity_summary_json TEXT NOT NULL,
            relationship_summary_json TEXT NOT NULL,
            execution_boundary_json TEXT NOT NULL,
            executed_utc TEXT NOT NULL
        );
    """)

    # Durable information requests; UI state is a projection of this table.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mapping_information_requests (
            request_id TEXT PRIMARY KEY,
            migration_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            canonical_field TEXT NOT NULL,
            source_file TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN',
            question TEXT NOT NULL,
            requested_by TEXT,
            created_utc TEXT NOT NULL,
            resolved_utc TEXT,
            resolution TEXT,
            response TEXT NOT NULL DEFAULT '',
            UNIQUE (migration_id, entity_name, canonical_field)
        );
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mapping_information_requests_status
        ON mapping_information_requests (migration_id, status, entity_name)
    """)

    conn.commit()
    conn.close()
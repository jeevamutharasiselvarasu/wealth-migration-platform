"""Promotion gate: the validation sign-off, recorded where the promotion pipeline can check it.

When the Validation Specialist signs off a batch, XODUS counts the batch's rows in each mirror table and writes one row to
xodus_ops.xodus_promotion_gate (the only table XODUS writes in the XODUS database). PL_XODUS_Promote refuses to run unless that row
exists and the mirror still holds exactly those counts, so a batch that changed after sign-off can never reach the live tables.
"""
import json
import sqlite3
from typing import Any, Callable, Dict, Optional

from control_plane.services import sql_target

MIRROR = "xodus_mirror"


def _mirror_counts(cfg: Dict[str, Any], manifest: Dict[str, Any], batch_id: str, connect) -> Dict[str, int]:
    tables = [t["mirror_table"] for t in manifest["liveTables"]]
    for t in tables:
        if not sql_target.SCHEMA.match(t):
            raise ValueError(f"Unsafe table name {t}.")
    rows = sql_target.run(cfg, [(f"SELECT COUNT_BIG(*) FROM [{MIRROR}].[{t}] WHERE xodus_batch_id = %s", (batch_id,)) for t in tables], connect)
    return {t: int(r[0][0]) for t, r in zip(tables, rows)}


def record(conn: sqlite3.Connection, migration_id: str, batch_id: str, validation_run_id: str, signed_by: str, signed_utc: str,
           manifest: Dict[str, Any], connect: Callable[..., Any] = sql_target._default_connect) -> Dict[str, Any]:
    cfg = sql_target.get_config(conn)
    counts = _mirror_counts(cfg, manifest, batch_id, connect)
    if not any(counts.values()):
        raise ValueError("The mirror holds no rows for this batch, so there is nothing to promote.")
    sql_target.run(cfg, [("INSERT INTO xodus_ops.xodus_promotion_gate (migration_id, batch_id, validation_run_id, signed_off_by, signed_off_utc, counts_json) "
                          "VALUES (%s, %s, %s, %s, %s, %s)", (migration_id, batch_id, validation_run_id, signed_by, signed_utc, json.dumps(counts)))],
                   connect, commit=True)
    return {"batch_id": batch_id, "counts": counts}


def status(conn: sqlite3.Connection, migration_id: str, batch_id: str, connect: Callable[..., Any] = sql_target._default_connect) -> Optional[Dict[str, Any]]:
    """The latest gate row for the batch and the latest promotion of it, read from the XODUS database. None when nothing is recorded."""
    cfg = sql_target.get_config(conn)
    gate, promo = sql_target.run(cfg, [
        ("SELECT TOP (1) signed_off_by, signed_off_utc, counts_json, validation_run_id FROM xodus_ops.xodus_promotion_gate "
         "WHERE migration_id = %s AND batch_id = %s ORDER BY gate_id DESC", (migration_id, batch_id)),
        ("SELECT TOP (1) status, detail, logged_utc, run_id FROM xodus_ops.xodus_promotion_log WHERE migration_id = %s AND batch_id = %s "
         "ORDER BY promotion_id DESC", (migration_id, batch_id))], connect)
    if not gate:
        return None
    out = {"signed_off_by": gate[0][0], "signed_off_utc": gate[0][1], "counts": json.loads(gate[0][2]), "validation_run_id": gate[0][3], "promotion": None}
    if promo:
        try:
            rows = json.loads(promo[0][1] or "[]")
        except ValueError:
            rows = []
        out["promotion"] = {"status": promo[0][0], "tables": rows, "logged_utc": str(promo[0][2]), "run_id": promo[0][3]}
    return out

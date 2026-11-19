"""Wave validation as the application shows it: what the pipeline's SQL wrote after each wave, read from the XODUS database.

The blocking checks run inside Data Factory (`usp_validate_<slug>`); they write one finding per check and entity to
xodus_ops.xodus_validation_result and the keys (never other values) of failing rows to xodus_validation_row_key. This module only reads
those tables, groups them by wave and entity next to the entity's build state, and records an approved exception as a waiver
(xodus_ops.xodus_wave_waiver, the one table besides the promotion gate that the platform may write). The validation procedure marks the
finding WAIVED on its next run, so the wave gate passes; the run is then restarted from that wave.
"""
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from control_plane.services import sql_target

REF_PREFIX = "WAVE"
RUN_PREFIX = "WAVE:"  # run_id of an exception raised against a batch's wave finding (validation_exceptions.run_id)


def check_ref(entity: str, check: str, table: str, field: str) -> str:
    """The stable name of a finding: what an exception and a waiver are attached to."""
    return "|".join((REF_PREFIX, str(entity), str(check), str(table or ""), str(field or "")))


def parse_ref(ref: str) -> Optional[Dict[str, str]]:
    parts = str(ref or "").split("|", 4)
    if len(parts) != 5 or parts[0] != REF_PREFIX or not parts[1] or not parts[2]:
        return None
    return {"entity": parts[1], "check": parts[2], "table": parts[3], "field": parts[4]}


def _entity_status(state: Optional[Dict[str, Any]]) -> str:
    return (state or {}).get("status") or "NOT_BUILT"


def assemble(batch_id: str, registry: List[Dict[str, Any]], states: List[Dict[str, Any]], results: List[Dict[str, Any]],
             keys: List[Dict[str, Any]], waivers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The board: waves in order, each with its entities, their build state and findings. A wave passes when every entity is VALIDATED."""
    state_of = {s["entity"]: s for s in states}
    keys_of: Dict[Any, List[str]] = {}
    for k in keys:  # "key (file row n)" when the key could be traced to the source row it came from
        where = f" ({str(k['source_file']).rsplit('/', 1)[-1]} row {k['source_row']})" if k.get("source_file") and k.get("source_row") is not None else ""
        keys_of.setdefault(k["result_id"], []).append(str(k["row_key"]) + where)
    waiver_of = {check_ref(w["entity"], w["check_name"], w["table_name"], w["field_name"]): w for w in waivers}
    findings: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        ref = check_ref(r["entity"], r["check_name"], r["table_name"], r["field_name"])
        findings.setdefault(r["entity"], []).append({
            "result_id": r["result_id"], "check": r["check_name"], "table": r["table_name"], "field": r["field_name"], "status": r["status"],
            "observed": r.get("observed_count"), "expected": r.get("expected_count"), "detail": r.get("detail") or "", "source": r.get("source") or "BUILTIN",
            "executed_utc": str(r.get("executed_utc") or "")[:19], "ref": ref, "row_keys": keys_of.get(r["result_id"], []), "waiver": waiver_of.get(ref)})
    by_wave: Dict[int, List[Dict[str, Any]]] = {}
    for reg in registry:
        entity = reg["entity"]
        fs = findings.get(entity, [])
        counts = {s: sum(1 for f in fs if f["status"] == s) for s in ("PASS", "FAIL", "WARN", "WAIVED")}
        st = state_of.get(entity)
        by_wave.setdefault(int(reg["wave"]), []).append({
            "entity": entity, "depends_on": [d for d in str(reg.get("depends_on") or "").split(",") if d], "state": st, "status": _entity_status(st),
            "checks": fs, "counts": counts})
    waves = []
    for number in sorted(by_wave):
        ents = by_wave[number]
        statuses = {e["status"] for e in ents}
        status = ("FAIL" if "FAILED" in statuses else "STALE" if "STALE" in statuses else "PASS" if statuses == {"VALIDATED"}
                  else "PENDING")
        waves.append({"wave": number, "status": status, "entities": ents,
                      "findings": {s: sum(e["counts"][s] for e in ents) for s in ("PASS", "FAIL", "WARN", "WAIVED")}})
    return {"batch_id": batch_id, "waves": waves, "summary": {s: sum(1 for w in waves if w["status"] == s) for s in ("PASS", "FAIL", "STALE", "PENDING")},
            "built": any(e["state"] for w in waves for e in w["entities"]), "findings": sum(len(v) for v in findings.values())}


def _rows(rows: Any, names: List[str]) -> List[Dict[str, Any]]:
    return [dict(zip(names, r)) for r in rows or []]


def read_board(conn: sqlite3.Connection, migration_slug: str, migration_id: str, batch_id: str,
               connect: Callable[..., Any] = sql_target._default_connect) -> Dict[str, Any]:
    """Read the registry, entity states, findings, failing keys and waivers of one batch (read-only). Raises ValueError with a readable reason."""
    try:
        return _read_board(conn, migration_slug, migration_id, batch_id, connect)
    except ValueError as exc:
        if "Invalid object name" in str(exc) and "xodus_ops" in str(exc):  # the XODUS database predates this package's wave tables
            raise ValueError("This package was built before validation by wave, so the XODUS database has no wave tables yet. Generate a new package (stage 07), "
                             "merge its pull request, publish it and run the setup pipeline (stage 08); the next batch then validates wave by wave.") from exc
        raise


def _read_board(conn: sqlite3.Connection, migration_slug: str, migration_id: str, batch_id: str, connect: Callable[..., Any]) -> Dict[str, Any]:
    cfg = sql_target.get_config(conn)
    reg, st, res, keys, wv = sql_target.run(cfg, [
        ("SELECT [entity], [wave], [depends_on] FROM xodus_ops.xodus_entity_registry WHERE migration_slug = %s ORDER BY [wave], [entity]", (migration_slug,)),
        ("SELECT [entity], [stage], [status], [rows_written], [last_run_id], [package_version], [updated_utc] FROM xodus_ops.xodus_entity_state "
         "WHERE migration_id = %s AND batch_id = %s", (migration_id, batch_id)),
        ("SELECT [result_id], [wave], [entity], [check_name], [table_name], [field_name], [status], [observed_count], [expected_count], [detail], [source], [executed_utc] "
         "FROM xodus_ops.xodus_validation_result WHERE migration_id = %s AND batch_id = %s ORDER BY [wave], [entity], [result_id]", (migration_id, batch_id)),
        ("SELECT k.[result_id], k.[row_key], k.[source_file], k.[source_row] FROM xodus_ops.xodus_validation_row_key AS k JOIN xodus_ops.xodus_validation_result AS r ON r.[result_id] = k.[result_id] "
         "WHERE r.migration_id = %s AND r.batch_id = %s ORDER BY k.[key_id]", (migration_id, batch_id)),
        ("SELECT [entity], [check_name], [table_name], [field_name], [reason], [approved_by], [written_utc] FROM xodus_ops.xodus_wave_waiver "
         "WHERE migration_id = %s AND batch_id = %s", (migration_id, batch_id))], connect)
    return assemble(batch_id, _rows(reg, ["entity", "wave", "depends_on"]),
                    _rows(st, ["entity", "stage", "status", "rows_written", "last_run_id", "package_version", "updated_utc"]),
                    _rows(res, ["result_id", "wave", "entity", "check_name", "table_name", "field_name", "status", "observed_count", "expected_count", "detail", "source", "executed_utc"]),
                    _rows(keys, ["result_id", "row_key", "source_file", "source_row"]),
                    _rows(wv, ["entity", "check_name", "table_name", "field_name", "reason", "approved_by", "written_utc"]))


def failing_finding(board: Dict[str, Any], ref: str) -> Optional[Dict[str, Any]]:
    """The finding with this reference when it is currently a failure (the only kind an exception can cover)."""
    for w in board["waves"]:
        for e in w["entities"]:
            for c in e["checks"]:
                if c["ref"] == ref and c["status"] == "FAIL":
                    return {**c, "entity": e["entity"], "wave": w["wave"]}
    return None


def record_waiver(conn: sqlite3.Connection, migration_id: str, batch_id: str, ref: str, reason: str, approved_by: str,
                  connect: Callable[..., Any] = sql_target._default_connect) -> Dict[str, str]:
    """Write the approved exception where the validation procedure reads it. The caller has checked that a different person approved it."""
    parsed = parse_ref(ref)
    if not parsed:
        raise ValueError("That is not a wave finding.")
    if not str(reason or "").strip() or not str(approved_by or "").strip():
        raise ValueError("A waiver needs a reason and an approver.")
    cfg = sql_target.get_config(conn)
    sql_target.run(cfg, [("INSERT INTO xodus_ops.xodus_wave_waiver (migration_id, batch_id, [entity], check_name, table_name, field_name, reason, approved_by) "
                          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                          (migration_id, batch_id, parsed["entity"], parsed["check"], parsed["table"], parsed["field"], str(reason).strip()[:1000], approved_by[:256]))],
                   connect, commit=True)
    return parsed

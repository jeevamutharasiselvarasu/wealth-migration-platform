import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from control_plane.services import run_monitor as rm
from control_plane.services import sql_target as st


def row(i, step, status, run="R1", msg=None, rr=None, rw=None, ts="2026-11-06T10:00:00+00:00"):
    return {"log_id": i, "pipeline_run_id": run, "trigger_file": "x.ready", "step_name": step, "status": status, "rows_read": rr, "rows_written": rw,
            "message": msg, "logged_utc": ts}


def newest_first(*rows):
    return sorted(rows, key=lambda r: r["log_id"], reverse=True)


def test_no_rows_means_no_run_and_started_only_means_running():
    assert rm.summarise([])["status"] == "NO_RUN"
    now = datetime(2026, 11, 6, 10, 5, tzinfo=timezone.utc)
    out = rm.summarise(newest_first(row(1, "Pipeline", "STARTED")), now)
    assert out["status"] == "RUNNING" and out["run_id"] == "R1" and out["finished_utc"] is None


def test_a_run_with_no_end_row_for_an_hour_is_stalled_not_running():
    now = datetime(2026, 11, 6, 11, 30, tzinfo=timezone.utc)
    assert rm.summarise(newest_first(row(1, "Pipeline", "STARTED")), now)["status"] == "STALLED"


def test_success_requires_the_pipeline_end_row_and_only_the_latest_run_counts():
    rows = newest_first(
        row(1, "Pipeline", "STARTED", run="OLD"), row(2, "Copy_Account", "FAILED", run="OLD", msg="boom"),
        row(3, "Pipeline", "STARTED"), row(4, "Copy_Account", "SUCCEEDED", rr=60, rw=60), row(5, "Pipeline", "SUCCEEDED", ts="2026-11-06T10:03:00+00:00"))
    out = rm.summarise(rows)
    assert out["status"] == "SUCCEEDED" and out["failures"] == [] and out["finished_utc"] == "2026-11-06T10:03:00+00:00"
    assert [s["step"] for s in out["steps"]] == ["Pipeline", "Copy_Account"] and out["steps"][1]["rows_written"] == 60


@pytest.mark.parametrize("message,action,needle", [
    ("Invalid object name 'staging.Account'.", None, "create scripts"),
    ("Invalid column name 'CUSTODIAN_CODE'.", "rewrite_stage", "headers"),
    ("ErrorCode=UserErrorSourceBlobNotExist BlobNotFound The specified blob does not exist", "rewrite_stage", "stage/"),
    ("This request is not authorized to perform this operation. AuthorizationPermissionMismatch", None, "Storage Blob Data Reader"),
    ("Login failed for user '<token-identified principal>'.", None, "CREATE USER"),
    ("String or binary data would be truncated", None, "longer"),
    ("Violation of PRIMARY KEY constraint", "rewrite_stage", "duplicate"),
    ("something nobody has seen", None, "does not recognise"),
])
def test_failures_are_diagnosed_with_a_recommendation_and_only_data_causes_offer_a_rewrite(message, action, needle):
    out = rm.summarise(newest_first(row(1, "Pipeline", "STARTED"), row(2, "Copy_Account", "FAILED", msg=message)))
    assert out["status"] == "FAILED" and len(out["failures"]) == 1
    f = out["failures"][0]
    assert f["step"] == "Copy_Account" and f["action"] == action and needle.lower() in (f["cause"] + f["recommendation"]).lower()
    assert f["message"] == message


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(tmp_path / "m.db")
    c.row_factory = sqlite3.Row
    st.ensure_table(c)
    st.save(c, "Ann", {"server": "s.database.windows.net", "database_name": "xodus", "username": "u", "password_env_var": "SQL_TEST_PW", "schema_name": "simplicity"})
    monkeypatch.setenv("SQL_TEST_PW", "secret-pw")
    return c


class _Cur:
    def __init__(self, rows, seen):
        self.rows, self.seen = rows, seen

    def execute(self, sql, params=()):
        self.seen.append((sql, params))

    def fetchall(self):
        return self.rows


class _Db:
    def __init__(self, rows, seen):
        self.rows, self.seen = rows, seen

    def cursor(self):
        return _Cur(self.rows, self.seen)

    def close(self):
        pass


def _connect(rows, seen):
    return lambda cfg, pw: _Db(rows, seen)


def test_refresh_reads_with_a_parameterised_select_stores_the_summary_and_marks_the_stage(conn):
    seen = []
    rows = [(2, "R1", "x.ready", "Pipeline", "SUCCEEDED", None, None, None, "2026-11-06 10:03:00"), (1, "R1", "x.ready", "Pipeline", "STARTED", None, None, None, "2026-11-06 10:00:00")]
    out = rm.refresh(conn, "MIG-1", _connect(rows, seen))
    assert out["status"] == "SUCCEEDED" and rm.succeeded(conn, "MIG-1") and not rm.succeeded(conn, "MIG-2")
    sql, params = seen[0]
    assert "FROM [simplicity].[xodus_run_log]" in sql and "migration_id = %s" in sql and params == ("MIG-1",)
    assert rm.stored(conn, "MIG-1")["checked_utc"]


def test_unreadable_log_is_reported_without_the_password_and_does_not_change_the_stored_status(conn):
    rm.refresh(conn, "MIG-1", _connect([(1, "R1", None, "Pipeline", "SUCCEEDED", None, None, None, "2026-11-06 10:03:00")], []))
    def boom(cfg, pw):
        raise RuntimeError("Login failed for user u secret-pw")
    out = rm.refresh(conn, "MIG-1", boom)
    assert out["status"] == "UNREADABLE" and "secret-pw" not in out["error"] and rm.succeeded(conn, "MIG-1")
    def missing(cfg, pw):
        raise RuntimeError("Invalid object name 'simplicity.xodus_run_log'.")
    assert "02_run_log.sql" in rm.refresh(conn, "MIG-1", missing)["error"]


def test_a_missing_sql_sign_in_is_a_readable_error(tmp_path, monkeypatch):
    c = sqlite3.connect(tmp_path / "n.db")
    c.row_factory = sqlite3.Row
    st.ensure_table(c)
    assert "Save the SQL settings" in rm.refresh(c, "MIG-1")["error"]

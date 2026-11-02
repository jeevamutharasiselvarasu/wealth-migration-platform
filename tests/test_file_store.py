"""Files (source uploads, stored artifacts) round-trip on both backends: on disk for SQLite, in the database for PostgreSQL."""
import pytest

import control_plane.db as cdb
from control_plane import file_store
from control_plane.services import artifact_store


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(cdb, "DB_PATH", tmp_path / "files.db")
    monkeypatch.setattr(artifact_store, "STORAGE_ROOT", tmp_path / "Storage")
    cdb.init_db()
    return tmp_path


def test_upload_bytes_round_trip_and_survive_a_new_connection(storage):
    location = file_store.write(storage / "source_uploads" / "Account", "abc_accounts.csv", b"ID,NAME\n1,Ann\n")
    assert file_store.is_file(location)
    assert file_store.read_bytes(location) == b"ID,NAME\n1,Ann\n"
    if cdb.using_postgres():
        assert str(location).startswith("db://") and not (storage / "source_uploads").exists()
    else:
        assert (storage / "source_uploads" / "Account" / "abc_accounts.csv").is_file()


def test_missing_file_is_reported_not_invented(storage):
    gone = "db://source_uploads/Account/none.csv" if cdb.using_postgres() else str(storage / "none.csv")
    assert not file_store.is_file(gone)
    with pytest.raises(OSError):
        file_store.read_bytes(gone)


def test_artifacts_are_stored_versioned_and_tampering_is_detected(storage):
    conn = cdb.get_db_connection()
    try:
        first = artifact_store.save(conn, "MIG-T", "SIGNOFF_EVIDENCE", b'{"a": 1}', "Ann")
        second = artifact_store.save(conn, "MIG-T", "SIGNOFF_EVIDENCE", b'{"a": 2}', "Ann")
        conn.commit()
        assert (first["artifact_version"], second["artifact_version"]) == (1, 2)
        assert artifact_store.read(conn, first["artifact_id"])["data"] == b'{"a": 1}'
        if cdb.using_postgres():
            conn.execute("UPDATE stored_files SET content = ? WHERE locator LIKE ?", (b"tampered", "%v1.json"))
        else:
            (storage / "Storage" / first["rel_path"]).write_bytes(b"tampered")
        conn.commit()
        with pytest.raises(ValueError, match="no longer matches"):
            artifact_store.read(conn, first["artifact_id"])
        assert artifact_store.read(conn, second["artifact_id"])["data"] == b'{"a": 2}'
    finally:
        conn.close()

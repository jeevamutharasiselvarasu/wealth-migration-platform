"""One-time copy of the platform's own data (users, approvals, mappings, events, packages, ...) from PostgreSQL into the XODUS
Azure SQL database (schema xodus_app), so the app can run on the XODUS database.

Run it where both databases are reachable, for example inside the running container:
    docker compose exec control_plane python scripts/copy_platform_db_to_xodus.py --target "mssql://xodus_app_user:<password>@<server>:1433/<database>"
The source is DATABASE_URL (the local PostgreSQL); pass --source to use another. The target user must have DEFAULT_SCHEMA = xodus_app
(deploy/xodus/01_app_schema.sql). Tables are created with the app's own definitions first; a table the app defines only inline is
created from the PostgreSQL catalogue. Nothing is written into a target table that already holds rows unless --replace is given.
Prints table names and row counts only.
"""
import argparse
import importlib
import os
import pkgutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BATCH = 200


def create_app_tables(target_url: str) -> None:
    """Run every table definition the app has against the target (db.init_db, each service's ensure_table(s), the file store)."""
    os.environ["DATABASE_URL"] = target_url
    from control_plane import db, file_store, mssql_compat
    import control_plane.services as services
    db.init_db()
    conn = mssql_compat.connect(target_url)
    try:
        conn.execute(file_store._DDL)
        for mod in pkgutil.iter_modules(services.__path__):
            m = importlib.import_module(f"control_plane.services.{mod.name}")
            for fn in ("ensure_table", "ensure_tables"):
                if callable(getattr(m, fn, None)):
                    getattr(m, fn)(conn)
        conn.commit()
    finally:
        conn.close()
    from control_plane.services import workflow_event_engine
    workflow_event_engine._ensure_event_table()  # opens its own connection (DATABASE_URL is the target here)


PG_TYPES = {"text": "TEXT", "character varying": "TEXT", "integer": "INTEGER", "bigint": "INTEGER", "smallint": "INTEGER",
            "real": "REAL", "double precision": "REAL", "numeric": "REAL", "bytea": "BYTEA", "boolean": "BOOLEAN"}


def create_from_catalogue(pg, mconn, table: str) -> None:
    from control_plane import mssql_compat
    cols = pg.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema = current_schema() "
                      "AND table_name = %s ORDER BY ordinal_position", (table,)).fetchall()
    pk = [r[0] for r in pg.execute("SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                                   "WHERE i.indrelid = %s::regclass AND i.indisprimary", (table,)).fetchall()]
    body = [f"{c} {PG_TYPES.get(t, 'TEXT')}{'' if n == 'YES' else ' NOT NULL'}" for c, t, n in cols]
    if pk:
        body.append(f"PRIMARY KEY ({', '.join(pk)})")
    mconn.execute(mssql_compat.translate_create_table(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(body)})"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.getenv("DATABASE_URL", ""), help="PostgreSQL URL (default: DATABASE_URL)")
    ap.add_argument("--target", required=True, help="mssql://user:password@server:1433/database (the XODUS database)")
    ap.add_argument("--replace", action="store_true", help="empty a target table that already holds rows before copying into it")
    args = ap.parse_args()
    if not args.source.startswith("postgres"):
        print("The source must be the PostgreSQL URL (DATABASE_URL in the container).")
        return 2
    import psycopg
    from control_plane import mssql_compat
    mssql_compat.parse_url(args.target)
    print("Creating the platform tables in the target (app definitions)...")
    create_app_tables(args.target)
    pg = psycopg.connect(args.source)
    mconn = mssql_compat.connect(args.target)
    copied, skipped = 0, []
    try:
        tables = [r[0] for r in pg.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
                                           "AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]
        for t in tables:
            exists = mconn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = SCHEMA_NAME() AND table_name = ?", (t,)).fetchone()
            if not exists:
                create_from_catalogue(pg, mconn, t)
                print(f"  {t}: created from the PostgreSQL catalogue (the app defines it inline)")
            target_cols = {r[0] for r in mconn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = SCHEMA_NAME() "
                                                       "AND table_name = ?", (t,)).fetchall()}
            have = mconn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            if have and not args.replace:
                skipped.append(t)
                print(f"  {t}: target already holds {have} row(s); left as it is (use --replace to overwrite)")
                continue
            if have:
                mconn.execute(f"DELETE FROM [{t}]")
            cur = pg.execute(f'SELECT * FROM "{t}"')
            cols = [d.name for d in cur.description if d.name in target_cols]
            idx = [i for i, d in enumerate(cur.description) if d.name in target_cols]
            missing = [d.name for d in cur.description if d.name not in target_cols]
            sql = f"INSERT INTO [{t}] ({', '.join(f'[{c}]' for c in cols)}) VALUES ({', '.join('%s' for _ in cols)})"
            raw = mconn._conn.cursor()
            n = 0
            while True:
                rows = cur.fetchmany(BATCH)
                if not rows:
                    break
                raw.executemany(sql, [mssql_compat.params_for_mssql([r[i] for i in idx]) for r in rows])
                n += len(rows)
            mconn.commit()
            copied += n
            print(f"  {t}: {n} row(s) copied" + (f" (columns not in the target, left out: {', '.join(missing)})" if missing else ""))
    finally:
        mconn.close()
        pg.close()
    print(f"Done: {copied} row(s) in {len(tables) - len(skipped)} table(s)." + (f" Skipped (already had rows): {', '.join(skipped)}." if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Source files in Azure Blob storage, with a local Azurite emulator standing in for Azure during development.

The connection string comes from the server environment (AZURE_BLOB_CONNECTION_STRING), never from a form. In Docker Compose it points
at the `blob` (Azurite) service; in production it is the real storage account's connection string or an account URL with managed identity.
Only listing, reading and (for seeding the demo in the emulator) writing blobs happens here. The ADF data plane is not touched.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

ENV_VAR = "AZURE_BLOB_CONNECTION_STRING"
# Azurite's published development account; the key is a documented public constant, not a secret.
AZURITE_ACCOUNT = "devstoreaccount1"
SOURCE_SUFFIXES = (".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".json")
# Data lake zones inside the one container the user names. Blob storage has no real empty folders (outside ADLS Gen2), so a folder is
# made visible with a zero-byte marker blob; the marker is hidden from file lists.
ZONES = ("raw", "stage", "curated")
MARKER = ".xodus-folder"


def firm_folder(source_firm: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (source_firm or "").lower()).strip("-") or "source"


def source_prefix(source_firm: str) -> str:
    """Where the source firm's files are dropped: raw/<firm>/."""
    return f"raw/{firm_folder(source_firm)}/"


def layout_prefixes(source_firm: str) -> List[str]:
    return [f"{z}/" for z in ZONES] + [source_prefix(source_firm)]


def connection_string() -> str:
    return os.environ.get(ENV_VAR, "").strip()


def is_emulator(conn_str: Optional[str] = None) -> bool:
    cs = connection_string() if conn_str is None else conn_str
    return f"AccountName={AZURITE_ACCOUNT}" in cs and any(h in cs for h in ("//blob:", "//127.0.0.1:", "//localhost:", "//host.docker.internal:"))


def _service(conn_str: Optional[str] = None):
    from azure.storage.blob import BlobServiceClient  # imported late so the app starts without the package
    cs = conn_str or connection_string()
    if not cs:
        raise ValueError(f"No blob storage is configured on the server ({ENV_VAR} is not set).")
    return BlobServiceClient.from_connection_string(cs)


def account_name(conn_str: Optional[str]) -> str:
    """The storage account name from a connection string (a public identifier; the key is never read or returned)."""
    m = re.search(r"(?i)(?:^|;)AccountName=([^;]+)", conn_str or "")
    return m.group(1).strip() if m else ""


def status(container: str, conn_str: Optional[str] = None) -> Dict[str, Any]:
    """Reachability and, if the container exists, how many source blobs it holds. Never raises."""
    cs = conn_str if conn_str is not None else connection_string()
    out = {"configured": bool(cs), "emulator": is_emulator(cs), "reachable": False, "container_exists": False, "detail": "", "account": account_name(cs)}
    if not cs:
        out["detail"] = f"{ENV_VAR} is not set on the server."
        return out
    if not container:
        out["detail"] = "Set the source container on the Azure connection page."
    try:
        svc = _service(cs)
        svc.get_service_properties()
        if container:  # the container check is what the stages use; an account that answers but refuses the container is not reachable
            out["container_exists"] = svc.get_container_client(container).exists()
            out["detail"] = "" if out["container_exists"] else f"Container '{container}' does not exist yet."
        out["reachable"] = True
    except Exception as exc:  # network, auth or SDK errors: report, do not crash the page
        out["detail"] = explain_error(exc)
    return out


_HINTS = {
    "AuthenticationFailed": "the account key or connection string is wrong or was rotated",
    "AuthorizationFailure": "the storage firewall (selected networks) or the permissions block this request",
    "AuthorizationPermissionMismatch": "the identity lacks the needed blob permission",
    "KeyBasedAuthenticationNotPermitted": "key access is disabled on the storage account, so a key or SAS cannot be used",
    "AccountIsDisabled": "the storage account is disabled",
    "InvalidAuthenticationInfo": "the connection string or token is malformed",
}


def explain_error(exc: Exception) -> str:
    """A safe reason for a blob failure: the error type, HTTP status and Azure error code only (never the key, URL or request body)."""
    code = str(getattr(exc, "error_code", "") or "").split(".")[-1]
    status_code = getattr(exc, "status_code", None)
    bits = [type(exc).__name__] + ([f"HTTP {status_code}"] if status_code else []) + ([code] if code else [])
    hint = _HINTS.get(code, "")
    if not hint and not status_code:
        text = str(exc).upper()
        hint = ("a proxy or TLS inspection is in the way: the container must trust its certificate" if "CERTIFICATE" in text or "SSL" in text
                else "the storage endpoint could not be reached (network, DNS or the account name)")
    return f"Cannot reach blob storage ({', '.join(bits)})" + (f": {hint}." if hint else ".")


def ensure_layout(container: str, source_firm: str, conn_str: Optional[str] = None) -> Dict[str, Any]:
    """Create the container if needed and the raw/stage/curated folders plus raw/<firm>/. Safe to run again; never deletes anything."""
    if not NAME_OK.match(container or ""):
        raise ValueError("Container names use lowercase letters, digits and '-' (3 to 63 characters).")
    cc = _service(conn_str).get_container_client(container)
    created_container = False
    if not cc.exists():
        cc.create_container()
        created_container = True
    existing = {b.name for b in cc.list_blobs()}
    created = []
    for prefix in layout_prefixes(source_firm):
        marker = prefix + MARKER
        if marker not in existing:
            cc.upload_blob(marker, b"", overwrite=False)
            created.append(prefix)
    return {"container_created": created_container, "folders_created": created}


NAME_OK = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61})[a-z0-9]$")


def tree(container: str, conn_str: Optional[str] = None) -> List[Dict[str, Any]]:
    """Flat, depth-annotated rows (folders and files) for showing the container's structure. Marker blobs only make their folder visible."""
    entries: Dict[str, Dict[str, Any]] = {}
    for b in _service(conn_str).get_container_client(container).list_blobs():
        parts = b.name.split("/")
        for i in range(1, len(parts)):
            path = "/".join(parts[:i]) + "/"
            entries.setdefault(path, {"path": path, "name": parts[i - 1], "kind": "folder", "depth": i - 1, "size": 0})
        if parts[-1] != MARKER:
            entries[b.name] = {"path": b.name, "name": parts[-1], "kind": "file", "depth": len(parts) - 1, "size": int(b.size or 0)}
    def order(path: str):  # data lake zones in pipeline order (raw, stage, curated), anything else after, alphabetical inside
        parts = path.rstrip("/").split("/")
        return (ZONES.index(parts[0]) if parts[0] in ZONES else len(ZONES), parts)
    return [entries[k] for k in sorted(entries, key=order)]


def list_source_blobs(container: str, conn_str: Optional[str] = None, prefix: str = "") -> List[Dict[str, Any]]:
    svc = _service(conn_str)
    items = []
    for b in svc.get_container_client(container).list_blobs(name_starts_with=prefix or None):
        if not b.name.lower().endswith(SOURCE_SUFFIXES):
            continue
        items.append({"name": b.name, "size": int(b.size or 0), "modified": b.last_modified.isoformat() if b.last_modified else ""})
    return sorted(items, key=lambda i: i["name"])


def read_blob(container: str, name: str, conn_str: Optional[str] = None) -> bytes:
    if not name or name.startswith("/") or ".." in name.split("/"):
        raise ValueError("Invalid blob name.")
    return _service(conn_str).get_container_client(container).download_blob(name).readall()


def seed(container: str, files: Dict[str, bytes], conn_str: Optional[str] = None, prefix: str = "", source_firm: str = "") -> int:
    """Emulator only: create the layout, then make `prefix` hold exactly these files (other folders are left alone)."""
    cs = conn_str if conn_str is not None else connection_string()
    if not is_emulator(cs):
        raise ValueError("Seeding sample files is only allowed against the local blob emulator.")
    ensure_layout(container, source_firm or "source", cs)
    cc = _service(cs).get_container_client(container)
    keep = {prefix + n for n in files}
    for existing in [b.name for b in cc.list_blobs(name_starts_with=prefix or None)]:
        if existing not in keep and not existing.endswith("/" + MARKER) and existing != MARKER:
            cc.delete_blob(existing)
    for name, data in files.items():
        cc.upload_blob(prefix + name, data, overwrite=True)
    return len(files)


def write_stage_run(container: str, run_id: str, outputs: Dict[str, List[Dict[str, Any]]], support: Dict[str, Any],
                    conn_str: Optional[str] = None) -> Dict[str, Any]:
    """Write one run's transformed output and the files curation needs under stage/run-<id>/. Never touches raw/ or curated/.

    <TABLE>.csv per target table, plus _run/<name>.json for each supporting document (package contract, counts, source file hashes)."""
    import csv
    import io
    import json
    folder = f"stage/{run_id.lower()}/"
    cc = _service(conn_str).get_container_client(container)
    written = []
    for table, rows in sorted(outputs.items()):
        cols = list(rows[0].keys()) if rows else []
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
        cc.upload_blob(folder + f"{table}.csv", buf.getvalue().encode("utf-8"), overwrite=True)
        written.append({"name": f"{table}.csv", "rows": len(rows)})
    for name, doc in sorted(support.items()):
        cc.upload_blob(folder + f"_run/{name}.json", json.dumps(doc, indent=2, default=str).encode("utf-8"), overwrite=True)
        written.append({"name": f"_run/{name}.json", "rows": None})
    return {"written": True, "container": container, "path": folder, "files": written}


def canonical_csv(columns: List[str], rows: List[Dict[str, Any]]) -> bytes:
    """One canonical-named CSV: exactly the staging columns in order, blanks for missing values."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in columns])
    return buf.getvalue().encode("utf-8")


def project_csv(data: bytes, columns: List[str]) -> bytes:
    """A lookup file cut to exactly the staging table's columns (matched by name, ignoring case and a leading BOM). The staging table only
    holds the columns the rules read, and the copy fails on any other column, so the file must not carry them."""
    import csv
    import io
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    by_lower = {str(h).strip().lower(): h for h in (reader.fieldnames or [])}
    missing = [c for c in columns if c.lower() not in by_lower]
    if missing:
        raise ValueError("The lookup file has no column named " + ", ".join(missing) + ".")
    return canonical_csv(columns, [{c: row.get(by_lower[c.lower()]) for c in columns} for row in reader])


def write_canonical_stage(container: str, folder: str, tables: Dict[str, Any], lookups: Dict[str, bytes], conn_str: Optional[str] = None) -> Dict[str, Any]:
    """Write the raw files, mapped to canonical names, under stage/<folder>/: <Entity>.csv per entity (tables = {name: (columns, rows)})
    and lookups/<name>.csv copied as they are. Only stage/ is written; raw/ and curated/ are never touched."""
    if not folder.startswith("stage/") or ".." in folder:
        raise ValueError("The canonical files go under stage/ only.")
    cc = _service(conn_str).get_container_client(container)
    written = []
    for name, (columns, rows) in sorted(tables.items()):
        cc.upload_blob(f"{folder}/{name}.csv", canonical_csv(columns, rows), overwrite=True)
        written.append({"name": f"{name}.csv", "rows": len(rows), "columns": len(columns)})
    for name, data in sorted(lookups.items()):
        cc.upload_blob(f"{folder}/lookups/{name}.csv", data, overwrite=True)
        written.append({"name": f"lookups/{name}.csv", "rows": None, "columns": None})
    return {"container": container, "path": folder + "/", "files": written}


def write_ready_marker(container: str, name: str, marker: Dict[str, Any], conn_str: Optional[str] = None) -> None:
    """Place the .ready marker under curated/ that starts the pipeline. Never overwrites an earlier marker."""
    import json
    if not re.fullmatch(r"curated/[A-Za-z0-9_.\-]+\.ready", name):
        raise ValueError("The ready marker goes under curated/ and ends with .ready.")
    _service(conn_str).get_container_client(container).upload_blob(name, json.dumps(marker, indent=2, default=str).encode("utf-8"), overwrite=False)


def promote_to_curated(container: str, run_id: str, approver: str, review_summary: Dict[str, Any], conn_str: Optional[str] = None) -> Dict[str, Any]:
    """Copy one run's transformed tables from stage/run-<id>/ to curated/ (latest approved version per table) and write curated/_lineage/<run>.json.
    Reads only the run's own stage folder; never touches raw/. The caller has already applied the approval gate."""
    import csv
    import hashlib
    import io
    import json
    from datetime import datetime, timezone
    folder = f"stage/{run_id.lower()}/"
    cc = _service(conn_str).get_container_client(container)
    tables = [b.name for b in cc.list_blobs(name_starts_with=folder) if b.name.endswith(".csv") and "/_run/" not in b.name]
    if not tables:
        raise ValueError("This run has no transformed tables in stage to curate.")
    files = []
    for name in sorted(tables):
        data = cc.download_blob(name).readall()
        target = "curated/" + name.rsplit("/", 1)[-1]
        cc.upload_blob(target, data, overwrite=True)
        rows = max(0, len(list(csv.reader(io.StringIO(data.decode("utf-8"))))) - 1)
        files.append({"name": target, "rows": rows, "sha256": hashlib.sha256(data).hexdigest(), "from": name})
    when = datetime.now(timezone.utc).isoformat()
    lineage = {"run_id": run_id, "promoted_by": approver, "promoted_utc": when, "reviews": review_summary, "files": files}
    cc.upload_blob(f"curated/_lineage/{run_id.lower()}.json", json.dumps(lineage, indent=2).encode("utf-8"), overwrite=True)
    return {"promoted": True, "container": container, "path": "curated/", "files": files, "by": approver, "utc": when}


def preview(container: str, name: str, max_lines: int = 25, max_bytes: int = 65536, conn_str: Optional[str] = None) -> Dict[str, Any]:
    """Read-only look at the start of one text file (CSV/JSON/TSV/TXT) in the container. Hidden marker blobs are refused."""
    if name.endswith(MARKER) or not name.lower().endswith((".csv", ".tsv", ".json", ".txt")):
        raise ValueError("Only CSV, TSV, JSON and text files can be previewed.")
    data = read_blob(container, name, conn_str)
    text = data[:max_bytes].decode("utf-8", errors="replace")
    lines = text.splitlines()
    shown = lines[:max_lines]
    return {"name": name, "size": len(data), "text": "\n".join(shown), "truncated": len(data) > max_bytes or len(lines) > max_lines, "shown_lines": len(shown)}

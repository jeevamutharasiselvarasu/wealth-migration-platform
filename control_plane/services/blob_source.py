"""Source files in Azure Blob storage, with a local Azurite emulator standing in for Azure during development.

The connection string comes from the server environment (AZURE_BLOB_CONNECTION_STRING), never from a form. In Docker Compose it points
at the `blob` (Azurite) service; in production it is the real storage account's connection string or an account URL with managed identity.
Only listing, reading and (for seeding the demo in the emulator) writing blobs happens here. The ADF data plane is not touched.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

ENV_VAR = "AZURE_BLOB_CONNECTION_STRING"
# Azurite's published development account; the key is a documented public constant, not a secret.
AZURITE_ACCOUNT = "devstoreaccount1"
SOURCE_SUFFIXES = (".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".json")


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


def status(container: str, conn_str: Optional[str] = None) -> Dict[str, Any]:
    """Reachability and, if the container exists, how many source blobs it holds. Never raises."""
    cs = conn_str if conn_str is not None else connection_string()
    out = {"configured": bool(cs), "emulator": is_emulator(cs), "reachable": False, "container_exists": False, "detail": ""}
    if not cs:
        out["detail"] = f"{ENV_VAR} is not set on the server."
        return out
    if not container:
        out["detail"] = "Set the source container on the Azure connection page."
    try:
        svc = _service(cs)
        svc.get_service_properties()
        out["reachable"] = True
        if container:
            out["container_exists"] = svc.get_container_client(container).exists()
            out["detail"] = "" if out["container_exists"] else f"Container '{container}' does not exist yet."
    except Exception as exc:  # network, auth or SDK errors: report, do not crash the page
        out["detail"] = f"Cannot reach blob storage: {type(exc).__name__}"
    return out


def list_source_blobs(container: str, conn_str: Optional[str] = None) -> List[Dict[str, Any]]:
    svc = _service(conn_str)
    items = []
    for b in svc.get_container_client(container).list_blobs():
        if not b.name.lower().endswith(SOURCE_SUFFIXES):
            continue
        items.append({"name": b.name, "size": int(b.size or 0), "modified": b.last_modified.isoformat() if b.last_modified else ""})
    return sorted(items, key=lambda i: i["name"])


def read_blob(container: str, name: str, conn_str: Optional[str] = None) -> bytes:
    if not name or name.startswith("/") or ".." in name.split("/"):
        raise ValueError("Invalid blob name.")
    return _service(conn_str).get_container_client(container).download_blob(name).readall()


def seed(container: str, files: Dict[str, bytes], conn_str: Optional[str] = None) -> int:
    """Create the container if needed, make it hold exactly these files, upload them. Only allowed against the emulator."""
    cs = conn_str if conn_str is not None else connection_string()
    if not is_emulator(cs):
        raise ValueError("Seeding sample files is only allowed against the local blob emulator.")
    cc = _service(cs).get_container_client(container)
    if not cc.exists():
        cc.create_container()
    for existing in [b.name for b in cc.list_blobs()]:  # the emulator container is reset to exactly this set
        if existing not in files:
            cc.delete_blob(existing)
    for name, data in files.items():
        cc.upload_blob(name, data, overwrite=True)
    return len(files)

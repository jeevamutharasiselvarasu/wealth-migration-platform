"""Publishing through pull requests (option b): XODUS pushes the package to its own branch, opens a pull request into the configured
branch, shows its state, and merges it when a person approves it in XODUS (the approver is recorded; the merge commit names them).

Never forces the configured branch, never deletes, never rebases. Only branches named xodus/<migration>-<version> are written.
"""
import json
import re
import sqlite3
from fnmatch import fnmatch
from typing import Any, Callable, Dict, List

import httpx

from control_plane.services.git_connection import BRANCH, _default_http, _headers, _now, _repo_url, _unreachable, get_config, layout

# Files the earlier XODUS package format generated (one pipeline per migration, staging datasets, the ready trigger). They do not belong
# with the stable pipelines and can block publishing in Data Factory, so the pull request removes them; nothing else is ever deleted.
LEGACY = ("pipeline/PL_XODUS_MIG_*.json", "dataset/DS_Staging_*.json", "dataset/DS_StagingSql_*.json", "trigger/TR_XODUS_Curated_Ready.json")

_DDL = """
CREATE TABLE IF NOT EXISTS git_pull_requests (
    number INTEGER NOT NULL, repository TEXT NOT NULL, migration_id TEXT NOT NULL, artifact_id TEXT, package_version TEXT, branch TEXT NOT NULL,
    base TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL, opened_by TEXT NOT NULL, opened_utc TEXT NOT NULL, approved_by TEXT,
    merged_utc TEXT, merge_sha TEXT, checked_utc TEXT, detail TEXT, PRIMARY KEY (repository, number)
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _caller(cfg: Dict[str, Any], http: Callable[..., httpx.Response]):
    h = _headers(cfg)

    def call(method: str, tail: str, ok=(200, 201), **kw):
        r = http(method, _repo_url(cfg, tail), headers=h, **kw)
        if r.status_code not in ok:
            try:
                msg = str(r.json().get("message") or "")[:240]
            except ValueError:
                msg = ""
            if r.status_code in (401, 403):
                raise ValueError(f"GitHub refused (the token needs Contents and Pull requests: read and write). {msg}".strip())
            if r.status_code == 405:
                raise ValueError(f"GitHub will not merge this pull request yet: {msg or 'it is not mergeable'}.")
            if r.status_code == 409:
                raise ValueError(f"The branch moved on GitHub: {msg or 'conflict'}. Refresh and try again.")
            if r.status_code == 422:
                raise ValueError(f"GitHub rejected the request: {msg or 'unprocessable'}.")
            raise ValueError(f"GitHub returned HTTP {r.status_code}. {msg}".strip())
        return r.json() if r.content else {}
    return call


def branch_name(migration_id: str, version: str) -> str:
    name = "xodus/" + re.sub(r"[^a-z0-9-]", "", migration_id.lower()) + "-" + re.sub(r"[^a-z0-9]", "", version.lower())[:16]
    if not BRANCH.match(name):
        raise ValueError("Could not name the branch safely.")
    return name


def open_pull_request(conn: sqlite3.Connection, actor: str, migration_id: str, package_files: Dict[str, str], version: str, artifact_id: str,
                      body: str, http: Callable[..., httpx.Response] = _default_http) -> Dict[str, Any]:
    """Commit the package to xodus/<migration>-<version> (made from the configured branch) and open a pull request into that branch."""
    ensure_table(conn)
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Save the GitHub settings first.")
    if cfg["status"] != "CONNECTED":
        raise ValueError("Test the GitHub connection successfully before publishing.")
    files = layout(cfg, migration_id, package_files)
    branch = branch_name(migration_id, version)
    open_rows = conn.execute("SELECT number FROM git_pull_requests WHERE repository = ? AND branch = ? AND state = 'OPEN'", (cfg["repository"], branch)).fetchall()
    if open_rows:
        raise ValueError(f"Pull request #{open_rows[0]['number']} is already open for this package version.")
    try:
        call = _caller(cfg, http)
        head = call("GET", f"/git/ref/heads/{cfg['branch']}")["object"]["sha"]
        base_tree = call("GET", f"/git/commits/{head}")["tree"]["sha"]
        entries = [{"path": p, "mode": "100644", "type": "blob", "content": t} for p, t in sorted(files.items())]
        listing = call("GET", f"/git/trees/{base_tree}?recursive=1")
        if listing.get("truncated"):
            raise ValueError("The repository is too large to list safely; nothing was published.")
        root = cfg["adf_folder"] + "/"
        removed = sorted(n["path"] for n in listing.get("tree", []) if n.get("type") == "blob" and n["path"].startswith(root)
                         and n["path"] not in files and any(fnmatch(n["path"][len(root):], pat) for pat in LEGACY))
        entries += [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in removed]
        if removed:
            body += "\n\nRemoves files of the earlier XODUS package format:\n" + "\n".join(f"- {p}" for p in removed)
        tree = call("POST", "/git/trees", json={"base_tree": base_tree, "tree": entries})["sha"]
        commit = call("POST", "/git/commits", json={"message": f"XODUS package {version} for {migration_id}", "tree": tree, "parents": [head]})
        if call("GET", f"/git/ref/heads/{branch}", ok=(200, 404)).get("object"):
            call("PATCH", f"/git/refs/heads/{branch}", json={"sha": commit["sha"], "force": True})  # XODUS's own branch for this version only
        else:
            call("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        pr = call("POST", "/pulls", json={"title": f"XODUS {migration_id}: data-plane package {version}", "head": branch, "base": cfg["branch"], "body": body})
    except httpx.HTTPError as exc:
        raise ValueError(_unreachable(exc))
    rec = {"number": pr["number"], "repository": cfg["repository"], "migration_id": migration_id, "artifact_id": artifact_id, "package_version": version,
           "branch": branch, "base": cfg["branch"], "url": pr["html_url"], "state": "OPEN", "opened_by": actor, "opened_utc": _now()}
    conn.execute("INSERT INTO git_pull_requests (number, repository, migration_id, artifact_id, package_version, branch, base, url, state, opened_by, opened_utc) "
                 "VALUES (:number, :repository, :migration_id, :artifact_id, :package_version, :branch, :base, :url, :state, :opened_by, :opened_utc)", rec)
    conn.execute("UPDATE git_connection_config SET last_commit_sha = ?, last_published_utc = ?, last_published_by = ?, last_artifact_id = ? WHERE config_id = 1",
                 (commit["sha"], _now(), actor, artifact_id or None))
    return {**rec, "files": len(files), "removed": removed}


def list_for(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, Any]]:
    ensure_table(conn)
    out = []
    for r in conn.execute("SELECT * FROM git_pull_requests WHERE migration_id = ? ORDER BY opened_utc DESC", (migration_id,)).fetchall():
        d = dict(r)
        d.update(json.loads(d.get("detail") or "{}"))
        out.append(d)
    return out


def refresh(conn: sqlite3.Connection, number: int, http: Callable[..., httpx.Response] = _default_http) -> Dict[str, Any]:
    """GitHub's current view of a pull request opened by XODUS: open, merged or closed, whether it can be merged, and its reviews."""
    ensure_table(conn)
    cfg = get_config(conn)
    row = conn.execute("SELECT * FROM git_pull_requests WHERE repository = ? AND number = ?", (cfg["repository"], int(number))).fetchone() if cfg else None
    if not row:
        raise ValueError("That pull request is not one XODUS opened.")
    try:
        call = _caller(cfg, http)
        pr = call("GET", f"/pulls/{int(number)}")
        reviews = call("GET", f"/pulls/{int(number)}/reviews")
    except httpx.HTTPError as exc:
        raise ValueError(_unreachable(exc))
    state = "MERGED" if pr.get("merged") else str(pr.get("state", "")).upper()
    detail = {"mergeable": pr.get("mergeable"), "mergeable_state": pr.get("mergeable_state"), "changed_files": pr.get("changed_files"),
              "reviews": [{"user": (r.get("user") or {}).get("login"), "state": r.get("state")} for r in reviews][-5:]}
    merged_by = ("GitHub: " + str((pr.get("merged_by") or {}).get("login") or "unknown")) if pr.get("merged") else None  # merged outside XODUS
    conn.execute("UPDATE git_pull_requests SET state = ?, checked_utc = ?, detail = ?, merge_sha = COALESCE(merge_sha, ?), approved_by = COALESCE(approved_by, ?), "
                 "merged_utc = COALESCE(merged_utc, ?) WHERE repository = ? AND number = ?",
                 (state, _now(), json.dumps(detail), pr.get("merge_commit_sha") if pr.get("merged") else None, merged_by,
                  pr.get("merged_at") if pr.get("merged") else None, cfg["repository"], int(number)))
    return {**dict(row), "state": state, **detail}


def merge(conn: sqlite3.Connection, number: int, actor: str, http: Callable[..., httpx.Response] = _default_http) -> Dict[str, Any]:
    """Option b: the approver is recorded in XODUS and the platform merges on their behalf (a normal merge commit naming them)."""
    current = refresh(conn, number, http)
    if current["state"] != "OPEN":
        raise ValueError(f"Pull request #{number} is {current['state'].lower()}, not open.")
    cfg = get_config(conn)
    try:
        res = _caller(cfg, http)("PUT", f"/pulls/{int(number)}/merge", json={
            "merge_method": "merge", "commit_title": f"Merge XODUS package (pull request #{int(number)})",
            "commit_message": f"Approved in XODUS by {actor}."})
    except httpx.HTTPError as exc:
        raise ValueError(_unreachable(exc))
    conn.execute("UPDATE git_pull_requests SET state = 'MERGED', approved_by = ?, merged_utc = ?, merge_sha = ? WHERE repository = ? AND number = ?",
                 (actor, _now(), res.get("sha"), cfg["repository"], int(number)))
    return {**current, "state": "MERGED", "approved_by": actor, "merge_sha": res.get("sha")}

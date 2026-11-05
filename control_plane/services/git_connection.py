"""GitHub connection for handing the generated ADF artifacts to the data factory owner.

The platform commits the stored ADF package to a GitHub repository; the owner of the data factory links that repository in
Azure Data Factory (Manage > Git configuration) and publishes from there. Only non-secret settings are stored (repository,
branch, folder, the NAME of the environment variable that holds the token). The token itself is never accepted from a form, stored,
logged or returned. The platform only ever adds a commit on the configured branch: it never force-pushes, deletes files, or touches
other branches. Uses the GitHub REST API, so no git binary is needed on the server."""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import httpx
from control_plane.db import table_columns

_DDL = """
CREATE TABLE IF NOT EXISTS git_connection_config (
    config_id INTEGER PRIMARY KEY CHECK (config_id = 1),
    repository TEXT NOT NULL, branch TEXT NOT NULL, adf_folder TEXT NOT NULL, token_env_var TEXT NOT NULL,
    status TEXT NOT NULL, checked_utc TEXT, check_detail TEXT, configured_by TEXT NOT NULL, configured_utc TEXT NOT NULL,
    last_commit_sha TEXT, last_published_utc TEXT, last_published_by TEXT, last_artifact_id TEXT
)
"""
FIELDS = ("repository", "branch", "adf_folder", "token_env_var")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
ENV_VAR = re.compile(r"^GIT(HUB)?_[A-Z0-9_]{1,50}$")
FILE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
NATIVE_DIRS = ("pipeline", "dataset", "linkedService")  # the folders Azure Data Factory reads from its repository root folder
DEFAULT_TOKEN_ENV = "GITHUB_ADF_TOKEN"
DEFAULT_BRANCH = "main"
DEFAULT_FOLDER = "adf"
API = "https://api.github.com"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)
    if "last_artifact_id" not in table_columns(conn, "git_connection_config"):  # tables created before the publish stage existed
        conn.execute("ALTER TABLE git_connection_config ADD COLUMN last_artifact_id TEXT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate(values: Dict[str, str]) -> Dict[str, str]:
    v = {k: str(values.get(k, "")).strip() for k in FIELDS}
    v["repository"] = re.sub(r"^https://github\.com/", "", v["repository"]).removesuffix(".git").strip("/")
    v["branch"] = v["branch"] or DEFAULT_BRANCH
    v["adf_folder"] = v["adf_folder"] or DEFAULT_FOLDER
    v["token_env_var"] = v["token_env_var"] or DEFAULT_TOKEN_ENV
    if not REPO.match(v["repository"]):
        raise ValueError("Repository must look like owner/name (or its github.com address).")
    if not BRANCH.match(v["branch"]) or ".." in v["branch"] or v["branch"].endswith("/") or v["branch"].endswith(".lock"):
        raise ValueError("Branch name is not valid.")
    if not FOLDER.match(v["adf_folder"]):
        raise ValueError("The ADF root folder must be letters, digits, '-' or '_' (one folder, up to 40 characters).")
    if not ENV_VAR.match(v["token_env_var"]):
        raise ValueError("The token is read from a server environment variable whose name starts with GIT_ or GITHUB_ "
                         "(for example GITHUB_ADF_TOKEN). Never type the token itself here.")
    return v


def get_config(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM git_connection_config WHERE config_id = 1").fetchone()
    if not row:
        return None
    cfg = dict(row)
    cfg["token_present"] = bool(os.environ.get(cfg["token_env_var"]))
    return cfg


def save(conn: sqlite3.Connection, actor: str, values: Dict[str, str]) -> Dict[str, Any]:
    """Save settings (status CONFIGURED). Any change clears an earlier successful test."""
    ensure_table(conn)
    v = validate(values)
    conn.execute("DELETE FROM git_connection_config WHERE config_id = 1")
    conn.execute("INSERT INTO git_connection_config (config_id, " + ", ".join(FIELDS) + ", status, configured_by, configured_utc) VALUES (1, "
                 + ", ".join("?" for _ in FIELDS) + ", 'CONFIGURED', ?, ?)", (*[v[k] for k in FIELDS], actor, _now()))
    return get_config(conn)


def _default_http(method: str, url: str, **kw) -> httpx.Response:
    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        return client.request(method, url, **kw)


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    token = os.environ.get(cfg["token_env_var"], "")
    if not token:
        raise ValueError(f"The server has no value for {cfg['token_env_var']}. Set it in the server environment and restart.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _repo_url(cfg: Dict[str, Any], tail: str = "") -> str:
    return f"{API}/repos/{cfg['repository']}{tail}"


def test_connection(conn: sqlite3.Connection, http: Callable[..., httpx.Response] = _default_http) -> Dict[str, Any]:
    """Read the repository and the branch with the token. Records CONNECTED or FAILED with a safe message; changes nothing on GitHub."""
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Save the GitHub settings first.")
    try:
        h = _headers(cfg)
        resp = http("GET", _repo_url(cfg), headers=h)
        if resp.status_code == 401:
            status, detail = "FAILED", "GitHub rejected the token (expired, revoked or mistyped)."
        elif resp.status_code == 404:
            status, detail = "FAILED", "Repository not found, or the token has no access to it."
        elif resp.status_code != 200:
            status, detail = "FAILED", f"GitHub returned HTTP {resp.status_code}."
        elif not (resp.json().get("permissions") or {}).get("push"):
            status, detail = "FAILED", "The token can read the repository but cannot write to it (needs Contents: read and write)."
        else:
            br = http("GET", _repo_url(cfg, f"/branches/{cfg['branch']}"), headers=h)
            if br.status_code == 200:
                status, detail = "CONNECTED", f"Signed in and can write to {cfg['repository']} on branch {cfg['branch']}."
            elif br.status_code == 404:
                status, detail = "FAILED", (f"Branch {cfg['branch']} does not exist. Create the repository with a README (or the branch) first.")
            else:
                status, detail = "FAILED", f"GitHub returned HTTP {br.status_code} for the branch."
    except ValueError as exc:
        status, detail = "FAILED", str(exc)
    except httpx.HTTPError as exc:
        status, detail = "FAILED", _unreachable(exc)
    conn.execute("UPDATE git_connection_config SET status = ?, checked_utc = ?, check_detail = ? WHERE config_id = 1", (status, _now(), detail))
    return get_config(conn)


def _unreachable(exc: Exception) -> str:
    """A safe reason for a network failure: the error type and the low-level reason only (never headers, URLs or the token)."""
    reason = re.sub(r"\s+", " ", str(exc))[:140]
    hint = (" Likely a proxy or TLS inspection on this network: the container must trust the proxy certificate or use the proxy."
            if "CERTIFICATE" in reason.upper() or "SSL" in reason.upper() else "")
    return f"Could not reach GitHub ({type(exc).__name__}: {reason}).{hint}"


def layout(cfg: Dict[str, Any], migration_id: str, package_files: Dict[str, str]) -> Dict[str, str]:
    """Where each package file goes in the repository. Native Data Factory JSON goes under the ADF root folder (what the
    factory reads once the repository is linked); the SQL, ARM template and manifest go beside it under xodus/<migration>/."""
    mig = re.sub(r"[^A-Za-z0-9_-]", "_", migration_id)
    out: Dict[str, str] = {}
    for path, text in package_files.items():
        if not FILE_PATH.match(path) or ".." in path.split("/"):
            raise ValueError(f"Unsafe file path in the package: {path}")
        native = path.split("/")[0] in NATIVE_DIRS
        out[(f"{cfg['adf_folder']}/{path}" if native else f"xodus/{mig}/{path}")] = text
    return out


def publish(conn: sqlite3.Connection, actor: str, migration_id: str, package_files: Dict[str, str], message: str,
            http: Callable[..., httpx.Response] = _default_http, artifact_id: str = "") -> Dict[str, Any]:
    """Add one commit with the package files to the configured branch (Git Data API). Never forces, deletes or touches other branches."""
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Save the GitHub settings first.")
    if cfg["status"] != "CONNECTED":
        raise ValueError("Test the GitHub connection successfully before publishing.")
    files = layout(cfg, migration_id, package_files)
    if not files:
        raise ValueError("The package has no files to publish.")
    h = _headers(cfg)
    try:
        def call(method: str, tail: str, ok=(200, 201), **kw):
            r = http(method, _repo_url(cfg, tail), headers=h, **kw)
            if r.status_code not in ok:
                if r.status_code in (401, 403):
                    raise ValueError("GitHub refused the write (the token needs Contents: read and write on this repository).")
                if r.status_code == 409:
                    raise ValueError("The repository is empty or the branch moved while publishing. Add a README to the repository and try again.")
                if r.status_code == 422:
                    raise ValueError("GitHub rejected the commit (the branch may be protected or have changed). Try again.")
                raise ValueError(f"GitHub returned HTTP {r.status_code} while publishing.")
            return r.json()
        head = call("GET", f"/git/ref/heads/{cfg['branch']}")["object"]["sha"]
        base_tree = call("GET", f"/git/commits/{head}")["tree"]["sha"]
        tree = call("POST", "/git/trees", json={"base_tree": base_tree, "tree": [
            {"path": p, "mode": "100644", "type": "blob", "content": t} for p, t in sorted(files.items())]})["sha"]
        commit = call("POST", "/git/commits", json={"message": message, "tree": tree, "parents": [head]})
        call("PATCH", f"/git/refs/heads/{cfg['branch']}", json={"sha": commit["sha"], "force": False})
    except httpx.HTTPError as exc:
        raise ValueError(_unreachable(exc))
    conn.execute("UPDATE git_connection_config SET last_commit_sha = ?, last_published_utc = ?, last_published_by = ?, last_artifact_id = ? WHERE config_id = 1",
                 (commit["sha"], _now(), actor, artifact_id or None))
    return {"commit_sha": commit["sha"], "files": len(files), "branch": cfg["branch"], "repository": cfg["repository"],
            "url": f"https://github.com/{cfg['repository']}/commit/{commit['sha']}"}

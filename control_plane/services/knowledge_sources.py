"""Where knowledge can be read from besides uploads: approved web pages and the firm's Confluence.

Both are set up by a Platform Admin only; everyone else just uses them and is told to ask an admin when something is not set up.

Web links: https only, only hosts the admin approved (a subdomain of an approved host counts), the host must resolve to public
addresses only (no loopback, private, link-local or reserved ranges), redirects are followed by hand and every hop is checked again,
at most 2 MB, 20 seconds, and only HTML, plain text, Markdown or PDF. The page is reduced to visible text; nothing is executed.
(The address is checked before the request and not pinned to it, so a host that changes its answer between the two could still be
reached; the approved-host list is what limits that.)

Confluence (Cloud or Data Center): the admin saves the base address, how to sign in (account email plus API token, or a personal
access token), the allowed spaces and a page cap. The secret itself is never entered here, stored, shown or logged: only the NAME of
the server environment variable that holds it is saved. A page, a page with its children, or a whole space can be imported, only
from allowed spaces. A page keeps its Confluence version, so importing it again changes nothing unless the page changed.
"""
import hashlib
import ipaddress
import os
import re
import socket
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from control_plane.services import confluence_source, document_text

MAX_BYTES = 2_000_000
MAX_HOPS = 3
TIMEOUT = 20.0
WEB_TYPES = ("text/html", "text/plain", "text/markdown", "application/pdf")
HOST = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
SPACE = re.compile(r"^[A-Za-z0-9~_-]{1,255}$")
TOKEN_ENV = re.compile(r"^CONFLUENCE_[A-Z0-9_]{1,50}$")
EMAIL = re.compile(r"^[^@\s]{1,100}@[^@\s]{1,150}\.[A-Za-z]{2,}$")
DEFAULT_TOKEN_ENV = "CONFLUENCE_API_TOKEN"
AUTHS = ("basic", "bearer")

_DDL = (
    "CREATE TABLE IF NOT EXISTS knowledge_web_domains (host TEXT PRIMARY KEY, added_by TEXT NOT NULL, added_utc TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS confluence_connection (
        config_id INTEGER PRIMARY KEY CHECK (config_id = 1), base_url TEXT NOT NULL, auth TEXT NOT NULL, email TEXT, token_env_var TEXT NOT NULL,
        allowed_spaces TEXT NOT NULL, page_cap INTEGER NOT NULL, status TEXT NOT NULL, checked_utc TEXT, check_detail TEXT,
        configured_by TEXT NOT NULL, configured_utc TEXT NOT NULL)""",
)


class SourceError(ValueError):
    pass


def ensure_tables(conn: sqlite3.Connection) -> None:
    for ddl in _DDL:
        conn.execute(ddl)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------------------------------------------------- web pages
def list_domains(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    return [dict(r) for r in conn.execute("SELECT host, added_by, added_utc FROM knowledge_web_domains ORDER BY host").fetchall()]


def add_domain(conn: sqlite3.Connection, actor: str, host: str) -> str:
    ensure_tables(conn)
    host = str(host or "").strip().lower().rstrip(".")
    if not HOST.match(host) or host in ("localhost", "localhost.localdomain"):
        raise SourceError("Enter a host name such as docs.example.com (no address, port or path).")
    try:
        ipaddress.ip_address(host)
        raise SourceError("Enter a host name, not an IP address.")
    except ValueError as exc:
        if isinstance(exc, SourceError):
            raise
    if conn.execute("SELECT 1 FROM knowledge_web_domains WHERE host = ?", (host,)).fetchone():
        raise SourceError(f"{host} is already approved.")
    conn.execute("INSERT INTO knowledge_web_domains (host, added_by, added_utc) VALUES (?, ?, ?)", (host, actor, _now()))
    return host


def remove_domain(conn: sqlite3.Connection, host: str) -> None:
    ensure_tables(conn)
    if conn.execute("DELETE FROM knowledge_web_domains WHERE host = ?", (str(host or "").strip().lower(),)).rowcount == 0:
        raise SourceError("That host is not on the approved list.")


def _approved(conn: sqlite3.Connection, host: str) -> bool:
    return any(host == d["host"] or host.endswith("." + d["host"]) for d in list_domains(conn))


def _public_only(host: str, resolver: Callable[..., Any]) -> None:
    try:
        infos = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceError(f"Could not look up {host}.") from exc
    addresses = {i[4][0] for i in infos}
    if not addresses or any(not ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise SourceError(f"{host} does not resolve to a public address, so it cannot be read.")


class _Visible(HTMLParser):
    _SKIP = {"script", "style", "noscript", "nav", "footer", "header", "form", "svg", "iframe", "template"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "pre", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3") and not self._skip:
            self.parts.append("\n" + "#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> Tuple[str, str]:
    """(title, visible text with headings as Markdown) of an HTML page."""
    p = _Visible()
    p.feed(markup or "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(p.parts).splitlines()]
    return re.sub(r"\s+", " ", p.title).strip(), "\n".join(ln for ln in lines if ln)


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([A-Za-z0-9_-]+)", content_type or "")
    try:
        return body.decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _default_getter(url: str) -> Tuple[int, Dict[str, str], bytes]:
    from control_plane import connection_health
    resp = connection_health.http_request("Web link", lambda: httpx.Client(timeout=TIMEOUT, follow_redirects=False), "GET", url,
                                          headers={"Accept": ", ".join(WEB_TYPES), "User-Agent": "knowledge-reader"})
    return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.content[:MAX_BYTES + 1]


def fetch_web(conn: sqlite3.Connection, url: str, getter: Optional[Callable[[str], Tuple[int, Dict[str, str], bytes]]] = None,
              resolver: Callable[..., Any] = socket.getaddrinfo) -> Dict[str, str]:
    """{"title", "text", "url", "version"} for an approved https page. Raises SourceError with a reason a user can act on."""
    current = str(url or "").strip()
    for _hop in range(MAX_HOPS + 1):
        parsed = urlparse(current)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise SourceError("Use a plain https link (no sign-in in the address, no custom port).")
        host = parsed.hostname.lower()
        if not _approved(conn, host):
            raise SourceError(f"{host} is not an approved web source. Ask a Platform Admin to approve it (Platform knowledge, Knowledge sources).")
        _public_only(host, resolver)
        try:
            status, headers, body = (getter or _default_getter)(current)
        except httpx.HTTPError as exc:
            raise SourceError(f"Could not reach {host}.") from exc
        if status in (301, 302, 303, 307, 308):
            if not headers.get("location"):
                raise SourceError("The page redirected without saying where.")
            current = urljoin(current, headers["location"])
            continue
        break
    else:
        raise SourceError("The page redirected too many times.")
    if status != 200:
        raise SourceError(f"{host} answered HTTP {status}.")
    if len(body) > MAX_BYTES:
        raise SourceError("The page is larger than 2 MB.")
    kind = headers.get("content-type", "").split(";")[0].strip().lower()
    if kind not in WEB_TYPES:
        raise SourceError("Only HTML, text, Markdown and PDF pages can be read.")
    if kind == "application/pdf":
        title, text = parsed.path.rsplit("/", 1)[-1] or host, "\n".join(document_text.pdf_pages(body))
    elif kind == "text/html":
        title, text = html_to_text(_decode(body, headers.get("content-type", "")))
    else:
        title, text = parsed.path.rsplit("/", 1)[-1] or host, _decode(body, headers.get("content-type", ""))
    text = text.strip()
    if not text:
        raise SourceError("The page has no readable text.")
    return {"title": title or host, "text": text, "url": current, "version": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]}


# ----------------------------------------------------------------------------------------------------------------------- Confluence
def _spaces(value: str) -> List[str]:
    out = [s.strip() for s in re.split(r"[,\s]+", str(value or "")) if s.strip()]
    if not out or any(not SPACE.match(s) for s in out):
        raise SourceError("List the allowed space keys, separated by commas (letters, digits, - and _ only).")
    return sorted(set(out))


def _base(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SourceError("The Confluence address must be an https address such as https://yourco.atlassian.net/wiki (Cloud) or https://wiki.yourco.com (Data Center).")
    return f"https://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def get_config(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    ensure_tables(conn)
    row = conn.execute("SELECT * FROM confluence_connection WHERE config_id = 1").fetchone()
    if not row:
        return None
    d = dict(row)
    d["allowed_spaces"] = [s for s in str(d["allowed_spaces"]).split(",") if s]
    d["token_present"] = bool(os.environ.get(d["token_env_var"], ""))
    return d


def save_config(conn: sqlite3.Connection, actor: str, values: Dict[str, str]) -> Dict[str, Any]:
    """Save the connection settings. The token itself is never accepted: only the name of the environment variable that holds it."""
    ensure_tables(conn)
    base = _base(values.get("base_url", ""))
    auth = str(values.get("auth", "basic")).strip()
    if auth not in AUTHS:
        raise SourceError("Sign in with an account email and API token, or a personal access token.")
    email = str(values.get("email", "")).strip()
    if auth == "basic" and not EMAIL.match(email):
        raise SourceError("An account email is needed with an API token.")
    env = str(values.get("token_env_var", "")).strip() or DEFAULT_TOKEN_ENV
    if not TOKEN_ENV.match(env):
        raise SourceError("The token is read from a server environment variable whose name starts with CONFLUENCE_ (for example CONFLUENCE_API_TOKEN). Never type the token itself here.")
    spaces = ",".join(_spaces(values.get("allowed_spaces", "")))
    try:
        cap = int(str(values.get("page_cap", "50")).strip() or 50)
    except ValueError:
        raise SourceError("The page cap must be a number.")
    if not 1 <= cap <= 500:
        raise SourceError("The page cap must be between 1 and 500.")
    conn.execute("DELETE FROM confluence_connection")
    conn.execute("INSERT INTO confluence_connection (config_id, base_url, auth, email, token_env_var, allowed_spaces, page_cap, status, configured_by, configured_utc) "
                 "VALUES (1, ?, ?, ?, ?, ?, ?, 'NOT_TESTED', ?, ?)", (base, auth, email if auth == "basic" else None, env, spaces, cap, actor, _now()))
    return get_config(conn)  # type: ignore[return-value]


def _api(cfg: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None,
         getter: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    token = os.environ.get(cfg["token_env_var"], "")
    if not token:
        raise SourceError(f"The server does not hold the Confluence token (environment variable {cfg['token_env_var']} is empty). Ask whoever runs the server to set it.")
    url = f"{cfg['base_url']}/rest/api{path}"
    if getter:
        return getter(url, params or {}, cfg["auth"], cfg.get("email"), token)
    from control_plane import connection_health
    headers = {"Accept": "application/json"}
    kw: Dict[str, Any] = {"params": params or {}, "headers": headers}
    if cfg["auth"] == "basic":
        kw["auth"] = (cfg["email"], token)
    else:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = connection_health.http_request("Confluence", lambda: httpx.Client(timeout=TIMEOUT, follow_redirects=False), "GET", url, **kw)
    except httpx.HTTPError as exc:
        raise SourceError("Could not reach Confluence.") from exc
    if resp.status_code in (401, 403):
        raise SourceError("Confluence refused the saved sign-in. Ask a Platform Admin to check the account and token.")
    if resp.status_code == 404:
        raise SourceError("Confluence has no such page or space for the saved account.")
    if resp.status_code != 200:
        raise SourceError(f"Confluence answered HTTP {resp.status_code}.")
    if len(resp.content) > 5 * MAX_BYTES:
        raise SourceError("The Confluence answer is too large.")
    return resp.json()


def test_connection(conn: sqlite3.Connection, getter: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Ask Confluence for one space with the saved sign-in (read-only) and record whether it worked."""
    cfg = get_config(conn)
    if not cfg:
        raise SourceError("Save the Confluence settings first.")
    try:
        _api(cfg, "/space", {"limit": 1}, getter)
        status, detail = "CONNECTED", "Confluence answered and accepted the saved sign-in."
    except SourceError as exc:
        status, detail = "FAILED", str(exc)
    conn.execute("UPDATE confluence_connection SET status = ?, checked_utc = ?, check_detail = ? WHERE config_id = 1", (status, _now(), detail))
    return get_config(conn)  # type: ignore[return-value]


def is_connected(conn: sqlite3.Connection) -> bool:
    cfg = get_config(conn)
    return bool(cfg and cfg["status"] == "CONNECTED")


def parse_link(cfg: Dict[str, Any], link: str) -> Dict[str, Optional[str]]:
    """{"page_id", "space"} from a Confluence link on the configured host: a page (…/pages/<id> or ?pageId=<id>) or a space (…/spaces/<KEY>/… or …/display/<KEY>)."""
    parsed = urlparse(str(link or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SourceError("Use an https Confluence link.")
    if parsed.netloc.lower() != urlparse(cfg["base_url"]).netloc:
        raise SourceError(f"That link is not on the connected Confluence ({urlparse(cfg['base_url']).netloc}).")
    m = re.search(r"/pages/(\d+)", parsed.path)
    page_id = m.group(1) if m else (parse_qs(parsed.query).get("pageId") or [""])[0]
    sp = re.search(r"/(?:spaces|display)/([A-Za-z0-9~_-]+)", parsed.path)
    return {"page_id": page_id if str(page_id).isdigit() else None, "space": sp.group(1) if sp else None}


_EXPAND = "body.storage,version,space"


def _page(cfg: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    space = str(((data.get("space") or {}).get("key")) or "")
    pid = str(data.get("id") or "")
    text = confluence_source.storage_to_text(((data.get("body") or {}).get("storage") or {}).get("value", ""))
    links = data.get("_links") or {}
    url = (links.get("base", cfg["base_url"]) + links["webui"]) if links.get("webui") else f"{cfg['base_url']}/pages/viewpage.action?pageId={pid}"
    return {"page_id": pid, "title": str(data.get("title") or f"confluence-{pid}"), "text": text, "url": url, "space": space,
            "version": str(((data.get("version") or {}).get("number")) or "")}


def _allowed(cfg: Dict[str, Any], page: Dict[str, Any]) -> None:
    if page["space"] not in cfg["allowed_spaces"]:
        raise SourceError(f"Space {page['space'] or '?'} is not allowed. Ask a Platform Admin to add it to the allowed spaces.")


def _list(cfg, path, params, getter) -> List[Dict[str, Any]]:
    out, start = [], 0
    while len(out) < cfg["page_cap"] + 1:
        data = _api(cfg, path, {**params, "limit": 50, "start": start, "expand": _EXPAND}, getter)
        results = data.get("results") or []
        out += results
        if len(results) < 50:
            break
        start += 50
    return out


def fetch_pages(conn: sqlite3.Connection, link: str, mode: str = "page", getter: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """{"pages": [{page_id, title, text, url, space, version}], "truncated": bool}. mode: page, children (the page and everything below it) or space."""
    cfg = get_config(conn)
    if not cfg or cfg["status"] != "CONNECTED":
        raise SourceError("Confluence is not connected. Ask a Platform Admin to connect it (Platform knowledge, Knowledge sources).")
    if mode not in ("page", "children", "space"):
        raise SourceError("Choose page, page with children, or whole space.")
    ref = parse_link(cfg, link)
    pages: List[Dict[str, Any]] = []
    truncated = False
    if mode == "space":
        if not ref["space"]:
            raise SourceError("The link must name a space (…/spaces/KEY/…).")
        if ref["space"] not in cfg["allowed_spaces"]:
            raise SourceError(f"Space {ref['space']} is not allowed. Ask a Platform Admin to add it to the allowed spaces.")
        raw = _list(cfg, "/content", {"spaceKey": ref["space"], "type": "page"}, getter)
        pages = [_page(cfg, d) for d in raw]
    else:
        if not ref["page_id"]:
            raise SourceError("The link must point to one page (…/pages/<id>/… or ?pageId=<id>).")
        root = _page(cfg, _api(cfg, f"/content/{ref['page_id']}", {"expand": _EXPAND}, getter))
        _allowed(cfg, root)
        pages = [root]
        if mode == "children":
            frontier, depth = [root["page_id"]], 0
            while frontier and depth < 3 and len(pages) <= cfg["page_cap"]:
                nxt = []
                for parent in frontier:
                    for d in _list(cfg, f"/content/{parent}/child/page", {}, getter):
                        child = _page(cfg, d)
                        if child["space"] == root["space"] and all(p["page_id"] != child["page_id"] for p in pages):
                            pages.append(child)
                            nxt.append(child["page_id"])
                frontier, depth = nxt, depth + 1
    if len(pages) > cfg["page_cap"]:
        pages, truncated = pages[:cfg["page_cap"]], True
    pages = [p for p in pages if p["text"]]
    if not pages:
        raise SourceError("The Confluence page has no readable text.")
    return {"pages": pages, "truncated": truncated}

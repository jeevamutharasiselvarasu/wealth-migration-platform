"""Read one Confluence Cloud page as plain text for use as a knowledge asset.

Only https links on an allow-listed Atlassian Cloud host are fetched, through the read-only REST API. The account email and API
token come from the server environment (CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN) and are never accepted from a form, stored, logged
or returned. Nothing in the page is executed; its storage-format markup is reduced to visible text."""
from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

MAX_PAGE_BYTES = 2_000_000
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "pre"}


class ConfluenceError(ValueError):
    pass


def allowed_hosts() -> set:
    extra = {h.strip().lower() for h in os.environ.get("CONFLUENCE_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return extra


def _host_ok(host: str) -> bool:
    host = host.lower()
    return host.endswith(".atlassian.net") or host in allowed_hosts()


def parse_link(link: str) -> Tuple[str, str]:
    """Return (host, page_id) for a Confluence Cloud page link."""
    parsed = urlparse((link or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfluenceError("Use an https Confluence page link.")
    if not _host_ok(parsed.hostname):
        raise ConfluenceError("Only Atlassian Cloud (*.atlassian.net) links are allowed unless the host is in CONFLUENCE_ALLOWED_HOSTS.")
    match = re.search(r"/pages/(\d+)", parsed.path)
    page_id = match.group(1) if match else (parse_qs(parsed.query).get("pageId") or [""])[0]
    if not page_id.isdigit():
        raise ConfluenceError("The link must point to one page (…/pages/<id>/… or ?pageId=<id>).")
    return parsed.hostname.lower(), page_id


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def storage_to_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup or "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(parser.parts).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _get(url: str, auth: Tuple[str, str]) -> Dict:
    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        resp = client.get(url, auth=auth, headers={"Accept": "application/json"})
    if resp.status_code in (401, 403):
        raise ConfluenceError("Confluence refused the configured account for this page.")
    if resp.status_code == 404:
        raise ConfluenceError("Confluence page not found for the configured account.")
    if resp.status_code != 200:
        raise ConfluenceError(f"Confluence returned HTTP {resp.status_code}.")
    if len(resp.content) > MAX_PAGE_BYTES:
        raise ConfluenceError("The Confluence page is too large to import.")
    return resp.json()


def fetch_page(link: str, getter: Optional[Callable[[str, Tuple[str, str]], Dict]] = None) -> Dict[str, str]:
    """Return {title, text, url, page_id}. Raises ConfluenceError with a user-safe message."""
    host, page_id = parse_link(link)
    email, token = os.environ.get("CONFLUENCE_EMAIL", ""), os.environ.get("CONFLUENCE_API_TOKEN", "")
    if not email or not token:
        raise ConfluenceError("Confluence is not configured: the server needs CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN (read-only token).")
    api = f"https://{host}/wiki/rest/api/content/{page_id}?expand=body.storage"
    try:
        data = (getter or _get)(api, (email, token))
    except httpx.HTTPError as exc:
        raise ConfluenceError("Could not reach Confluence.") from exc
    title = str(data.get("title") or f"confluence-{page_id}")
    text = storage_to_text(((data.get("body") or {}).get("storage") or {}).get("value", ""))
    if not text:
        raise ConfluenceError("The Confluence page has no readable text.")
    return {"title": title, "text": text, "url": f"https://{host}/wiki/pages/{page_id}", "page_id": page_id}

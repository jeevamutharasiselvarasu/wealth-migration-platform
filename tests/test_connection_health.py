"""Every outside connection is retried three times and a failure is named on a banner; unsafe repeats never happen."""
import httpx
import pytest

from control_plane import connection_health as ch


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(ch, "RETRY_WAIT_S", 0)
    ch.reset()
    yield
    ch.reset()


class _Client:
    """Stands in for httpx.Client: runs the scripted outcomes in order."""

    def __init__(self, script, log):
        self.script, self.log = script, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, **kw):
        self.log.append(method)
        out = self.script.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _call(method, script):
    log = []
    try:
        resp = ch.http_request("GitHub", lambda: _Client(script, log), method, "https://api.example/x")
        return resp, log, None
    except Exception as exc:  # noqa: BLE001
        return None, log, exc


def test_get_that_cannot_connect_is_tried_three_times_then_banner_names_the_connection():
    _, log, exc = _call("GET", [httpx.ConnectError("no route")] * 3)
    assert isinstance(exc, httpx.ConnectError) and log == ["GET"] * 3
    assert [v["name"] for v in ch.failing()] == ["GitHub"] and "3 attempts" in ch.failing()[0]["message"]


def test_a_later_success_clears_the_banner():
    _call("GET", [httpx.ConnectError("no route")] * 3)
    assert ch.failing()
    resp, _, exc = _call("GET", [httpx.Response(200)])
    assert exc is None and resp.status_code == 200 and ch.failing() == []


def test_get_is_retried_on_503_and_recovers():
    resp, log, exc = _call("GET", [httpx.Response(503), httpx.Response(200)])
    assert exc is None and resp.status_code == 200 and log == ["GET", "GET"] and ch.failing() == []


def test_get_returns_the_service_answer_when_503_persists():
    resp, log, exc = _call("GET", [httpx.Response(503)] * 3)
    assert exc is None and resp.status_code == 503 and len(log) == 3
    assert [v["name"] for v in ch.failing()] == ["GitHub"]


def test_post_is_not_repeated_after_a_read_timeout_so_nothing_is_created_twice():
    _, log, exc = _call("POST", [httpx.ReadTimeout("slow"), httpx.Response(200)])
    assert isinstance(exc, httpx.ReadTimeout) and log == ["POST"]


def test_post_is_retried_only_when_it_never_connected():
    resp, log, exc = _call("POST", [httpx.ConnectError("no route"), httpx.Response(201)])
    assert exc is None and resp.status_code == 201 and log == ["POST", "POST"]


def test_non_transient_errors_are_not_retried_and_do_not_raise_the_banner():
    calls = []

    def once():
        calls.append(1)
        raise ValueError("bad password")

    with pytest.raises(ValueError):
        ch.retry("Azure SQL target database", once, lambda e: False)
    assert calls == [1] and ch.failing() == []


def test_tracked_decorator_records_failure_and_success():
    class Down(Exception):
        pass

    state = {"fail": True}

    @ch.tracked("Blob storage", lambda e: isinstance(e, Down))
    def read():
        if state["fail"]:
            raise Down("no route")
        return b"ok"

    with pytest.raises(Down):
        read()
    assert [v["name"] for v in ch.failing()] == ["Blob storage"]
    state["fail"] = False
    assert read() == b"ok" and ch.failing() == []


def test_every_outside_connection_goes_through_the_shared_retry():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "control_plane"
    for rel, marker in {"services/azure_connection.py": "connection_health.http_request", "services/git_connection.py": "connection_health.http_request",
                        "services/confluence_source.py": "connection_health.http_request", "services/sql_target.py": "connection_health.retry",
                        "services/blob_source.py": "connection_health.tracked", "mssql_compat.py": "connection_health"}.items():
        assert marker in (root / rel).read_text(encoding="utf-8"), rel

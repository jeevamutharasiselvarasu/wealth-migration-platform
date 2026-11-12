"""Retries and health for every outside connection (database, Azure SQL target, Azure/Entra, GitHub, Confluence, Blob storage).

Each connection has a name. A call that fails because the network or the service did not answer is retried (RETRIES attempts in all,
with a growing wait). When every attempt fails the connection is marked failed and a red banner names it on every page, until a
later call succeeds. Only safe cases are retried: a request that never reached the server (cannot connect), or a read (GET). A POST
that may have been received is never repeated, so a pipeline run or a pull request cannot be created twice.
"""
import functools
import threading
import time
from typing import Any, Callable, Dict, List

RETRIES = 3
RETRY_WAIT_S = 1.5
RETRY_STATUS = {429, 500, 502, 503, 504}

_lock = threading.Lock()
_state: Dict[str, Dict[str, Any]] = {}


def ok(name: str) -> None:
    if name in _state:
        with _lock:
            _state.pop(name, None)


def fail(name: str, exc: Exception) -> None:
    with _lock:
        _state[name] = {"name": name, "when": time.time(), "attempts": RETRIES,
                        "message": f"{name} did not answer after {RETRIES} attempts ({type(exc).__name__}). "
                                   "Check the service is running and that this machine is allowed to reach it."}


def failing() -> List[Dict[str, Any]]:
    with _lock:
        return sorted((dict(v) for v in _state.values()), key=lambda v: v["name"])


def reset() -> None:
    with _lock:
        _state.clear()


def retry(name: str, call: Callable[[], Any], transient: Callable[[Exception], bool]) -> Any:
    """Run `call` up to RETRIES times while `transient(exc)` says the error is worth another try."""
    last: Exception = RuntimeError("no attempt made")
    for attempt in range(1, RETRIES + 1):
        try:
            result = call()
        except Exception as exc:  # noqa: BLE001 - classified by `transient`
            if not transient(exc):
                raise
            last = exc
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT_S * attempt)
            continue
        ok(name)
        return result
    fail(name, last)
    raise last


def http_transient(method: str) -> Callable[[Exception], bool]:
    """httpx errors worth a retry: could not connect (any method), or timeouts and protocol drops on reads."""
    import httpx

    safe = method.upper() in ("GET", "HEAD")

    def check(exc: Exception) -> bool:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return True
        return safe and isinstance(exc, (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.PoolTimeout))

    return check


class _BadStatus(Exception):
    def __init__(self, resp: Any):
        super().__init__(f"HTTP {resp.status_code}")
        self.resp = resp


def http_request(name: str, client_factory: Callable[[], Any], method: str, url: str, **kw: Any) -> Any:
    """One HTTP call with retries; 429/5xx answers to a GET are retried too. The last response is returned, never hidden."""
    safe = method.upper() in ("GET", "HEAD")
    transient = http_transient(method)

    def once() -> Any:
        with client_factory() as client:
            resp = client.request(method, url, **kw)
        if safe and resp.status_code in RETRY_STATUS:
            raise _BadStatus(resp)
        return resp

    try:
        return retry(name, once, lambda e: isinstance(e, _BadStatus) or transient(e))
    except _BadStatus as bad:  # still 5xx after every attempt: hand the answer back so callers show the service's own message
        return bad.resp


def tracked(name: str, transient: Callable[[Exception], bool]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for libraries that retry themselves (Azure SDK): record success or failure for the banner."""
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*a: Any, **kw: Any) -> Any:
            try:
                result = fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001
                if transient(exc):
                    fail(name, exc)
                raise
            ok(name)
            return result
        return wrapper
    return deco

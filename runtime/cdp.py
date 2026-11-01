"""Minimal synchronous Chrome DevTools Protocol client (renderer pages and the Node inspector).

Only the request/response half of CDP is used; events are read and discarded. Connections are made
to 127.0.0.1 debug endpoints of disposable instances the harness (or the launcher) started itself.
"""
from __future__ import annotations

import itertools
import json
import re
import urllib.request

from websockets.sync.client import connect


class CDPError(RuntimeError):
    pass


def list_targets(port: int, timeout: float = 3.0) -> list[dict]:
    """GET /json/list on a DevTools endpoint (Chromium remote debugging port or Node --inspect port)."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as r:
        return json.load(r)


def pages(port: int, url_pattern: str | None = None) -> list[dict]:
    """Page targets, optionally filtered by a regex over the URL (in /json/list order)."""
    rx = re.compile(url_pattern) if url_pattern else None
    return [t for t in list_targets(port) if t.get("type") == "page" and (rx is None or rx.search(t.get("url", "")))]


class CDP:
    def __init__(self, ws_url: str, timeout: float = 10.0):
        self.timeout = timeout
        self._ws = connect(ws_url, max_size=None, open_timeout=timeout)
        self._ids = itertools.count(1)

    def call(self, method: str, **params) -> dict:
        i = next(self._ids)
        self._ws.send(json.dumps({"id": i, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv(timeout=self.timeout))
            if msg.get("id") != i:
                continue  # an event
            if "error" in msg:
                raise CDPError(f"{method}: {msg['error'].get('message')} {msg['error'].get('data', '')}".strip())
            return msg["result"]

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def remote_error(result: dict) -> str | None:
    """Text of a thrown exception in a Runtime.evaluate/callFunctionOn result, else None."""
    ex = result.get("exceptionDetails")
    if not ex:
        return None
    return (ex.get("exception") or {}).get("description") or ex.get("text") or "exception"

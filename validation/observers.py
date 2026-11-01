"""Harness-owned postcondition observers.

These run in the harness process (not in the target, and not written by the adapter author),
so an adapter cannot pass validation merely by reporting success. They are generic Win32
readers parameterised by the manifest; none is specific to one application.

Observer kinds:
  window_message       SendMessageTimeout(main window, msg, wparam, lparam) -> int (scalar args only)
  child_control_items  sum of SendMessageTimeout(child, count_msg) over descendants of a window class
  window_title         main window text
  top_level_windows    number of visible top-level windows of the process (optionally of one window_class)
  page_targets         (Electron) number of DevTools page targets, optionally with URL matching url_pattern;
                       read over the instance's 127.0.0.1 debugging endpoint, nothing runs in the app
  page_eval            (Electron) value of a JS expression in the first page matching url_pattern, evaluated
                       with V8 throwOnSideEffect, so the engine rejects any expression that could mutate state
Expectations: {"delta": n} (int observers), {"changed": true}, {"equals": v}.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumProc, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumProc, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = wintypes.LPARAM
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

SMTO_ABORTIFHUNG = 0x0002
KINDS = ("window_message", "child_control_items", "window_title", "top_level_windows", "page_targets", "page_eval")
PAGE_KINDS = ("page_targets", "page_eval")


class ObserverError(RuntimeError):
    pass


def _num(v):
    return int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def top_level(pid: int) -> list[int]:
    out = []

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            out.append(hwnd)
        return True
    user32.EnumWindows(EnumProc(cb), 0)
    return out


def _area(hwnd) -> int:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return max(0, r.right - r.left) * max(0, r.bottom - r.top)


def main_window(pid: int, window_class: str | None) -> int:
    """The given class's first window, else the largest visible top-level window."""
    wins = top_level(pid)
    if window_class:
        for h in wins:
            if _class(h) == window_class:
                return h
    else:
        visible = [h for h in wins if user32.IsWindowVisible(h)]
        if visible:
            return max(visible, key=_area)
    raise ObserverError(f"no {'visible' if window_class is None else window_class} top-level window in pid {pid}")


def _send(hwnd, msg, wparam=0, lparam=0) -> int:
    res = ctypes.c_size_t()
    if not user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, 2000, ctypes.byref(res)):
        raise ObserverError(f"SendMessageTimeout(0x{msg:x}) failed/timed out: winerror {ctypes.get_last_error()}")
    return ctypes.c_ssize_t(res.value).value


def _page(ctx: dict | None, spec: dict):
    from runtime import cdp
    if not ctx or not ctx.get("cdp_port"):
        raise ObserverError(f"{spec['observer']} needs an Electron instance's debugging port")
    p = spec.get("params", {})
    try:
        if spec["observer"] == "page_targets":
            return len(cdp.pages(ctx["cdp_port"], p.get("url_pattern")))
        found = cdp.pages(ctx["cdp_port"], p.get("url_pattern"))
        if not found:
            raise ObserverError(f"no page matching {p.get('url_pattern')!r}")
        conn = cdp.CDP(found[0]["webSocketDebuggerUrl"])
        try:
            r = conn.call("Runtime.evaluate", expression=p["expression"], returnByValue=True, throwOnSideEffect=True,
                          awaitPromise=True)
        finally:
            conn.close()
    except (OSError, cdp.CDPError) as e:
        raise ObserverError(f"{spec['observer']}: {e}") from e
    err = cdp.remote_error(r)
    if err:
        raise ObserverError(f"page_eval {p['expression']!r} threw: {err}")
    return r["result"].get("value")


def observe(pid: int, spec: dict, ctx: dict | None = None):
    kind, p = spec["observer"], spec.get("params", {})
    if kind in PAGE_KINDS:
        return _page(ctx, spec)
    if kind == "window_message":
        return _send(main_window(pid, p.get("window_class")), _num(p["msg"]), _num(p.get("wparam", 0)), _num(p.get("lparam", 0)))
    if kind == "child_control_items":
        root = main_window(pid, p.get("window_class"))
        kids = []
        user32.EnumChildWindows(root, EnumProc(lambda h, _: kids.append(h) or True), 0)
        return sum(_send(h, _num(p["count_msg"])) for h in kids if _class(h) == p["control_class"])
    if kind == "window_title":
        h = main_window(pid, p.get("window_class"))
        buf = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(h, buf, 1024)
        return buf.value
    if kind == "top_level_windows":
        cls = p.get("window_class")
        return sum(1 for h in top_level(pid) if user32.IsWindowVisible(h) and (cls is None or _class(h) == cls))
    raise ObserverError(f"unknown observer {kind!r}; expected one of {KINDS}")


def holds(spec: dict, before, after) -> bool:
    exp = spec["expect"]
    if "delta" in exp:
        return isinstance(before, int) and isinstance(after, int) and after - before == exp["delta"]
    if "changed" in exp:
        return (before != after) == bool(exp["changed"])
    if "equals" in exp:
        return after == exp["equals"]
    raise ObserverError(f"unknown expectation {exp}")

"""Validation harness: run a saved adapter against a disposable target instance.

Each native instance runs from a fresh copy of the portable app directory (doLocalConf.xml keeps
all config inside that copy); each Electron instance runs with a fresh temp --user-data-dir and
127.0.0.1 debugging ports. Validation never touches the user's real application state.

Pass criteria (all required):
  - manifest schema and static lint pass (no absolute addresses / no UI-input or IPC injection,
    every Electron function handle carries a source_sha256 guard)
  - preflight check() succeeds
  - every call returns ok AND every harness-evaluated postcondition holds (observers.py)
  - the process is alive afterwards; extra arguments are rejected
  - all of the above again after a clean restart

Usage:
  python -m validation.harness notepadpp new_document [--calls 3]
  python -m validation.harness --adapter-dir PATH [--calls 3] [--no-write]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_server import muse  # noqa: E402
from runtime.bridge import Adapter, BridgeError, Session  # noqa: E402
from runtime.electron_bridge import CONTEXTS, ElectronAdapter, ElectronInstance, ElectronSession, load_any  # noqa: E402
from validation import observers  # noqa: E402

TARGETS = {
    "notepadpp": {
        "app_dir": ROOT / "targets" / "notepadpp" / "app",
        "exe": "notepad++.exe",
        "args": ["-multiInst", "-nosession"],
    },
    "sumatrapdf": {
        "app_dir": ROOT / "targets" / "sumatrapdf" / "app",
        "exe": "SumatraPDF-3.6.1-64.exe",
        "args": [],  # unique copy path => unique single-instance key; settings file disables update checks
    },
    "marktext": {
        "kind": "electron",
        "app_dir": ROOT / "targets" / "marktext" / "app",
        "exe": "marktext.exe",
        "args": [],  # the temp --user-data-dir also keys the single-instance lock; updates are user-initiated only
        "extracted": ROOT / "targets" / "marktext" / "extracted",
    },
}

REQUIRED = {
    "tool": ["name", "description", "inputSchema", "mutates_state", "destructive"],
    "app": ["name", "version", "module", "sha256", "pe_timestamp", "target"],
    "resolution": ["strategy", "function", "calling_convention", "thread_affinity"],
}
# Absolute addresses (image-base-relative Ghidra addresses or live pointers) must not be baked in.
ABSOLUTE_ADDR = re.compile(r"0x(?:7ff[0-9a-f]{9}|1[0-9a-f]{8,})\b", re.I)
# Electron: synthesized input, DOM event dispatch, menu-item clicks and IPC messages route through the UI or
# a message handler rather than calling the internal function (the JS analogue of WM_COMMAND).
JS_INJECTION = re.compile(r"\b(sendInputEvent|dispatchEvent|execCommand|Input\.dispatch\w*|new\s+(?:Mouse|Keyboard|Pointer|"
                          r"Input|Touch)Event|ipcRenderer|ipcMain|webContents\.send|postMessage|executeJavaScript)\b"
                          r"|(\.(?:click|emit)\s*\()", re.I)
UI_INJECTION = re.compile(r"\b(SendInput|keybd_event|mouse_event|WM_COMMAND|WM_SYSCOMMAND|WM_KEYDOWN|WM_CHAR|"
                          r"0x0*111\b|0x0*112\b|BM_CLICK)", re.I)


def kind_of(target: str) -> str:
    return TARGETS.get(target, {}).get("kind", "native")


def static_checks(adapter) -> list[str]:
    # Every published tool must be callable from Meta Muse Spark (see mcp_server/muse.py).
    muse_problems = muse.tool_problems(adapter.manifest.get("tool", {}))
    if isinstance(adapter, ElectronAdapter):
        return muse_problems + static_checks_electron(adapter)
    problems = list(muse_problems)
    m = adapter.manifest
    for section, keys in REQUIRED.items():
        for k in keys:
            if k not in m.get(section, {}):
                problems.append(f"manifest.{section}.{k} missing")
    if m.get("app", {}).get("target") not in TARGETS:
        problems.append(f"manifest.app.target must be one of {list(TARGETS)}")
    posts = m.get("postcondition")
    if not isinstance(posts, list) or not posts:
        problems.append("manifest.postcondition must be a non-empty list of observer specs")
    else:
        for i, p in enumerate(posts):
            if p.get("observer") not in observers.KINDS or "expect" not in p:
                problems.append(f"manifest.postcondition[{i}] needs observer in {observers.KINDS} and expect")
    if m.get("tool", {}).get("destructive"):
        problems.append("destructive tools are rejected")
    js_path = adapter.path / "adapter.js"
    if not js_path.exists():
        problems.append("adapter.js missing")
        return problems
    js = js_path.read_text(encoding="utf-8")
    cfg = json.dumps(m.get("adapter_config", {}))
    for src, text in (("adapter.js", js), ("adapter_config", cfg)):
        for hit in ABSOLUTE_ADDR.findall(text):
            problems.append(f"{src}: absolute address literal {hit} (use module-relative RVA + checks)")
    for hit in set(UI_INJECTION.findall(js)):
        problems.append(f"adapter.js: UI/command injection ({hit}) is not an in-process call")
    if "NativeFunction" not in js:
        problems.append("adapter.js never constructs a NativeFunction (no in-process call)")
    if "rpc.exports" not in js:
        problems.append("adapter.js must define rpc.exports.check and rpc.exports.invoke")
    return problems


ELECTRON_REQUIRED = {
    "tool": REQUIRED["tool"],
    "app": ["name", "version", "kind", "exe", "sha256", "asar_sha256", "electron_version", "target"],
    "resolution": ["strategy", "function", "execution_context"],
    "context": ["kind"],
}


def static_checks_electron(adapter: ElectronAdapter) -> list[str]:
    problems = []
    m = adapter.manifest
    for section, keys in ELECTRON_REQUIRED.items():
        for k in keys:
            if k not in m.get(section, {}):
                problems.append(f"manifest.{section}.{k} missing")
    target = m.get("app", {}).get("target")
    if kind_of(target) != "electron" or target not in TARGETS:
        problems.append(f"manifest.app.target must be an electron target: {[t for t in TARGETS if kind_of(t) == 'electron']}")
    if m.get("context", {}).get("kind") not in CONTEXTS:
        problems.append(f"manifest.context.kind must be one of {CONTEXTS}")
    handles = m.get("handles")
    if not isinstance(handles, dict) or not handles:
        problems.append("manifest.handles must name at least one internal function/object the adapter calls")
        handles = {}
    for name, h in handles.items():
        if not isinstance(h, dict) or not isinstance(h.get("anchor"), str) or not isinstance(h.get("path", []), list):
            problems.append(f"handles.{name}: needs anchor (str) and path (list)")
            continue
        for i, st in enumerate(h.get("path", [])):
            if not isinstance(st, dict) or len(st) != 1 or next(iter(st)) not in ("prop", "internal", "scope", "get"):
                problems.append(f"handles.{name}.path[{i}]: one of {{prop|internal|scope|get: ...}}")
        if h.get("expect") == "function" and not re.fullmatch(r"[0-9a-f]{64}", str(h.get("source_sha256", ""))):
            problems.append(f"handles.{name}: function handles need source_sha256 (hex sha256 of its source)")
    if not any(h.get("expect") == "function" for h in handles.values() if isinstance(h, dict)):
        problems.append('at least one handle must be the internal function (expect: "function")')
    posts = m.get("postcondition")
    if not isinstance(posts, list) or not posts:
        problems.append("manifest.postcondition must be a non-empty list of observer specs")
    else:
        for i, p in enumerate(posts):
            if p.get("observer") not in observers.KINDS or "expect" not in p:
                problems.append(f"manifest.postcondition[{i}] needs observer in {observers.KINDS} and expect")
    if m.get("tool", {}).get("destructive"):
        problems.append("destructive tools are rejected")
    js_path = adapter.path / "adapter.js"
    if not js_path.exists():
        return problems + ["adapter.js missing"]
    js = js_path.read_text(encoding="utf-8")
    texts = [("adapter.js", js)] + [(f"handles.{n}", json.dumps(h)) for n, h in handles.items()]
    for src, text in texts:
        for hit in {m.group(0) for m in JS_INJECTION.finditer(text)}:
            problems.append(f"{src}: UI/IPC injection ({hit}) is not a direct call of the internal function")
    if "rpc.exports" not in js:
        problems.append("adapter.js must define rpc.exports.check and rpc.exports.invoke")
    if not any(re.search(rf"\bh\s*\.\s*{re.escape(n)}\b|\bh\s*\[\s*['\"]{re.escape(n)}['\"]", js) for n in handles):
        problems.append("adapter.js never uses a resolved handle (h.<name>): no in-process call of the internal function")
    return problems


class Instance:
    def __init__(self, target: str):
        spec = TARGETS[target]
        self.workdir = Path(tempfile.mkdtemp(prefix=f"wam-{target}-"))
        self.app_dir = self.workdir / "app"
        shutil.copytree(spec["app_dir"], self.app_dir)
        self.cmd = [str(self.app_dir / spec["exe"]), *spec["args"]]
        self.proc: subprocess.Popen | None = None

    def start(self) -> int:
        self.proc = subprocess.Popen(self.cmd)
        return self.proc.pid

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.workdir, ignore_errors=True)

    observer_ctx: dict | None = None


class ElectronTargetInstance(ElectronInstance):
    """A registered Electron target, launched in place (the app dir is read-only to it) with fresh user data."""

    def __init__(self, target: str, inspect: bool = False):
        spec = TARGETS[target]
        super().__init__(spec["app_dir"] / spec["exe"], spec.get("args"), spec.get("user_data_arg", "--user-data-dir={dir}"),
                         inspect=inspect)

    @property
    def observer_ctx(self) -> dict:
        return self.ports


def make_instance(target: str, adapter=None):
    if kind_of(target) == "electron":
        inspect = isinstance(adapter, ElectronAdapter) and adapter.context.get("kind") == "main"
        return ElectronTargetInstance(target, inspect=inspect)
    return Instance(target)


def open_session(inst, adapter):
    if isinstance(adapter, ElectronAdapter):
        if not inst.wait_endpoint():
            raise BridgeError("remote debugging endpoint did not come up")
        return ElectronSession(inst.proc.pid, inst.ports, adapter)
    time.sleep(1.0)  # let the loader map the image before attaching
    return Session(inst.proc.pid, adapter)


def wait_ready(session: Session, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = session.check()
        if last.get("ok"):
            return last
        if last.get("error") not in ("window_missing", "state_not_ready", "handle_missing"):
            return last  # identity/layout failure: not going to fix itself
        time.sleep(0.25)
    return last or {"ok": False, "error": "timeout"}


def snapshot(pid: int, posts: list[dict], ctx: dict | None = None) -> list:
    out = []
    for p in posts:
        try:
            out.append(observers.observe(pid, p, ctx))
        except observers.ObserverError as e:
            out.append({"observer_error": str(e)})
    return out


def run_phase(inst, adapter, calls: int, label: str) -> dict:
    posts = adapter.manifest["postcondition"]
    pid = inst.start()
    phase = {"phase": label, "pid": pid, "calls": []}
    try:
        session = open_session(inst, adapter)
    except Exception as e:  # identity mismatch, script compile error, ...
        phase.update(passed=False, error=f"attach/load failed: {e}")
        inst.stop()
        return phase
    try:
        ready = wait_ready(session)
        phase["preflight"] = ready
        if not ready.get("ok"):
            phase["passed"] = False
            return phase
        time.sleep(0.5)
        for _ in range(calls):
            before = snapshot(pid, posts, inst.observer_ctx)
            try:
                res = session.invoke({})
            except BridgeError as e:
                res = {"ok": False, "error": "target_crashed", "message": str(e)}
            time.sleep(0.3)
            after = snapshot(pid, posts, inst.observer_ctx) if inst.proc.poll() is None else None
            checks = [{"observer": p["observer"], "why": p.get("why"), "before": b, "after": a,
                       "holds": a is not None and observers.holds(p, b, a)}
                      for p, b, a in zip(posts, before, after or [None] * len(posts))]
            call = {"adapter_result": res, "postconditions": checks,
                    "ok": bool(res.get("ok")) and all(c["holds"] for c in checks)}
            phase["calls"].append(call)
            if not call["ok"]:
                break
        time.sleep(0.5)
        phase["alive_after"] = inst.proc.poll() is None and session.alive
        if phase["alive_after"]:
            bad = session.invoke({"__unexpected__": 1})
            phase["rejects_bad_args"] = bad.get("error") == "invalid_arguments"
        else:
            phase["rejects_bad_args"] = False
        phase["passed"] = (len(phase["calls"]) == calls and all(c["ok"] for c in phase["calls"])
                           and phase["alive_after"] and phase["rejects_bad_args"])
        return phase
    finally:
        session.close()
        inst.stop()


def validate(adapter, calls: int) -> dict:
    report = {"tool": adapter.manifest.get("tool", {}).get("name"), "adapter_dir": str(adapter.path),
              "started": datetime.now(timezone.utc).isoformat(), "phases": []}
    report["static_problems"] = static_checks(adapter)
    if report["static_problems"]:
        report["passed"] = False
        return report
    inst = make_instance(adapter.manifest["app"]["target"], adapter)
    try:
        for label in ("fresh_instance", "after_restart"):
            phase = run_phase(inst, adapter, calls, label)
            report["phases"].append(phase)
            if not phase["passed"]:
                break
    finally:
        inst.cleanup()
    report["passed"] = len(report["phases"]) == 2 and all(p["passed"] for p in report["phases"])
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?")
    ap.add_argument("operation", nargs="?")
    ap.add_argument("--adapter-dir")
    ap.add_argument("--calls", type=int, default=3)
    ap.add_argument("--no-write", action="store_true", help="do not update manifest status / validation.json")
    a = ap.parse_args(argv)

    adapter = load_any(Path(a.adapter_dir) if a.adapter_dir else ROOT / "adapters" / a.target / a.operation)
    report = validate(adapter, a.calls)
    if not a.no_write:
        (adapter.path / "validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        adapter.manifest["status"] = "verified" if report["passed"] else "failed_validation"
        if report["passed"]:
            adapter.manifest["validation"] = {"report": "validation.json", "at": report["started"]}
        (adapter.path / "manifest.json").write_text(json.dumps(adapter.manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

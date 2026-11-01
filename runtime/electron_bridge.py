"""Electron runtime bridge: run a saved adapter inside an Electron app over the DevTools protocol.

Electron counterpart of runtime/bridge.py (which uses Frida for native code). The application is
launched with a remote-debugging port (renderer pages) and, where the EnableNodeCliInspectArguments
fuse allows it, a Node inspector port (main process). The bridge only executes adapters from the store.

Resolution: manifest.handles names the internal objects/functions the adapter calls. Each handle is an
anchor expression (evaluated side-effect free) followed by steps:
  {"prop": "name"}     property read via Runtime.getProperties (no page code runs; a getter is called
                       side-effect free); own or inherited
  {"internal": "[[Target]]"}   internal slot, e.g. the raw target behind a Proxy (Vue reactive objects,
                       whose traps V8 treats as side effects)
  {"scope": "name"}    variable `name` from the current function's closure scopes ([[Scopes]]), which is
                       how module-private functions of a bundle are reached without being exported
  {"get": "function () { return this.x.get('y'); }"}   accessor run on the current object (side-effect free)
Guards: `expect` (typeof the final value) and, for functions, `source_sha256` of Function.prototype.toString
(the bundle-locked analogue of the native entry-byte check). Any failure stops before the adapter runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from runtime.bridge import ADAPTERS_DIR, BridgeError, pid_alive, process_image_path, process_image_sha256
from runtime.cdp import CDP, CDPError, list_targets, pages, remote_error

WAM_ELECTRON = Path(__file__).resolve().parent / "wam_electron.js"
FUSE_SENTINEL = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"
FUSE_NAMES = ["RunAsNode", "EnableCookieEncryption", "EnableNodeOptionsEnvironmentVariable",
              "EnableNodeCliInspectArguments", "EnableEmbeddedAsarIntegrityValidation", "OnlyLoadAppFromAsar",
              "LoadBrowserProcessSpecificV8Snapshot", "GrantFileProtocolExtraPrivileges", "WasmTrapHandlers"]
CONTEXTS = ("renderer", "main")
GROUP = "wam"

DISPATCH = """async function (method, names, args, ...values) {
  const h = {};
  names.forEach((n, i) => { h[n] = values[i]; });
  try {
    const r = await this[method](h, args);
    return (r !== null && typeof r === 'object') ? r : { ok: false, error: 'bad_result', message: `adapter returned ${typeof r}` };
  } catch (e) {
    return { ok: false, error: (e && e.code) || 'exception', message: String((e && e.message) || e) };
  }
}"""
PROP = "function (k) { return this[k]; }"
SOURCE = "function () { return Function.prototype.toString.call(this); }"


# ---------------------------------------------------------------- identity

def asar_path(exe: Path) -> Path:
    return Path(exe).parent / "resources" / "app.asar"


def read_fuses(data: bytes) -> dict | None:
    i = data.find(FUSE_SENTINEL)
    if i < 0:
        return None
    s = i + len(FUSE_SENTINEL)
    n = data[s + 1]
    raw = data[s + 2:s + 2 + n].decode("ascii", "replace")
    state = {"0": "disabled", "1": "enabled", "r": "removed"}
    return {FUSE_NAMES[k] if k < len(FUSE_NAMES) else f"fuse_{k}": state.get(c, c) for k, c in enumerate(raw)}


def identity(exe: Path) -> dict:
    data = Path(exe).read_bytes()
    m = re.search(rb"Electron/(\d+\.\d+\.\d+)", data)
    asar = asar_path(exe)
    fuses = read_fuses(data)
    return {"exe": Path(exe).name, "sha256": hashlib.sha256(data).hexdigest(),
            "asar_sha256": hashlib.sha256(asar.read_bytes()).hexdigest() if asar.exists() else None,
            "electron_version": m.group(1).decode() if m else None, "fuses": fuses,
            "channels": {"renderer": True,
                         "main": fuses is None or fuses.get("EnableNodeCliInspectArguments") != "disabled"}}


# ---------------------------------------------------------------- launching

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ElectronInstance:
    """A disposable instance: fresh temp user-data dir, 127.0.0.1 debug ports. Restarting keeps the
    user-data dir (state persists, as in a real restart); cleanup() deletes it."""

    def __init__(self, exe: Path, args: list[str] | None = None, user_data_arg: str = "--user-data-dir={dir}",
                 inspect: bool = False, user_data_dir: Path | None = None):
        self.exe = Path(exe)
        self.args = list(args or [])
        self.user_data_arg = user_data_arg
        self.inspect = inspect
        self._own_dir = user_data_dir is None
        self.user_data_dir = Path(user_data_dir or tempfile.mkdtemp(prefix=f"wam-{self.exe.stem}-"))
        self.proc: subprocess.Popen | None = None
        self.ports: dict = {}

    def start(self) -> int:
        self.ports = {"cdp_port": free_port()}
        cmd = [str(self.exe), self.user_data_arg.format(dir=self.user_data_dir),
               f"--remote-debugging-port={self.ports['cdp_port']}"]
        if self.inspect:
            self.ports["inspect_port"] = free_port()
            cmd.append(f"--inspect=127.0.0.1:{self.ports['inspect_port']}")
        self.proc = subprocess.Popen(cmd + self.args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.proc.pid

    def wait_endpoint(self, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.proc.poll() is None:
            try:
                list_targets(self.ports["cdp_port"], timeout=1)
                return True
            except OSError:
                time.sleep(0.25)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            # Electron spawns GPU/renderer/utility children: end the whole tree.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def cleanup(self):
        self.stop()
        if self._own_dir:
            for _ in range(10):  # children may hold files for a moment after taskkill
                shutil.rmtree(self.user_data_dir, ignore_errors=True)
                if not self.user_data_dir.exists():
                    break
                time.sleep(0.3)


# ---------------------------------------------------------------- adapters

@dataclass
class ElectronAdapter:
    path: Path
    manifest: dict

    kind = "electron"

    @classmethod
    def from_dir(cls, path: Path) -> "ElectronAdapter":
        path = Path(path)
        return cls(path, json.loads((path / "manifest.json").read_text(encoding="utf-8")))

    @property
    def context(self) -> dict:
        return self.manifest.get("context", {"kind": "renderer"})

    def wrapper_source(self) -> str:
        """Expression that evaluates to the adapter's rpc.exports object."""
        config = {"timeout_ms": 5000, "inputSchema": self.manifest["tool"].get("inputSchema", {}),
                  **self.manifest.get("adapter_config", {})}
        js = (self.path / "adapter.js").read_text(encoding="utf-8")
        return (f"(() => {{\n'use strict';\nconst CONFIG = {json.dumps(config)};\n"
                f"{WAM_ELECTRON.read_text(encoding='utf-8')}\nconst rpc = {{ exports: {{}} }};\n{js}\n"
                f"return rpc.exports;\n}})()")


class HandleError(BridgeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _arg(remote: dict) -> dict:
    """RemoteObject -> CallArgument."""
    if "objectId" in remote:
        return {"objectId": remote["objectId"]}
    if "unserializableValue" in remote:
        return {"unserializableValue": remote["unserializableValue"]}
    return {"value": remote.get("value")} if "value" in remote else {}


def _missing(remote: dict) -> bool:
    return remote.get("type") == "undefined" or remote.get("subtype") == "null"


class Resolver:
    """Walks handle paths on one CDP connection (all objects live in GROUP until released)."""

    def __init__(self, cdp: CDP):
        self.cdp = cdp

    def evaluate(self, expression: str, side_effect_free: bool = True) -> dict:
        r = self.cdp.call("Runtime.evaluate", expression=expression, objectGroup=GROUP,
                          throwOnSideEffect=side_effect_free, awaitPromise=False)
        err = remote_error(r)
        if err:
            raise HandleError("handle_missing", f"{expression!r} threw: {err}")
        return r["result"]

    def call_on(self, remote: dict, fn: str, *args, by_value: bool = False, side_effect_free: bool = True) -> dict:
        r = self.cdp.call("Runtime.callFunctionOn", objectId=remote["objectId"], functionDeclaration=fn,
                          arguments=[{"value": a} for a in args], objectGroup=GROUP, returnByValue=by_value,
                          throwOnSideEffect=side_effect_free)
        err = remote_error(r)
        if err:
            raise HandleError("handle_missing", f"{fn[:60]!r} threw: {err}")
        return r["result"]

    def internal(self, remote: dict, name: str) -> dict | None:
        props = self.cdp.call("Runtime.getProperties", objectId=remote["objectId"], ownProperties=True)
        for p in props.get("internalProperties", []):
            if p["name"] == name:
                return p.get("value")
        return None

    def scopes(self, fn_remote: dict) -> list[dict]:
        """[{type, name, variables: {name: RemoteObject}}] innermost first (closure scopes of a function)."""
        sc = self.internal(fn_remote, "[[Scopes]]")
        if not sc:
            return []
        out = []
        listing = self.cdp.call("Runtime.getProperties", objectId=sc["objectId"], ownProperties=True)["result"]
        for entry in sorted((p for p in listing if p["name"].isdigit()), key=lambda p: int(p["name"])):
            obj = entry["value"]
            desc = obj.get("description", "")
            variables = {}
            if not desc.startswith("Global"):  # the global scope is reachable directly and is huge
                for v in self.cdp.call("Runtime.getProperties", objectId=obj["objectId"], ownProperties=True)["result"]:
                    if "value" in v:
                        variables[v["name"]] = v["value"]
            out.append({"scope": desc, "variables": variables})
        return out

    def properties(self, remote: dict) -> list[dict]:
        """Own and inherited property descriptors; no page code runs (accessors are not invoked)."""
        return self.cdp.call("Runtime.getProperties", objectId=remote["objectId"], ownProperties=False)["result"]

    def step(self, current: dict, step: dict) -> dict:
        if _missing(current) or "objectId" not in current:
            raise HandleError("handle_missing", f"cannot apply {step} to {current.get('description', current.get('type'))}")
        if "prop" in step:
            if current.get("subtype") == "proxy":
                raise HandleError("handle_missing", f"prop {step['prop']!r} on a Proxy: unwrap it first with "
                                                    '{"internal": "[[Target]]"}')
            for d in self.properties(current):  # own properties come first, then the prototype chain
                if d["name"] == step["prop"]:
                    if "value" in d:
                        return d["value"]
                    return self.call_on(current, PROP, step["prop"])  # accessor: call the getter side-effect free
            return {"type": "undefined"}
        if "internal" in step:
            v = self.internal(current, step["internal"])
            if v is None:
                raise HandleError("handle_missing", f"no internal property {step['internal']} on {current.get('description', '')[:60]!r}")
            return v
        if "get" in step:
            return self.call_on(current, step["get"])
        if "scope" in step:
            if current.get("type") != "function":
                raise HandleError("handle_missing", f"scope step {step['scope']!r} needs a function, got {current.get('type')}")
            for s in self.scopes(current):
                if step["scope"] in s["variables"]:
                    return s["variables"][step["scope"]]
            raise HandleError("handle_missing", f"variable {step['scope']!r} not in the closure scopes of "
                                                f"{current.get('description', '')[:80]!r}")
        raise HandleError("handle_missing", f"unknown step {step}")

    def describe(self, remote: dict, limit: int = 200) -> dict:
        """Exploration view of a value: properties, internal slots and (functions) closure variables."""
        out = {k: remote.get(k) for k in ("type", "subtype", "className", "description", "value") if k in remote}
        if "objectId" not in remote:
            return out
        brief = lambda v: (v.get("description") or repr(v.get("value")))[:100] if isinstance(v, dict) else None  # noqa: E731
        props = self.cdp.call("Runtime.getProperties", objectId=remote["objectId"], ownProperties=False)
        out["internal"] = {p["name"]: brief(p.get("value")) for p in props.get("internalProperties", [])
                           if p["name"] != "[[Scopes]]"}
        out["properties"] = [{"name": d["name"], "own": d.get("isOwn", False),
                              "type": d["value"].get("type") if "value" in d else "accessor",
                              "brief": brief(d.get("value"))} for d in props["result"][:limit]]
        out["truncated"] = len(props["result"]) > limit
        if remote.get("type") == "function":
            out["source_sha256"] = self.source_sha256(remote)
            out["scopes"] = [{"scope": s["scope"], "variables": {n: {"type": v.get("type"), "brief": brief(v)}
                                                                 for n, v in list(s["variables"].items())[:limit]}}
                             for s in self.scopes(remote)]
        return out

    def source_sha256(self, fn_remote: dict) -> str:
        src = self.call_on(fn_remote, SOURCE, by_value=True)["value"]
        return hashlib.sha256(src.encode("utf-8")).hexdigest()

    def resolve(self, name: str, spec: dict) -> dict:
        cur = self.evaluate(spec["anchor"])
        for i, st in enumerate(spec.get("path", [])):
            cur = self.step(cur, st)
            if _missing(cur):
                raise HandleError("handle_missing", f"handle {name}: step {i} {st} gave {cur.get('type')}")
        if _missing(cur):
            raise HandleError("handle_missing", f"handle {name}: anchor {spec['anchor']!r} is {cur.get('type')}")
        want = spec.get("expect")
        got = "object" if cur.get("type") == "object" else cur.get("type")
        if want and got != want:
            raise HandleError("state_invalid", f"handle {name}: expected {want}, got {got} ({cur.get('description', '')[:60]})")
        if spec.get("source_sha256"):
            if cur.get("type") != "function":
                raise HandleError("state_invalid", f"handle {name}: source_sha256 given but value is not a function")
            got_sha = self.source_sha256(cur)
            if got_sha != spec["source_sha256"]:
                raise HandleError("signature_mismatch", f"handle {name}: source sha256 {got_sha} != {spec['source_sha256']}")
        return cur

    def release(self):
        try:
            self.cdp.call("Runtime.releaseObjectGroup", objectGroup=GROUP)
        except (CDPError, OSError):
            pass


def ws_url(ports: dict, context: dict) -> str:
    """WebSocket URL of the adapter's execution context; HandleError('state_not_ready') if absent yet."""
    if context.get("kind") == "main":
        if not ports.get("inspect_port"):
            raise HandleError("channel_unavailable", "main-process context needs an inspector port; this target's "
                                                     "EnableNodeCliInspectArguments fuse may be disabled")
        try:
            targets = list_targets(ports["inspect_port"])
        except OSError as e:
            raise HandleError("state_not_ready", f"Node inspector not reachable: {e}") from e
        if not targets:
            raise HandleError("state_not_ready", "Node inspector lists no targets")
        return targets[0]["webSocketDebuggerUrl"]
    try:
        found = pages(ports["cdp_port"], context.get("url_pattern"))
    except OSError as e:
        raise HandleError("state_not_ready", f"remote debugging port not reachable: {e}") from e
    if not found:
        raise HandleError("state_not_ready", f"no page target matching {context.get('url_pattern')!r}")
    return found[0]["webSocketDebuggerUrl"]


def connect_context(ports: dict, context: dict, timeout: float = 10.0) -> CDP:
    return CDP(ws_url(ports, context), timeout=timeout)


class ElectronSession:
    """Runs check/invoke of one adapter against one running instance. Each call opens a fresh CDP
    connection, re-resolves and re-guards every handle, and releases its objects afterwards."""

    def __init__(self, pid: int, ports: dict, adapter: ElectronAdapter, verify_identity: bool = True):
        self.pid, self.ports, self.adapter = pid, ports, adapter
        if verify_identity:
            app = adapter.manifest["app"]
            got = process_image_sha256(pid)
            if got != app["sha256"]:
                raise BridgeError(f"pid {pid} image sha256 {got} does not match adapter's {app['sha256']}")
            asar = asar_path(process_image_path(pid))
            got = hashlib.sha256(asar.read_bytes()).hexdigest() if asar.exists() else None
            if got != app.get("asar_sha256"):
                raise BridgeError(f"{asar} sha256 {got} does not match adapter's {app.get('asar_sha256')}")

    @property
    def alive(self) -> bool:
        return pid_alive(self.pid)

    def _run(self, method: str, args: dict | None) -> dict:
        if not self.alive:
            raise BridgeError(f"pid {self.pid} is not running")
        timeout = self.adapter.manifest.get("adapter_config", {}).get("timeout_ms", 5000) / 1000 + 5
        try:
            cdp = connect_context(self.ports, self.adapter.context, timeout=timeout)
        except HandleError as e:
            return {"ok": False, "error": e.code, "message": str(e)}
        except OSError as e:
            return {"ok": False, "error": "state_not_ready", "message": f"cannot connect: {e}"}
        res = Resolver(cdp)
        try:
            handles = self.adapter.manifest.get("handles", {})
            values = [res.resolve(n, spec) for n, spec in handles.items()]
            exports = res.evaluate(self.adapter.wrapper_source(), side_effect_free=False)
            r = cdp.call("Runtime.callFunctionOn", objectId=exports["objectId"], functionDeclaration=DISPATCH,
                         arguments=[{"value": method}, {"value": list(handles)}, {"value": args or {}},
                                    *map(_arg, values)],
                         objectGroup=GROUP, awaitPromise=True, returnByValue=True)
            err = remote_error(r)
            if err:
                return {"ok": False, "error": "exception", "message": err}
            return r["result"].get("value") or {"ok": False, "error": "bad_result", "message": "no value"}
        except HandleError as e:
            return {"ok": False, "error": e.code, "message": str(e)}
        except (CDPError, OSError, TimeoutError) as e:
            if not self.alive:
                raise BridgeError(f"target exited during call: {e}") from e
            return {"ok": False, "error": "cdp_error", "message": str(e)}
        finally:
            res.release()
            cdp.close()

    def check(self) -> dict:
        return self._run("check", None)

    def invoke(self, args: dict | None = None) -> dict:
        return self._run("invoke", args or {})

    def close(self):
        pass  # connections are per call


def load_any(path: Path):
    """Adapter.from_dir or ElectronAdapter.from_dir depending on manifest.app.kind."""
    from runtime.bridge import Adapter
    m = json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))
    return (ElectronAdapter if m.get("app", {}).get("kind") == "electron" else Adapter)(Path(path), m)


def load(app: str, operation: str):
    return load_any(ADAPTERS_DIR / app / operation)


def env_ports() -> dict | None:
    """WAM_CDP_PORT / WAM_INSPECT_PORT override (for an instance launched outside the launcher)."""
    if not os.environ.get("WAM_CDP_PORT"):
        return None
    out = {"cdp_port": int(os.environ["WAM_CDP_PORT"])}
    if os.environ.get("WAM_INSPECT_PORT"):
        out["inspect_port"] = int(os.environ["WAM_INSPECT_PORT"])
    return out

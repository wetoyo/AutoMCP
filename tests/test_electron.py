"""Offline tests for the Electron pipeline: manifest lint, handle resolution (against a fake CDP),
fuse parsing, adapter wrapping, fault injection.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.run_m1 import inject_fault  # noqa: E402
from runtime import electron_bridge as eb  # noqa: E402
from runtime.bridge import Adapter  # noqa: E402
from validation import observers  # noqa: E402
from validation.harness import static_checks  # noqa: E402

SHA = "ab" * 32
GOOD_JS = """
rpc.exports = {
  check(h) { return { ok: typeof h.fn === 'function' }; },
  async invoke(h, args) { WAM.validateArgs(args, CONFIG.inputSchema); h.fn.call(h.owner, {}); return { ok: true }; },
};
"""


def manifest(**overrides) -> dict:
    m = {
        "tool": {"name": "t", "description": "d", "inputSchema": {"type": "object", "properties": {}},
                 "mutates_state": True, "destructive": False},
        "app": {"name": "A", "version": "1", "kind": "electron", "exe": "a.exe", "sha256": "0" * 64,
                "asar_sha256": "1" * 64, "electron_version": "42.1.0", "target": "marktext"},
        "context": {"kind": "renderer", "url_pattern": "index\\.html"},
        "handles": {"owner": {"anchor": "window.app", "path": [{"prop": "store"}], "expect": "object"},
                    "fn": {"anchor": "window.app", "path": [{"prop": "store"}, {"internal": "[[Target]]"},
                                                            {"prop": "act"}, {"scope": "fn"}],
                           "expect": "function", "source_sha256": SHA}},
        "resolution": {"strategy": "s", "function": "f", "execution_context": "renderer"},
        "postcondition": [{"observer": "page_eval", "params": {"expression": "1"}, "expect": {"changed": True}}],
    }
    m.update(overrides)
    return m


class AdapterDir:
    def __init__(self, m: dict, js: str = GOOD_JS):
        self.path = Path(tempfile.mkdtemp())
        (self.path / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        (self.path / "adapter.js").write_text(js, encoding="utf-8")

    def __enter__(self):
        return eb.load_any(self.path)

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


class ElectronStaticChecks(unittest.TestCase):
    def test_good_adapter_passes(self):
        with AdapterDir(manifest()) as a:
            self.assertIsInstance(a, eb.ElectronAdapter)
            self.assertEqual(static_checks(a), [])

    def test_function_handle_needs_source_hash(self):
        m = manifest()
        del m["handles"]["fn"]["source_sha256"]
        with AdapterDir(m) as a:
            self.assertTrue(any("source_sha256" in p for p in static_checks(a)))

    def test_ui_and_ipc_injection_rejected(self):
        for bad in ("document.querySelector('#new').click();", "window.electron.ipcRenderer.send('mt::new-tab');",
                    "el.dispatchEvent(new KeyboardEvent('keydown'));", "bus.emit('file-new');"):
            with AdapterDir(manifest(), GOOD_JS + bad) as a:
                self.assertTrue(any("injection" in p for p in static_checks(a)), bad)

    def test_adapter_must_use_a_handle(self):
        js = "rpc.exports = { check() { return {ok: true}; }, async invoke() { return {ok: true}; } };"
        with AdapterDir(manifest(), js) as a:
            self.assertTrue(any("never uses a resolved handle" in p for p in static_checks(a)))

    def test_schema_problems(self):
        m = manifest(context={"kind": "worker"})
        m["app"]["target"] = "notepadpp"
        m["handles"]["fn"]["path"].append({"call": "x"})
        with AdapterDir(m) as a:
            problems = static_checks(a)
        self.assertTrue(any("electron target" in p for p in problems))
        self.assertTrue(any("context.kind" in p for p in problems))
        self.assertTrue(any("path[4]" in p for p in problems))

    def test_native_manifest_still_loads_native(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "manifest.json").write_text(json.dumps({"app": {"module": "a.exe"}}), encoding="utf-8")
            self.assertIs(type(eb.load_any(d)), Adapter)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class FakeCDP:
    """Just enough of Runtime.* for Resolver: objects are dicts keyed by objectId."""

    def __init__(self, objects: dict, sources: dict):
        self.objects, self.sources, self.calls = objects, sources, []

    def call(self, method, **p):
        self.calls.append(method)
        if method == "Runtime.evaluate":
            return {"result": self.objects["anchor"]}
        if method == "Runtime.getProperties":
            o = self.objects[p["objectId"]]
            return {"result": [{"name": k, "value": v, "isOwn": True} for k, v in o.get("props", {}).items()],
                    "internalProperties": [{"name": k, "value": v} for k, v in o.get("internal", {}).items()]}
        if method == "Runtime.callFunctionOn" and p["functionDeclaration"] == eb.SOURCE:
            return {"result": {"type": "string", "value": self.sources[p["objectId"]]}}
        if method == "Runtime.releaseObjectGroup":
            return {}
        raise AssertionError(f"unexpected {method} {p}")


def remote(oid, type_="object", subtype=None, description=""):
    r = {"objectId": oid, "type": type_, "description": description}
    if subtype:
        r["subtype"] = subtype
    return r


def fake_app(fn_source="function fn() {}"):
    objects = {
        "anchor": remote("app"),
        "app": {"props": {"store": remote("proxy", subtype="proxy", description="Proxy(Object)")}},
        "proxy": {"internal": {"[[Target]]": remote("raw")}},
        "raw": {"props": {"act": remote("wrapper", "function")}},
        "wrapper": {"internal": {"[[Scopes]]": remote("scopes")}},
        "scopes": {"props": {"0": remote("closure", description="Closure (action)"),
                             "1": remote("global", description="Global")}},
        "closure": {"props": {"fn": remote("inner", "function")}},
        "global": {"props": {"fn": remote("wrong", "function")}},
    }
    return FakeCDP(objects, {"inner": fn_source})


class Resolution(unittest.TestCase):
    def test_walks_proxy_target_and_closure_scope(self):
        spec = manifest()["handles"]["fn"]
        spec["source_sha256"] = hashlib.sha256(b"function fn() {}").hexdigest()
        self.assertEqual(eb.Resolver(fake_app()).resolve("fn", spec)["objectId"], "inner")

    def test_source_hash_mismatch_fails_closed(self):
        with self.assertRaises(eb.HandleError) as cm:
            eb.Resolver(fake_app("function fn() { evil(); }")).resolve("fn", manifest()["handles"]["fn"])
        self.assertEqual(cm.exception.code, "signature_mismatch")

    def test_prop_on_proxy_needs_explicit_unwrap(self):
        spec = {"anchor": "window.app", "path": [{"prop": "store"}, {"prop": "act"}], "expect": "function"}
        with self.assertRaises(eb.HandleError) as cm:
            eb.Resolver(fake_app()).resolve("fn", spec)
        self.assertIn("[[Target]]", str(cm.exception))

    def test_missing_scope_variable(self):
        spec = {"anchor": "window.app", "path": [{"prop": "store"}, {"internal": "[[Target]]"}, {"prop": "act"},
                                                 {"scope": "nope"}]}
        with self.assertRaises(eb.HandleError) as cm:
            eb.Resolver(fake_app()).resolve("fn", spec)
        self.assertEqual(cm.exception.code, "handle_missing")

    def test_type_guard(self):
        spec = {"anchor": "window.app", "path": [{"prop": "store"}], "expect": "function"}
        with self.assertRaises(eb.HandleError) as cm:
            eb.Resolver(fake_app()).resolve("owner", spec)
        self.assertEqual(cm.exception.code, "state_invalid")


class Misc(unittest.TestCase):
    def test_fuses(self):
        data = b"xx" + eb.FUSE_SENTINEL + bytes([1, 4]) + b"10r1" + b"yy"
        self.assertEqual(eb.read_fuses(data), {"RunAsNode": "enabled", "EnableCookieEncryption": "disabled",
                                               "EnableNodeOptionsEnvironmentVariable": "removed",
                                               "EnableNodeCliInspectArguments": "enabled"})
        self.assertIsNone(eb.read_fuses(b"no wire"))

    def test_wrapper_carries_config_and_returns_exports(self):
        m = manifest(adapter_config={"k": 1})
        with AdapterDir(m) as a:
            src = a.wrapper_source()
        self.assertIn('"inputSchema": {"type": "object", "properties": {}}', src)
        self.assertIn('"k": 1', src)
        self.assertIn("const WAM =", src)
        self.assertTrue(src.rstrip().endswith("return rpc.exports;\n})()"))

    def test_inject_fault_flips_source_hash(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "manifest.json").write_text(json.dumps(manifest()), encoding="utf-8")
            info = inject_fault(d / "manifest.json")
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(info["key"], "handles.fn.source_sha256")
            self.assertEqual(m["handles"]["fn"]["source_sha256"], SHA[:-1] + "4")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_page_observers_need_a_debug_port(self):
        with self.assertRaises(observers.ObserverError):
            observers.observe(1, {"observer": "page_targets", "params": {}})


if __name__ == "__main__":
    unittest.main()

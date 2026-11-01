"""Offline unit tests for framework pieces that don't need a live target.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.run_m1 import audit, inject_fault  # noqa: E402
from runtime.bridge import Adapter, _ints  # noqa: E402
from validation import observers  # noqa: E402
from validation.harness import static_checks  # noqa: E402

GOOD_JS = """
rpc.exports = { check() { return {ok: true}; },
  async invoke(args) { const f = new NativeFunction(WAM.module(CONFIG.module).base.add(CONFIG.rva), 'void', []); f(); return {ok: true}; } };
"""


def manifest(**overrides) -> dict:
    m = {
        "tool": {"name": "t", "description": "d", "inputSchema": {"type": "object"}, "mutates_state": True,
                 "destructive": False},
        "app": {"name": "A", "version": "1", "module": "a.exe", "sha256": "0" * 64, "pe_timestamp": "0x1",
                "target": "notepadpp"},
        "adapter_config": {"rva": "0x1000", "entry_hex": "48895c2408"},
        "resolution": {"strategy": "s", "function": "f", "calling_convention": "c", "thread_affinity": "t"},
        "postcondition": [{"observer": "window_title", "expect": {"changed": True}}],
    }
    m.update(overrides)
    return m


class AdapterDir:
    def __init__(self, m: dict, js: str = GOOD_JS):
        self.path = Path(tempfile.mkdtemp())
        (self.path / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        (self.path / "adapter.js").write_text(js, encoding="utf-8")

    def __enter__(self) -> Adapter:
        return Adapter.from_dir(self.path)

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


class StaticChecks(unittest.TestCase):
    def test_good_adapter_passes(self):
        with AdapterDir(manifest()) as a:
            self.assertEqual(static_checks(a), [])

    def test_absolute_address_rejected(self):
        with AdapterDir(manifest(), GOOD_JS + "\nconst f = ptr('0x14013c7a0');") as a:
            self.assertTrue(any("absolute address" in p for p in static_checks(a)))
        cfg = manifest(adapter_config={"addr": "0x7ff6a1b2c3d4"})
        with AdapterDir(cfg) as a:
            self.assertTrue(any("absolute address" in p for p in static_checks(a)))

    def test_ui_injection_rejected(self):
        with AdapterDir(manifest(), GOOD_JS + "\nSendMessageW(h, WM_COMMAND, ptr(41001), ptr(0));") as a:
            self.assertTrue(any("injection" in p for p in static_checks(a)))

    def test_no_native_call_rejected(self):
        with AdapterDir(manifest(), "rpc.exports = {check(){}, invoke(){}};") as a:
            self.assertTrue(any("NativeFunction" in p for p in static_checks(a)))

    def test_postcondition_and_destructive_required(self):
        with AdapterDir(manifest(postcondition=[])) as a:
            self.assertTrue(any("postcondition" in p for p in static_checks(a)))
        m = manifest()
        m["tool"]["destructive"] = True
        with AdapterDir(m) as a:
            self.assertTrue(any("destructive" in p for p in static_checks(a)))


class Postconditions(unittest.TestCase):
    def test_holds(self):
        self.assertTrue(observers.holds({"expect": {"delta": 1}}, 2, 3))
        self.assertFalse(observers.holds({"expect": {"delta": 1}}, 2, 4))
        self.assertFalse(observers.holds({"expect": {"delta": 1}}, {"observer_error": "x"}, 3))
        self.assertTrue(observers.holds({"expect": {"changed": True}}, "a", "b"))
        self.assertFalse(observers.holds({"expect": {"changed": True}}, "a", "a"))
        self.assertTrue(observers.holds({"expect": {"equals": 5}}, 1, 5))


class Bridge(unittest.TestCase):
    def test_hex_strings_become_ints(self):
        self.assertEqual(_ints({"rva": "0x13c7a0", "bytes": "48895c", "n": [{"o": "0x20"}]}),
                         {"rva": 0x13c7a0, "bytes": "48895c", "n": [{"o": 0x20}]})


class Orchestrator(unittest.TestCase):
    def test_inject_fault_flips_entry_bytes_only(self):
        with AdapterDir(manifest()) as a:
            info = inject_fault(a.path / "manifest.json")
            cfg = json.loads((a.path / "manifest.json").read_text(encoding="utf-8"))["adapter_config"]
            self.assertEqual(info["key"], "entry_hex")
            self.assertEqual(cfg["rva"], "0x1000")
            self.assertNotEqual(cfg["entry_hex"], "48895c2408")
            self.assertEqual(cfg["entry_hex"][:-1], "48895c240")

    def test_audit_flags_prior_results_but_not_own_run(self):
        own = ROOT / "runs" / "x"
        inputs = [{"file_path": str(ROOT / "adapters" / "notepadpp" / "new_document" / "adapter.js")},
                  {"file_path": str(own / "workspace" / "TASK.md")},
                  {"command": "./wam probe scratch/a.js"},
                  {"command": "cat ../../../adapters/x/adapter.js"}]
        tmp = Path(tempfile.mkdtemp())
        try:
            log = tmp / "log.jsonl"
            log.write_text("\n".join(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": i}]}}) for i in inputs), encoding="utf-8")
            flagged = [v["input"] for v in audit(log, own)["forbidden_access"]]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn(inputs[0], flagged)
        self.assertIn(inputs[3], flagged)
        self.assertNotIn(inputs[1], flagged)
        self.assertNotIn(inputs[2], flagged)

    def test_audit_allows_only_own_session_tool_results(self):
        projects = Path.home() / ".claude" / "projects"
        sid = "4686e1b4-9b9e-4992-96f8-8eccd11b5f70"
        inputs = [{"path": str(projects / "D--ws-runs-x-workspace" / sid / "tool-results" / "a.txt")},
                  {"path": str(projects / "D--ws" / "0000aaaa-other-session" / "tool-results" / "a.txt")},
                  {"path": str(projects / "D--ws" / "memory" / "MEMORY.md")}]
        tmp = Path(tempfile.mkdtemp())
        try:
            log = tmp / "log.jsonl"
            log.write_text("\n".join(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Grep", "input": i}]}}) for i in inputs), encoding="utf-8")
            flagged = [v["input"] for v in audit(log, ROOT / "runs" / "x", sid)["forbidden_access"]]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(flagged, inputs[1:])

    def test_audit_tolerates_system_events_and_records_denials(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            log = tmp / "log.jsonl"
            log.write_text("\n".join([
                json.dumps({"type": "system", "subtype": "permission_denied", "tool_name": "Bash",
                            "tool_use_id": "t1", "message": "Permission to use Bash has been denied"}),
                json.dumps([1, 2]), "not json",
                json.dumps({"type": "result", "subtype": "success", "num_turns": 3})]), encoding="utf-8")
            r = audit(log, ROOT / "runs" / "x")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(r["permission_denials"], [{"tool": "Bash", "tool_use_id": "t1"}])
        self.assertEqual(r["result"]["subtype"], "success")


if __name__ == "__main__":
    unittest.main()

import argparse
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from orchestrator import agent_loop, provider
from orchestrator.run_m1 import audit
from orchestrator.workbench import Workbench, WorkbenchError


class WorkbenchPolicyTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.ws, self.src, self.secret = root / "ws", root / "src", root / "secret"
        for d in (self.ws / "adapter", self.src / "private", self.secret):
            d.mkdir(parents=True)
        (self.ws / "TASK.md").write_text("task\n")
        (self.src / "a.c").write_text("int fileNew() {}\n")
        (self.src / "private" / "x.c").write_text("hidden\n")
        (self.secret / "s.txt").write_text("no\n")
        self.wb = Workbench({"workspace": str(self.ws), "read_roots": [str(self.ws), str(self.src)],
                             "write_dirs": ["adapter"], "forbidden": [str(self.src / "private")],
                             "commands": {"wam": [sys.executable, "-c", "import sys; print(sys.argv[1:])"]}})

    def test_reads_inside_roots_only(self):
        self.assertIn("task", self.wb.read_file("TASK.md"))
        self.assertIn("fileNew", self.wb.read_file(str(self.src / "a.c")))
        for bad in (str(self.secret / "s.txt"), "../secret/s.txt", str(self.src / "private" / "x.c")):
            with self.assertRaises(WorkbenchError, msg=bad):
                self.wb.read_file(bad)

    def test_search_and_list_skip_forbidden(self):
        self.assertIn("a.c:1:", self.wb.search_files("fileNew|hidden", str(self.src)))
        self.assertNotIn("hidden", self.wb.search_files("hidden", str(self.src)))
        self.assertNotIn("x.c", self.wb.list_files(str(self.src)))

    def test_writes_only_in_write_dirs(self):
        self.wb.write_file("adapter/manifest.json", "{}")
        self.wb.edit_file("adapter/manifest.json", "{}", '{"a": 1}')
        self.assertEqual((self.ws / "adapter" / "manifest.json").read_text(), '{"a": 1}')
        for bad in ("TASK.md", "../x", "adapter/../TASK.md"):
            with self.assertRaises(WorkbenchError, msg=bad):
                self.wb.write_file(bad, "x")

    def test_command(self):
        self.assertIn("['identity']", self.wb.run_command("wam", ["identity"]))


class FakeHttp:
    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    async def post(self, url, json):
        self.bodies.append(json)
        reply = self.replies.pop(0)
        return SimpleNamespace(status_code=200, json=lambda: reply, text="")


class FakeSession:
    async def call_tool(self, name, args):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=f"{name} ok")])


def tool(name, description="d"):
    return SimpleNamespace(name=name, description=description, input_schema={"type": "object", "properties": {}})


class AgentLoopTests(unittest.TestCase):
    def test_loop_transcript_is_auditable(self):
        run = Path(tempfile.mkdtemp())
        router = agent_loop.Router()
        router.add(FakeSession(), [tool("read_file"), tool("wam")])
        router.add(FakeSession(), [tool("open_binary"), tool("start_frida_session")], prefix="rea_",
                   allow=agent_loop.REA_TOOLS)
        self.assertEqual([t["name"] for t in router.tools], ["read_file", "wam", "rea_open_binary"])
        forbidden = str(Path.home() / ".claude" / "x")
        http = FakeHttp([
            {"id": "r1", "output": [{"type": "function_call", "name": "read_file", "call_id": "c1",
                                     "arguments": json.dumps({"path": forbidden})}],
             "usage": {"input_tokens": 100, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 40}}},
            {"id": "r2", "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
             "usage": {"input_tokens": 50, "output_tokens": 5}},
        ])
        log = run / "attempt-1.jsonl"
        result = asyncio.run(agent_loop.run(http, router, agent_loop.Transcript(log), "muse-spark-1.3", "go", None,
                                            10, run / "state.json"))
        self.assertEqual(result["subtype"], "success")
        self.assertEqual(result["usage"], {"input_tokens": 110, "cache_read_input_tokens": 40, "output_tokens": 15})
        self.assertEqual(json.loads((run / "state.json").read_text())["previous_response_id"], "r2")
        self.assertEqual(http.bodies[1]["previous_response_id"], "r1")
        report = audit(log, run)
        self.assertEqual(report["tool_counts"], {"read_file": 1})
        self.assertEqual(len(report["forbidden_access"]), 1)
        self.assertEqual(report["result"]["subtype"], "success")

    def test_turn_limit(self):
        call = {"id": "r", "output": [{"type": "function_call", "name": "wam", "call_id": "c", "arguments": "{}"}]}
        router = agent_loop.Router()
        router.add(FakeSession(), [tool("wam")])
        run = Path(tempfile.mkdtemp())
        result = asyncio.run(agent_loop.run(FakeHttp([call] * 3), router, agent_loop.Transcript(run / "l.jsonl"),
                                            "m", "go", None, 2, run / "s.json"))
        self.assertEqual(result["subtype"], "error_max_turns")


class HarnessFlagTests(unittest.TestCase):
    def parse(self, *argv):
        ap = argparse.ArgumentParser()
        provider.add_args(ap)
        return ap.parse_args(list(argv))

    def test_native_requires_muse_spark(self):
        with self.assertRaises(provider.ProviderError):
            provider.resolve_model(self.parse("--harness", "native", "--provider", "anthropic"))
        self.assertEqual(provider.resolve_model(self.parse("--harness", "claude", "--provider", "anthropic")), "opus")


if __name__ == "__main__":
    unittest.main()

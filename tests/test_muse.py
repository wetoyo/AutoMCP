import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from mcp_server import muse, muse_chat

ROOT = Path(__file__).resolve().parent.parent


class ToolRuleTests(unittest.TestCase):
    def test_saved_adapters_are_muse_compatible(self):
        manifests = sorted((ROOT / "adapters").glob("*/*/manifest.json"))
        if not manifests:
            self.skipTest("no local adapters (adapters/ is git-ignored)")
        for p in manifests:
            tool = json.loads(p.read_text(encoding="utf-8"))["tool"]
            self.assertEqual(muse.tool_problems(tool), [], p)

    def test_rejects_what_meta_rejects(self):
        ok = {"name": "app_do_thing", "description": "Does the thing.", "inputSchema": {"type": "object"}}
        self.assertEqual(muse.tool_problems(ok), [])
        for bad in ({"name": "app.do.thing"}, {"name": "app do"}, {"name": "x" * 65}, {"description": ""},
                    {"inputSchema": {"type": "array"}}, {"inputSchema": {"oneOf": [{"type": "object"}]}},
                    {"inputSchema": {"type": "object", "properties": {"a": {"allOf": [{}]}}}}):
            self.assertTrue(muse.tool_problems(ok | bad), bad)
        deep = {"type": "object"}
        for _ in range(11):
            deep = {"type": "object", "properties": {"a": deep}}
        self.assertTrue(muse.tool_problems(ok | {"inputSchema": deep}))

    def test_function_tool_shape(self):
        t = muse.function_tool("n", "d", {"title": "nArguments", "type": "object", "properties": {}})
        self.assertEqual(t, {"type": "function", "name": "n", "description": "d", "strict": False,
                             "parameters": {"type": "object", "properties": {}}})


class FakeHttp:
    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    async def post(self, url, json):
        self.bodies.append(json)
        return SimpleNamespace(status_code=200, json=lambda r=self.replies.pop(0): r, text="")


class FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": true}')], isError=False)


class LoopTests(unittest.TestCase):
    def test_tool_call_round_trip(self):
        http = FakeHttp([
            {"id": "r1", "output": [{"type": "function_call", "name": "app_new", "arguments": "{}", "call_id": "c1"}]},
            {"id": "r2", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Done."}]}]},
        ])
        session = FakeSession()
        answer, rid = asyncio.run(muse_chat.turn(http, session, [{"name": "app_new"}], "muse-spark-1.3",
                                                 "make one", None, 4))
        self.assertEqual((answer, rid), ("Done.", "r2"))
        self.assertEqual(session.calls, [("app_new", {})])
        follow = http.bodies[1]
        self.assertEqual(follow["previous_response_id"], "r1")
        self.assertEqual(follow["input"], [{"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'}])
        self.assertIn("tools", follow)  # Meta needs tools on every request

    def test_round_limit(self):
        call = {"id": "r", "output": [{"type": "function_call", "name": "t", "arguments": "{}", "call_id": "c"}]}
        with self.assertRaises(muse_chat.MuseError):
            asyncio.run(muse_chat.turn(FakeHttp([call] * 3), FakeSession(), [], "m", "x", None, 2))


if __name__ == "__main__":
    unittest.main()

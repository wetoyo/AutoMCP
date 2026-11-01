"""Use the published tools from Meta Muse Spark.

Starts the windows-auto-mcp server over stdio, offers its tools to Muse Spark (Meta Model API, Responses API) as
function tools, and runs each call the model makes through MCP. No other client or SDK is needed.

Usage (needs MODEL_API_KEY):
  python -m mcp_server.muse_chat "Open a new document in Notepad++"     one request
  python -m mcp_server.muse_chat                                        interactive; empty line or Ctrl+C ends
  python -m mcp_server.muse_chat --check                                Muse Spark accepts every published tool?
  python -m mcp_server.muse_chat --print-tools                          the tool definitions as sent to Meta (no key)
Options: --model muse-spark-1.3 | muse-spark-1.1 | muse-spark-1.3-contributor, --server-arg ARG (repeatable,
passed to mcp_server.server, e.g. --server-arg=--include-unapproved), --max-rounds N.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import urllib.error
import urllib.request
from types import SimpleNamespace

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_server import muse  # noqa: E402
from orchestrator.provider import META_KEY_ENV, MUSE_SPARK_PRICES  # noqa: E402

INSTRUCTIONS = ("You control Windows applications through the provided tools. Each tool calls one internal function "
                "of a running app and returns JSON with an 'ok' field. Call a tool only when the user's request "
                "needs it, then report what happened, including any error message.")


class MuseError(RuntimeError):
    pass


class MetaClient:
    """Minimal async POST to the Meta Model API (stdlib only)."""

    def __init__(self, key: str, timeout: float = 300):
        self.headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.timeout = timeout

    def _post(self, url: str, body: dict):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                status, text = r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode("utf-8", "replace")
        return SimpleNamespace(status_code=status, text=text, json=lambda: json.loads(text))

    async def post(self, url: str, json: dict):  # noqa: A002 (mirrors httpx)
        return await asyncio.to_thread(self._post, url, json)


async def respond(http: MetaClient, body: dict) -> dict:
    r = await http.post(muse.RESPONSES_URL, json=body)
    if r.status_code != 200:
        raise MuseError(f"Meta Model API HTTP {r.status_code}: {r.text[:2000]}")
    return r.json()


def output_text(resp: dict) -> str:
    parts = []
    for item in resp.get("output", []):
        if item.get("type") == "message":
            parts += [c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text"]
    return "\n".join(p for p in parts if p)


def schema_of(tool) -> dict:
    """The MCP tool's input schema (mcp>=2 names it input_schema, 1.x inputSchema)."""
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {"type": "object"}


async def call_mcp(session: ClientSession, name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": "bad_arguments", "message": str(e)})
    res = await session.call_tool(name, args)
    text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
    return text or json.dumps({"ok": not getattr(res, "is_error", getattr(res, "isError", False))})


async def turn(http, session, tools, model, user_input, previous_id, max_rounds) -> tuple[str, str]:
    """One user turn: run the model's tool calls until it answers. Returns (answer, response id)."""
    body = {"model": model, "tools": tools,
            "input": [{"role": "developer", "content": INSTRUCTIONS}, {"role": "user", "content": user_input}]
            if previous_id is None else [{"role": "user", "content": user_input}]}
    if previous_id:
        body["previous_response_id"] = previous_id
    resp = await respond(http, body)
    for _ in range(max_rounds):
        calls = [i for i in resp.get("output", []) if i.get("type") == "function_call"]
        if not calls:
            return output_text(resp), resp["id"]
        outputs = []
        for c in calls:
            print(f"  -> {c['name']}({c.get('arguments') or '{}'})", file=sys.stderr)
            result = await call_mcp(session, c["name"], c.get("arguments"))
            print(f"  <- {result}", file=sys.stderr)
            outputs.append({"type": "function_call_output", "call_id": c["call_id"], "output": result})
        resp = await respond(http, {"model": model, "tools": tools, "input": outputs,
                                    "previous_response_id": resp["id"]})
    raise MuseError(f"no answer after {max_rounds} rounds of tool calls")


async def main(a) -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server", *a.server_arg],
                                   cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        listed = (await session.list_tools()).tools
        tools, bad = [], {}
        for t in listed:
            problems = muse.tool_problems({"name": t.name, "description": t.description, "inputSchema": schema_of(t)})
            if problems:
                bad[t.name] = problems
            else:
                tools.append(muse.function_tool(t.name, t.description, schema_of(t)))
        for name, problems in bad.items():
            print(f"[muse] not offered {name}: {'; '.join(problems)}", file=sys.stderr)
        if a.print_tools:
            print(json.dumps(tools, indent=2))
            return 0 if not bad else 1
        if not tools:
            print("[muse] no tools published; see the server's messages above (approve tools, or pass "
                  "--server-arg=--include-unapproved for testing)", file=sys.stderr)
            return 1
        key = os.environ.get(META_KEY_ENV)
        if not key:
            print(f"{META_KEY_ENV} is not set; create a key at https://dev.meta.ai", file=sys.stderr)
            return 2
        print(f"[muse] {a.model} with tools: {[t['name'] for t in tools]}", file=sys.stderr)
        http = MetaClient(key)
        if a.check:  # Meta validates tool definitions on every request; a 200 means all were accepted.
            await respond(http, {"model": a.model, "tools": tools, "max_output_tokens": 16,
                                 "input": [{"role": "user", "content": "Reply OK."}]})
            print(json.dumps({"ok": not bad, "accepted": [t["name"] for t in tools], "rejected": bad}))
            return 0 if not bad else 1
        previous = None
        while True:
            if a.prompt is None:
                try:
                    text = (await asyncio.to_thread(input, "you> ")).strip()
                except (EOFError, KeyboardInterrupt):
                    return 0
                if not text:
                    return 0
            else:
                text = a.prompt
            answer, previous = await turn(http, session, tools, a.model, text, previous, a.max_rounds)
            print(answer)
            if a.prompt is not None:
                return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Use the published Windows tools from Meta Muse Spark.")
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--model", default="muse-spark-1.3", choices=list(MUSE_SPARK_PRICES))
    ap.add_argument("--server-arg", action="append", default=[])
    ap.add_argument("--max-rounds", type=int, default=8)
    ap.add_argument("--check", action="store_true", help="send the tool definitions to Meta once and report")
    ap.add_argument("--print-tools", action="store_true", help="print the function tools; no API call")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(main(args)))
    except MuseError as e:
        print(f"[muse] {e}", file=sys.stderr)
        sys.exit(1)

"""Harness-free discovery agent: Muse Spark + MCP tools, no agent CLI.

Connects to the run's workbench (orchestrator.workbench) and to REA over stdio, offers their tools to Muse Spark
(Meta Model API, Responses API) as function tools, and runs the model's tool calls until it stops calling tools or
hits --max-turns. The transcript is written as JSONL in the format the orchestrator's audit reads, and the last
response id is saved so a later attempt can resume the same conversation.

  python -m orchestrator.agent_loop --workbench <run>/workbench.json --mcp-config <run>/mcp.json \
      --log <run>/attempt-1.jsonl --state <run>/agent_state.json [--resume] < prompt.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_server import muse  # noqa: E402
from mcp_server.muse_chat import MetaClient, MuseError, respond, schema_of  # noqa: E402
from orchestrator.provider import META_KEY_ENV, MUSE_SPARK_PRICES  # noqa: E402

# REA tools offered to the agent: static analysis only (no process instrumentation, devices or debuggers).
REA_TOOLS = [
    "open_binary", "close_binary", "binary_overview", "list_segments", "list_strings", "search_strings", "list_names",
    "search_procedures", "list_procedures", "procedure_info", "procedure_pseudo_code", "procedure_assembly",
    "procedure_callers", "procedure_callees", "procedure_references", "xrefs", "find_xrefs_to_name",
    "resolve_containing_procedure", "inspect_address_context", "read_bytes", "search_bytes",
    "address_to_file_offset", "analyze_function", "get_call_graph", "build_call_path", "inspect_native_instruction",
    "inspect_pe_resources", "analyze_javascript_application", "trace_javascript_semantics",
    "trace_application_feature", "recover_javascript_sources", "inspect_electron_fuses",
]
MAX_TOOL_OUTPUT = 60_000
INSTRUCTIONS = ("You are a software engineering agent working alone, with no human to ask. Use the tools to do the "
                "task in ./TASK.md. Verify claims with the tools rather than guessing. When the task is complete, or "
                "you have written the file the task says to write when it cannot be done, reply with a short final "
                "summary and no tool calls.")


class Transcript:
    def __init__(self, path: Path):
        self.f = open(path, "a", encoding="utf-8")

    def write(self, event: dict) -> None:
        self.f.write(json.dumps(event) + "\n")
        self.f.flush()

    def tool_use(self, name: str, args, call_id: str) -> None:
        self.write({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": call_id, "name": name, "input": args}]}})

    def text(self, text: str) -> None:
        if text:
            self.write({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def usage_add(total: dict, resp: dict) -> None:
    u = resp.get("usage") or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    total["input_tokens"] += (u.get("input_tokens") or 0) - cached
    total["cache_read_input_tokens"] += cached
    total["output_tokens"] += u.get("output_tokens") or 0


class Router:
    """Function-tool name -> (MCP session, MCP tool name)."""

    def __init__(self):
        self.routes, self.tools, self.skipped = {}, [], {}

    def add(self, session: ClientSession, listed, prefix: str = "", allow: list[str] | None = None,
            relax: bool = False) -> None:
        for t in listed:
            if allow is not None and t.name not in allow:
                continue
            name = prefix + t.name
            schema = muse.relax_schema(schema_of(t)) if relax else schema_of(t)
            spec = {"name": name, "description": t.description, "inputSchema": schema}
            problems = muse.tool_problems(spec)
            if problems or name in self.routes:
                self.skipped[name] = problems or ["duplicate name"]
                continue
            self.routes[name] = (session, t.name)
            self.tools.append(muse.function_tool(name, t.description, schema))

    async def call(self, name: str, arguments: str) -> tuple[dict | str, str]:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            return arguments, f"error: arguments are not JSON: {e}"
        if name not in self.routes:
            return args, f"error: unknown tool {name!r}"
        session, tool = self.routes[name]
        try:
            res = await session.call_tool(tool, args)
        except Exception as e:  # a failing tool must not end the run
            return args, f"error: {type(e).__name__}: {e}"
        text = "\n".join(getattr(c, "text", "") for c in res.content if getattr(c, "type", "") == "text")
        if len(text) > MAX_TOOL_OUTPUT:
            text = text[:MAX_TOOL_OUTPUT] + f"\n... [truncated {len(text) - MAX_TOOL_OUTPUT} chars]"
        return args, text or "(no output)"


async def run(http, router: Router, transcript: Transcript, model: str, prompt: str, previous: str | None,
              max_turns: int, state_path: Path) -> dict:
    usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    head = [] if previous else [{"role": "developer", "content": INSTRUCTIONS}]
    body = {"model": model, "tools": router.tools, "input": head + [{"role": "user", "content": prompt}]}
    if previous:
        body["previous_response_id"] = previous
    transcript.write({"type": "user", "message": {"content": [{"type": "text", "text": prompt}]}})
    turns, final = 0, None
    while True:
        resp = await respond(http, body)
        usage_add(usage, resp)
        state_path.write_text(json.dumps({"previous_response_id": resp["id"]}), encoding="utf-8")
        turns += 1
        calls = [i for i in resp.get("output", []) if i.get("type") == "function_call"]
        for item in resp.get("output", []):
            if item.get("type") == "message":
                transcript.text("\n".join(c.get("text", "") for c in item.get("content", [])
                                          if c.get("type") == "output_text"))
        if not calls:
            final = "success"
            break
        if turns >= max_turns:
            final = "error_max_turns"
            break
        outputs = []
        for c in calls:
            args, result = await router.call(c["name"], c.get("arguments"))
            transcript.tool_use(c["name"], args, c["call_id"])
            transcript.write({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": c["call_id"], "content": result[:4000]}]}})
            outputs.append({"type": "function_call_output", "call_id": c["call_id"], "output": result})
        body = {"model": model, "tools": router.tools, "input": outputs, "previous_response_id": resp["id"]}
    result = {"type": "result", "subtype": final, "is_error": final != "success", "num_turns": turns,
              "usage": usage, "result": None}
    transcript.write(result)
    return result


async def main_async(a) -> int:
    key = os.environ.get(META_KEY_ENV)
    if not key and not a.list_tools:
        print(f"{META_KEY_ENV} is not set", file=sys.stderr)
        return 2
    prompt = "" if a.list_tools else sys.stdin.read()
    state_path = Path(a.state)
    previous = None
    if a.resume and state_path.exists():
        previous = json.loads(state_path.read_text(encoding="utf-8")).get("previous_response_id")
    servers = {"workbench": {"command": sys.executable,
                             "args": ["-m", "orchestrator.workbench", "--config", a.workbench], "cwd": str(ROOT)}}
    if a.mcp_config:
        servers |= json.loads(Path(a.mcp_config).read_text(encoding="utf-8")).get("mcpServers", {})
    transcript = None if a.list_tools else Transcript(Path(a.log))
    router = Router()
    async with AsyncExitStack() as stack:
        for name, spec in servers.items():
            params = StdioServerParameters(command=spec["command"], args=spec.get("args", []),
                                           env=dict(os.environ) | spec.get("env", {}), cwd=spec.get("cwd"))
            r, w = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(r, w))
            await session.initialize()
            listed = (await session.list_tools()).tools
            if name == "workbench":
                router.add(session, listed)
            else:
                router.add(session, listed, prefix=f"{name}_", allow=None if a.all_rea_tools else REA_TOOLS,
                           relax=True)
        if a.list_tools:
            print(json.dumps({"tools": [t["name"] for t in router.tools], "skipped": router.skipped}, indent=2))
            return 0
        transcript.write({"type": "system", "subtype": "init", "model": a.model,
                          "tools": [t["name"] for t in router.tools], "skipped": router.skipped})
        try:
            result = await run(MetaClient(key), router, transcript, a.model, prompt, previous, a.max_turns, state_path)
        except MuseError as e:
            transcript.write({"type": "result", "subtype": "error_api", "is_error": True, "result": str(e)})
            print(f"[agent_loop] {e}", file=sys.stderr)
            return 1
    print(json.dumps({k: result[k] for k in ("subtype", "num_turns", "usage")}), file=sys.stderr)
    return 0 if result["subtype"] == "success" else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Harness-free discovery agent on Muse Spark")
    ap.add_argument("--workbench", required=True, help="workbench config (orchestrator.workbench)")
    ap.add_argument("--mcp-config", help="extra MCP servers, e.g. REA ({\"mcpServers\": {...}})")
    ap.add_argument("--log", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--model", default="muse-spark-1.3", choices=list(MUSE_SPARK_PRICES))
    ap.add_argument("--max-turns", type=int, default=150)
    ap.add_argument("--list-tools", action="store_true", help="print the tools the agent would get, then exit")
    ap.add_argument("--all-rea-tools", action="store_true", help="offer every REA tool, not just static analysis")
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

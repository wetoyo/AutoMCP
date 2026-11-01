"""Hackathon demo: saved tool -> separate MCP client -> app restart -> same tool again, no LLM.

For each selected app this script
  1. launches a disposable copy of the authorized target (from targets/<app>/app),
  2. starts the MCP server as a separate process and lists the published tools,
  3. invokes the tool and prints the harness-style before/after effect,
  4. kills and relaunches the app, then invokes the same tool on the same MCP session.
     The server re-resolves the new process; nothing is regenerated.

Usage:
  python demos/run_demo.py [--app notepadpp|sumatrapdf|marktext|all] [--calls 2] [--allow-unapproved]

Electron targets (marktext) are launched with 127.0.0.1 debugging ports, and each (re)launch is
recorded in runtime/live/<target>.json, which is how the separate server process finds the instance.

The server publishes only approved tools; --allow-unapproved passes the testing-only
--include-unapproved flag to the server. Close any other copies of the target app first: with
several matching processes the server refuses to guess.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runtime.electron_bridge import load  # noqa: E402
from runtime.electron_launch import LIVE_DIR, state_path  # noqa: E402
from validation import harness, observers  # noqa: E402

DEMOS = {
    "notepadpp": {"adapter": "notepadpp/new_document_agent",
                  "observe": {"observer": "window_message", "why": "open documents",
                              "params": {"window_class": "Notepad++", "msg": "0x7ef"}}},
    "sumatrapdf": {"adapter": "sumatrapdf/new_window_agent",
                   "observe": {"observer": "top_level_windows", "why": "SumatraPDF windows",
                               "params": {"window_class": "SUMATRA_PDF_FRAME"}}},
    "marktext": {"adapter": "marktext/new_tab_agent",
                 "observe": {"observer": "page_eval", "why": "editor tabs (DOM)",
                             "params": {"expression": "document.querySelectorAll('ul.tabs-container > li').length"}}},
}


def banner(text: str):
    print(f"\n=== {text}")


def wait_for_window(pid: int, spec: dict, ctx: dict | None = None, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = observers.observe(pid, spec, ctx)
            if value >= 1:  # e.g. 0 frames means the app is still starting up
                time.sleep(0.5)
                return value
        except observers.ObserverError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"target pid {pid} showed no window within {timeout}s")


def launch(app: str, inst, spec: dict):
    inst.start()
    if harness.kind_of(app) == "electron":
        inst.wait_endpoint()
        LIVE_DIR.mkdir(exist_ok=True)
        state_path(app).write_text(json.dumps({"pid": inst.proc.pid, **inst.ports}), encoding="utf-8")
    wait_for_window(inst.proc.pid, spec, inst.observer_ctx)


async def call_and_show(session: ClientSession, tool: str, inst, spec: dict, calls: int) -> bool:
    ok = True
    for _ in range(calls):
        before = observers.observe(inst.proc.pid, spec, inst.observer_ctx)
        res = await session.call_tool(tool, {})
        payload = json.loads(res.content[0].text)
        time.sleep(0.3)
        after = observers.observe(inst.proc.pid, spec, inst.observer_ctx)
        print(f"  call {tool}: ok={payload.get('ok')}  {spec['why']}: {before} -> {after}"
              + ("" if payload.get("ok") else f"  error={payload.get('error')}: {payload.get('message')}"))
        ok &= bool(payload.get("ok")) and after == before + 1
    return ok


async def demo(app: str, calls: int, allow_unapproved: bool) -> bool:
    cfg = DEMOS[app]
    adapter = load(*cfg["adapter"].split("/"))
    tool = adapter.manifest["tool"]["name"]
    banner(f"{app}: {tool}  [{cfg['adapter']}, status={adapter.manifest.get('status')}, "
           f"approved={'yes' if adapter.manifest.get('approval') else 'no'}, origin={adapter.manifest.get('origin', {}).get('kind', 'manual')}]")
    print(f"  function: {adapter.manifest['resolution']['function']}")

    inst = harness.make_instance(app, adapter)
    try:
        launch(app, inst, cfg["observe"])
        print(f"  launched disposable instance pid {inst.proc.pid} "
              f"({getattr(inst, 'app_dir', None) or getattr(inst, 'user_data_dir', '')})")

        server_args = ["-m", "mcp_server.server", "--only", cfg["adapter"]]
        if allow_unapproved:
            server_args.append("--include-unapproved")
        env = {k: v for k, v in os.environ.items() if k not in ("WAM_TARGET_PID", "WAM_CDP_PORT", "WAM_INSPECT_PORT")}
        params = StdioServerParameters(command=sys.executable, args=server_args, cwd=str(ROOT), env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = [t.name for t in (await session.list_tools()).tools]
                print(f"  MCP server (separate process) publishes: {tools}")
                if tool not in tools:
                    print(f"  not published: approve it (python -m orchestrator.approve {cfg['adapter'].replace('/', ' ')}) "
                          f"or pass --allow-unapproved")
                    return False
                ok = await call_and_show(session, tool, inst, cfg["observe"], calls)

                banner(f"{app}: restart the application (same MCP session, no regeneration)")
                inst.stop()
                launch(app, inst, cfg["observe"])
                print(f"  relaunched pid {inst.proc.pid}")
                ok &= await call_and_show(session, tool, inst, cfg["observe"], calls)
        print(f"  {app}: {'PASS' if ok else 'FAIL'}")
        return ok
    finally:
        inst.cleanup()
        if harness.kind_of(app) == "electron":
            state_path(app).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="all", choices=[*DEMOS, "all"])
    ap.add_argument("--calls", type=int, default=2)
    ap.add_argument("--allow-unapproved", action="store_true")
    a = ap.parse_args()
    apps = list(DEMOS) if a.app == "all" else [a.app]
    results = {app: asyncio.run(demo(app, a.calls, a.allow_unapproved)) for app in apps}
    banner("summary: " + ", ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

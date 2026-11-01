"""MCP server: publish validated adapters from the adapter store as MCP tools.

No reverse engineering or LLM calls happen here; each tool call is routed to a saved Frida
adapter via runtime.bridge, or a saved Electron (DevTools-protocol) adapter via runtime.electron_bridge.
A tool is published only if its manifest status is "verified" and, when it mutates application state,
it carries a recorded user approval.

Target selection (native): WAM_TARGET_PID=<pid> pins the process; otherwise the server uses the single
running process whose image matches the adapter's sha256 (and refuses if there are several).
Target selection (Electron): the instance started by `python -m runtime.electron_launch <target>`
(runtime/live/<target>.json), or WAM_TARGET_PID + WAM_CDP_PORT [+ WAM_INSPECT_PORT]. Identity (exe and
app.asar sha256) is verified before every new session.

Run: python -m mcp_server.server [--include-unapproved]   (stdio transport)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from mcp_server import muse  # noqa: E402
from runtime.bridge import ADAPTERS_DIR, BridgeError, Session, find_pids, process_image_sha256  # noqa: E402
from runtime.electron_bridge import ElectronAdapter, ElectronSession, env_ports, load  # noqa: E402
from runtime.electron_launch import read_state  # noqa: E402


def publishable(manifest: dict, include_unapproved: bool) -> tuple[bool, str]:
    if manifest.get("status") != "verified":
        return False, f"status is {manifest.get('status')!r}, not 'verified'"
    if manifest["tool"].get("destructive"):
        return False, "destructive operations are not published"
    muse_problems = muse.tool_problems(manifest["tool"])
    if muse_problems:
        return False, "not callable from Meta Muse Spark: " + "; ".join(muse_problems)
    if manifest["tool"].get("mutates_state") and not manifest.get("approval") and not include_unapproved:
        return False, "mutating tool has no recorded user approval (run: python -m orchestrator.approve <app> <op>)"
    return True, "ok"


class Router:
    """Keeps one live session per adapter; reattaches if the target restarted."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.session: Session | ElectronSession | None = None
        self.lock = threading.Lock()

    def _select_electron(self) -> tuple[int, dict]:
        ports = env_ports()
        if ports:
            if not os.environ.get("WAM_TARGET_PID"):
                raise BridgeError("WAM_CDP_PORT needs WAM_TARGET_PID (the Electron main process)")
            return int(os.environ["WAM_TARGET_PID"]), ports
        target = self.adapter.manifest["app"]["target"]
        state = read_state(target)
        if not state:
            raise BridgeError(f"no debuggable {target} instance; start one with: python -m runtime.electron_launch {target}")
        return state["pid"], {k: state[k] for k in ("cdp_port", "inspect_port") if k in state}

    def _select_pid(self) -> int:
        pinned = os.environ.get("WAM_TARGET_PID")
        if pinned:
            return int(pinned)
        want = self.adapter.manifest["app"]["sha256"]
        pids = [p for p in find_pids(self.adapter.manifest["app"]["module"]) if process_image_sha256(p) == want]
        if not pids:
            raise BridgeError(f"no running {self.adapter.manifest['app']['module']} matching the adapter's build")
        if len(pids) > 1:
            raise BridgeError(f"several matching processes {pids}; set WAM_TARGET_PID to choose one")
        return pids[0]

    def call(self, args: dict) -> dict:
        with self.lock:
            electron = isinstance(self.adapter, ElectronAdapter)
            pid, ports = self._select_electron() if electron else (self._select_pid(), None)
            if self.session is None or not self.session.alive or self.session.pid != pid:
                if self.session is not None:
                    self.session.close()
                self.session = ElectronSession(pid, ports, self.adapter) if electron else Session(pid, self.adapter)
            result = self.session.invoke(args)
            result["pid"] = pid
            return result


def make_handler(router: Router):
    def handler() -> str:
        try:
            return json.dumps(router.call({}))
        except BridgeError as e:
            return json.dumps({"ok": False, "error": "bridge_error", "message": str(e)})
    return handler


def build_server(include_unapproved: bool, only: list[str] | None = None) -> MCPServer:
    server = MCPServer(name="windows-auto-mcp", instructions="In-process tools for authorized Windows applications.")
    candidates = []
    for manifest_path in sorted(ADAPTERS_DIR.glob("*/*/manifest.json")):
        app, op = manifest_path.parent.parent.name, manifest_path.parent.name
        if only and f"{app}/{op}" not in only:
            continue
        adapter = load(app, op)
        ok, why = publishable(adapter.manifest, include_unapproved)
        if not ok:
            print(f"[windows-auto-mcp] skipping {app}/{op}: {why}", file=sys.stderr)
            continue
        candidates.append((app, op, adapter))
    # Two adapters claiming one tool name is ambiguous: publish neither (choose with --only).
    names = [c[2].manifest["tool"]["name"] for c in candidates]
    for app, op, adapter in candidates:
        tool = adapter.manifest["tool"]
        if names.count(tool["name"]) > 1:
            print(f"[windows-auto-mcp] skipping {app}/{op}: tool name {tool['name']!r} is claimed by several "
                  f"adapters; select one with --only", file=sys.stderr)
            continue
        server.add_tool(
            make_handler(Router(adapter)),
            name=tool["name"],
            description=tool["description"],
            annotations=ToolAnnotations(readOnlyHint=not tool.get("mutates_state"),
                                        destructiveHint=bool(tool.get("destructive")),
                                        idempotentHint=False),
            structured_output=False,
        )
        print(f"[windows-auto-mcp] published {tool['name']} ({app}/{op})", file=sys.stderr)
    return server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-unapproved", action="store_true",
                    help="testing only: also publish verified mutating tools that lack recorded approval")
    ap.add_argument("--only", action="append", metavar="APP/OP", help="publish only these adapters (repeatable)")
    a = ap.parse_args()
    build_server(a.include_unapproved, a.only).run("stdio")


if __name__ == "__main__":
    main()

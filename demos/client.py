"""Independent MCP client: spawn the windows-auto-mcp server over stdio, list tools, call one.

Usage: python demos/client.py [tool_name] [--times N] [--server-arg ARG ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main(tool: str, times: int, server_args: list[str]) -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server", *server_args],
                                   cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print("tools:", tools)
            if tool not in tools:
                print(f"{tool} not published")
                return 1
            ok = True
            for _ in range(times):
                res = await s.call_tool(tool, {})
                payload = json.loads(res.content[0].text)
                print(json.dumps(payload))
                ok &= bool(payload.get("ok"))
            return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("tool", nargs="?", default="notepadpp_new_document")
    ap.add_argument("--times", type=int, default=1)
    ap.add_argument("--server-arg", action="append", default=[])
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.tool, a.times, a.server_arg)))

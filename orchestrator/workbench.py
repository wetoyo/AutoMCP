"""Workbench: the discovery agent's tools as an MCP server, for any agent or MCP client.

One server instance serves one run workspace, described by a JSON config the orchestrator writes:

  {"workspace": "<run>/workspace",            relative paths resolve here
   "read_roots": ["<workspace>", "<source tree>", "<helper lib>", ...],
   "write_dirs": ["adapter", "scratch"],       relative to the workspace
   "forbidden": ["<adapters dir>", ...],       never readable, even inside a read root
   "commands": {"wam": ["<python>", "<wam_cli.py>", "--target", "...", "--workspace", "..."]},
   "command_timeout": 900,
   "log": "<run>/workbench-log.jsonl"}         optional; every call, in the transcript-audit format

Path rules are enforced here, after resolving symlinks and `..`, so they hold whatever the agent is.

Run: python -m orchestrator.workbench --config <run>/workbench.json      (stdio transport)
"""
from __future__ import annotations

import argparse
import fnmatch
import functools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

MAX_READ_LINES = 2000
MAX_OUTPUT_CHARS = 60_000
MAX_LIST = 1000
MAX_MATCHES = 300


class WorkbenchError(Exception):
    pass


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


class Workbench:
    def __init__(self, config: dict):
        self.ws = Path(config["workspace"]).resolve()
        self.read_roots = [Path(p).resolve() for p in config.get("read_roots", [self.ws])]
        self.write_dirs = [(self.ws / d).resolve() for d in config.get("write_dirs", [])]
        self.forbidden = [Path(p).resolve() for p in config.get("forbidden", [])]
        self.commands = config.get("commands", {})
        self.timeout = config.get("command_timeout", 900)
        self.log = Path(config["log"]) if config.get("log") else None

    # -- path policy --------------------------------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        p = Path(path)
        return (p if p.is_absolute() else self.ws / p).resolve()

    def readable(self, path: str) -> Path:
        p = self.resolve(path)
        if any(_within(p, f) for f in self.forbidden):
            raise WorkbenchError(f"{path}: access denied")
        if not any(_within(p, r) for r in self.read_roots):
            raise WorkbenchError(f"{path}: outside the readable roots {[str(r) for r in self.read_roots]}")
        return p

    def writable(self, path: str) -> Path:
        p = self.resolve(path)
        if not any(_within(p, d) for d in self.write_dirs):
            raise WorkbenchError(f"{path}: writes are allowed only under {[str(d) for d in self.write_dirs]}")
        return p

    def _walk(self, base: Path):
        for dirpath, dirnames, filenames in os.walk(base):
            d = Path(dirpath)
            dirnames[:] = [n for n in dirnames if not any(_within((d / n).resolve(), f) for f in self.forbidden)]
            for n in filenames:
                yield d / n

    # -- tools --------------------------------------------------------------------------------------------------
    def read_file(self, path: str, offset: int = 1, limit: int = 400) -> str:
        p = self.readable(path)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        limit = max(1, min(limit, MAX_READ_LINES))
        start = max(1, offset)
        chunk = lines[start - 1:start - 1 + limit]
        body = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start))
        more = f"\n... [{len(lines)} lines total; continue with offset={start + limit}]" if start - 1 + limit < len(lines) else ""
        return _clip(body + more)

    def list_files(self, path: str = ".", pattern: str = "*") -> str:
        base = self.readable(path)
        out = []
        for f in self._walk(base):
            rel = f.relative_to(base).as_posix()
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
                out.append(rel)
                if len(out) >= MAX_LIST:
                    out.append(f"... [stopped at {MAX_LIST}; narrow the pattern]")
                    break
        return "\n".join(out) or "(no files)"

    def search_files(self, pattern: str, path: str = ".", glob: str = "*", max_results: int = 100) -> str:
        base = self.readable(path)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise WorkbenchError(f"bad regex: {e}")
        limit = max(1, min(max_results, MAX_MATCHES))
        files = [base] if base.is_file() else self._walk(base)
        hits = []
        for f in files:
            if not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append(f"{f.relative_to(base.parent if base.is_file() else base).as_posix()}:{n}: "
                                        f"{line.rstrip()[:400]}")
                            if len(hits) >= limit:
                                return _clip("\n".join(hits) + f"\n... [stopped at {limit} matches]")
            except OSError:
                continue
        return _clip("\n".join(hits)) or "(no matches)"

    def write_file(self, path: str, content: str) -> str:
        p = self.writable(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
        return f"wrote {p.relative_to(self.ws).as_posix()} ({len(content)} chars)"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self.writable(path)
        text = p.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise WorkbenchError(f"old_text occurs {count} times in {path}; it must occur exactly once")
        p.write_text(text.replace(old_text, new_text), encoding="utf-8", newline="\n")
        return f"edited {p.relative_to(self.ws).as_posix()}"

    def run_command(self, name: str, args: list[str]) -> str:
        argv = self.commands[name] + [str(a) for a in args]
        try:
            r = subprocess.run(argv, cwd=self.ws, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return f"[{name}] timed out after {self.timeout} s"
        except OSError as e:
            return f"[{name}] could not start {argv[0]}: {e}"
        return _clip(f"[exit {r.returncode}]\n{r.stdout}{r.stderr}")

    # -- logging ------------------------------------------------------------------------------------------------
    def record(self, name: str, args: dict) -> None:
        if not self.log:
            return
        event = {"type": "assistant", "source": "workbench",
                 "message": {"content": [{"type": "tool_use", "name": name, "input": args}]}}
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")


def build_server(wb: Workbench) -> MCPServer:
    server = MCPServer(name="auto-mcp-workbench", instructions=(
        "Tools for one Auto-MCP discovery run. Paths are relative to the run workspace. Read ./TASK.md first."))

    def tool(fn, name: str, description: str):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            wb.record(name, kwargs)
            try:
                return fn(*args, **kwargs)
            except (WorkbenchError, OSError) as e:
                return f"error: {e}"
        server.add_tool(wrapped, name=name, description=description, structured_output=False)

    tool(wb.read_file, "read_file", "Read a text file as numbered lines (offset is 1-based; limit <= 2000).")
    tool(wb.list_files, "list_files", "List files under a directory, recursively, matching a glob pattern.")
    tool(wb.search_files, "search_files", "Search file contents with a regex; returns path:line: text.")
    tool(wb.write_file, "write_file", "Create or overwrite a file under the writable directories (e.g. adapter/).")
    tool(wb.edit_file, "edit_file", "Replace one exact, unique occurrence of old_text with new_text in a writable file.")
    for name in wb.commands:
        tool(_command(wb, name), name,
             f"Run `{name}` with these arguments, e.g. args=[\"identity\"]. TASK.md lists the subcommands.")
    return server


def _command(wb: Workbench, name: str):
    def run(args: list[str] | None = None) -> str:
        return wb.run_command(name, args or [])
    return run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True)
    a = ap.parse_args(argv)
    config = json.loads(Path(a.config).read_text(encoding="utf-8"))
    build_server(Workbench(config)).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())

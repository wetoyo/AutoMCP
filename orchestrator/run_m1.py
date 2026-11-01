"""M1 orchestrator: have a coding agent discover + implement an in-process adapter from a
natural-language request, then validate it with a bounded repair loop.

The agent (the headless `claude` CLI, running on Meta's Muse Spark by default; see orchestrator/provider.py) gets: the request, REA (MCP), the upstream source tree (native) or the
extracted app.asar (Electron), the generic WAM helper library (Frida or DevTools flavour) + observer docs,
and the `./wam` tool. It does not get existing adapters, the README, or session memory. Every attempt is logged under runs/<run_id>/.

Usage:
  python -m orchestrator.run_m1 --target notepadpp --operation new_document_agent \
      --request "Expose Notepad++'s internal function for creating a new document" [--attempts 3]
  python -m orchestrator.run_m1 --target marktext --operation <dir_name> --request "..."   (Electron target)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator import provider  # noqa: E402
from runtime.bridge import ADAPTERS_DIR, WAM_LIB  # noqa: E402
from runtime.electron_bridge import WAM_ELECTRON, load_any  # noqa: E402
from validation import harness  # noqa: E402

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CLAUDE = shutil.which("claude.exe") or str(Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
# Read-only reference given to the agent: upstream source (native) or the extracted app.asar (Electron).
SOURCES = {"notepadpp": ROOT / "targets" / "notepadpp" / "source",
           "sumatrapdf": ROOT / "targets" / "sumatrapdf" / "source",
           "marktext": ROOT / "targets" / "marktext" / "extracted"}
APP_NAMES = {"notepadpp": "Notepad++ 8.9.8.1 portable x64", "sumatrapdf": "SumatraPDF 3.6.1 portable x64",
             "marktext": "MarkText 0.22.0 portable x64 (Electron 42.1.0)"}
TEMPLATES = {"native": "task_template.md", "electron": "task_template_electron.md"}
# Paths whose contents would leak prior (manual) results to the agent.
FORBIDDEN = [str(ADAPTERS_DIR), str(ROOT / "README.md"), str(ROOT / "targets" / "notepadpp" / "src"),
             str(ROOT / "runs"), str(Path.home() / ".claude"), str(ROOT / ".mcp.json")]
DENY_READ = [p for p in FORBIDDEN if not p.endswith("runs")]  # the agent's own workspace is under runs/


def posix(p) -> str:
    return str(p).replace("\\", "/")


def norm(s: str) -> str:
    return s.replace("\\", "/").lower()


def write_workspace(run_dir: Path, a) -> Path:
    ws = run_dir / "workspace"
    (ws / "adapter").mkdir(parents=True)
    (ws / "scratch").mkdir()
    spec = harness.TARGETS[a.target]
    kind = harness.kind_of(a.target)
    task = (ROOT / "orchestrator" / TEMPLATES[kind]).read_text(encoding="utf-8").format(
        request=a.request, app_name=APP_NAMES[a.target], target=a.target,
        exe_path=spec["app_dir"] / spec["exe"], source_dir=SOURCES[a.target],
        wam_lib=WAM_ELECTRON if kind == "electron" else WAM_LIB,
        observers_py=ROOT / "validation" / "observers.py", max_turns=a.max_turns)
    (ws / "TASK.md").write_text(task, encoding="utf-8")
    (ws / "wam").write_text(
        "#!/bin/sh\n"
        f'exec "{posix(VENV_PY)}" "{posix(ROOT / "orchestrator" / "wam_cli.py")}" '
        f'--target {a.target} --workspace "{posix(ws)}" "$@"\n', encoding="utf-8", newline="\n")
    rea = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["rea"]
    (run_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"rea": rea}}, indent=2), encoding="utf-8")
    settings = {"permissions": {
        "allow": ["Read", "Glob", "Grep", "Edit(./adapter/**)", "Edit(./scratch/**)",
                  "Bash(./wam:*)", "Bash(./wam *)", "mcp__rea"],
        "deny": [f"Read(/{posix(p)}/**)" for p in DENY_READ] + ["WebFetch", "WebSearch", "Agent"],
    }}
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return ws


def run_agent(run_dir: Path, ws: Path, prompt: str, attempt: int, session_id: str, resume: bool, a) -> dict:
    log = run_dir / f"attempt-{attempt}.jsonl"
    cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(run_dir / "mcp.json"), "--strict-mcp-config",
           "--settings", str(run_dir / "settings.json"), "--setting-sources", "project",
           "--permission-mode", "dontAsk",
           "--allowedTools", "Read", "Glob", "Grep", "Edit(./adapter/**)", "Edit(./scratch/**)",
           "Bash(./wam:*)", "Bash(./wam *)", "mcp__rea",
           "--add-dir", str(SOURCES[a.target]), "--model", a.model]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    if a.max_budget_usd:
        cmd += ["--max-budget-usd", str(a.max_budget_usd)]
    started = time.monotonic()
    with open(log, "w", encoding="utf-8") as f:
        try:
            proc = subprocess.run(cmd, cwd=ws, input=prompt.encode("utf-8"), stdout=f, stderr=subprocess.STDOUT,
                                  timeout=a.attempt_timeout, env=a.env)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    return {"log": log.name, "returncode": rc, "seconds": round(time.monotonic() - started)}


def audit(log: Path, own_run: Path, session_id: str | None = None) -> dict:
    """Summarise tool use, permission denials, and flag any access to forbidden paths
    (the run's own directory excepted, and the agent session's own spilled tool outputs, which
    Claude Code writes to ~/.claude/projects/<workspace>/<session_id>/tool-results/)."""
    tools, violations, denials, result = {}, [], [], None
    own = norm(str(own_run))
    forbidden = [norm(p) for p in FORBIDDEN]
    own_spill = (re.compile(re.escape(norm(str(Path.home() / ".claude" / "projects"))) + r"/[^/\"]+/"
                            + re.escape(session_id.lower()) + r"/tool-results/") if session_id else None)
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            result = {k: ev.get(k) for k in ("subtype", "is_error", "num_turns", "total_cost_usd", "usage", "result")}
        if ev.get("subtype") == "permission_denied":
            denials.append({"tool": ev.get("tool_name"), "tool_use_id": ev.get("tool_use_id")})
        message = ev.get("message")
        if not isinstance(message, dict):
            continue  # e.g. system events carry a plain-text message
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools[block["name"]] = tools.get(block["name"], 0) + 1
                blob = norm(json.dumps(block.get("input")).replace("\\\\", "\\")).replace(own, "<own-run>")
                if own_spill:
                    blob = own_spill.sub("<own-tool-results>/", blob)
                for p in forbidden + ["../../"]:  # relative escapes out of the workspace count too
                    if p in blob:
                        violations.append({"tool": block["name"], "input": block.get("input")})
    return {"tool_counts": tools, "forbidden_access": violations, "permission_denials": denials, "result": result}


def inject_fault(manifest_path: Path) -> dict:
    """Repair-loop test: flip the last nibble of the first long hex byte-string in adapter_config
    (native entry bytes) or, failing that, of the first Electron handle's source_sha256."""
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    slots = [(m["adapter_config"], k, k) for k in m.get("adapter_config", {})]
    slots += [(h, "source_sha256", f"handles.{n}.source_sha256") for n, h in m.get("handles", {}).items()
              if isinstance(h, dict) and "source_sha256" in h]
    for container, key, label in slots:
        value = container[key]
        if isinstance(value, str) and len(value) >= 8 and not value.startswith("0x") and \
                all(c in "0123456789abcdefABCDEF" for c in value):
            corrupted = value[:-1] + format(int(value[-1], 16) ^ 0xF, "x")
            container[key] = corrupted
            manifest_path.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
            return {"key": label, "original": value, "corrupted": corrupted}
    return {"key": None, "note": "no entry-byte string or source hash found; nothing injected"}


def diagnostics(report: dict) -> str:
    slim = json.loads(json.dumps(report))
    for ph in slim.get("phases", []):
        ph["calls"] = ph.get("calls", [])[-1:]  # last call carries the failure
    return json.dumps(slim, indent=2)[:12000]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, choices=list(harness.TARGETS))
    ap.add_argument("--operation", required=True, help="adapter directory name to publish under on success")
    ap.add_argument("--request", required=True)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--attempt-timeout", type=int, default=3600)
    ap.add_argument("--max-turns", type=int, default=150)
    ap.add_argument("--max-budget-usd", type=float, default=0)
    provider.add_args(ap)
    ap.add_argument("--inject-fault", action="store_true",
                    help="test the repair loop: corrupt one entry-byte value after attempt 1 (recorded in run.json)")
    a = ap.parse_args(argv)
    try:
        a.model = provider.resolve_model(a)
        a.env = provider.agent_env(a.provider, a.model)
    except provider.ProviderError as e:
        print(f"refusing: {e}")
        return 2

    dest = ADAPTERS_DIR / a.target / a.operation
    if dest.exists():
        print(f"refusing: {dest} already exists")
        return 2
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{a.target}-{a.operation}"
    run_dir = ROOT / "runs" / run_id
    ws = write_workspace(run_dir, a)
    session_id = str(uuid.uuid4())
    log = {"run_id": run_id, "request": a.request, "target": a.target, "session_id": session_id,
           "provider": a.provider, "model": a.model,
           "started": datetime.now(timezone.utc).isoformat(), "attempts": []}

    prompt = f"Read ./TASK.md and carry out the task it describes. The user's request: {a.request}"
    passed = False
    for attempt in range(1, a.attempts + 1):
        info = run_agent(run_dir, ws, prompt, attempt, session_id, attempt > 1, a)
        info["audit"] = audit(run_dir / info["log"], run_dir, session_id)
        info["est_cost_usd"] = provider.estimate_cost((info["audit"]["result"] or {}).get("usage"), a.provider, a.model)
        adapter_dir = ws / "adapter"
        if (adapter_dir / "UNRESOLVED.md").exists():
            info["outcome"] = "agent_declared_unresolved"
            log["attempts"].append(info)
            break
        if a.inject_fault and attempt == 1 and (adapter_dir / "manifest.json").exists():
            info["fault_injected"] = inject_fault(adapter_dir / "manifest.json")
        if not (adapter_dir / "manifest.json").exists():
            info["outcome"] = "no_manifest"
            report = {"passed": False, "static_problems": ["adapter/manifest.json was not written"]}
        else:
            try:
                report = harness.validate(load_any(adapter_dir), calls=3)
            except Exception as e:
                report = {"passed": False, "static_problems": [f"harness error: {type(e).__name__}: {e}"]}
        (run_dir / f"validation-{attempt}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        snap = run_dir / f"adapter-attempt-{attempt}"
        shutil.copytree(adapter_dir, snap)
        info["validation"] = f"validation-{attempt}.json"
        info["passed"] = report["passed"]
        log["attempts"].append(info)
        (run_dir / "run.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
        if report["passed"]:
            passed = True
            break
        prompt = ("The orchestrator's independent validation of ./adapter FAILED. Diagnostics follow. "
                  "Fix the adapter (or write ./adapter/UNRESOLVED.md if it cannot be done safely), "
                  "then run ./wam validate.\n\n" + diagnostics(report))

    contaminated = any(att["audit"]["forbidden_access"] for att in log["attempts"])
    log["contaminated"] = contaminated
    log["passed"] = passed and not contaminated
    if log["passed"]:
        shutil.copytree(ws / "adapter", dest)
        m = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        m.update(status="verified", origin={"kind": "agent", "run_id": run_id},
                 validation={"report": "validation.json", "at": datetime.now(timezone.utc).isoformat()})
        m.pop("approval", None)
        (dest / "manifest.json").write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
        shutil.copy(run_dir / f"validation-{len(log['attempts'])}.json", dest / "validation.json")
        log["published_to"] = str(dest)
    log["finished"] = datetime.now(timezone.utc).isoformat()
    (run_dir / "run.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    print(json.dumps({k: log[k] for k in ("run_id", "passed", "contaminated")} |
                     {"attempts": [{k: v for k, v in at.items() if k != "audit"} | {"tools": at["audit"]["tool_counts"],
                                    "result": (at["audit"]["result"] or {}).get("subtype")}
                                   for at in log["attempts"]]}, indent=2))
    return 0 if log["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

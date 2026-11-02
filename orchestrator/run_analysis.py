"""Static-analysis orchestrator for JavaScript applications: locate the function behind an operation.

The agent (the headless `claude` CLI, running on Meta's Muse Spark by default; see orchestrator/provider.py) gets the request, REA (MCP), the extracted JS tree (read-only) and a
`./check` command; it writes analysis/discovery.json + DISCOVERY.md. Nothing in the target is executed.
The orchestrator re-checks the report independently (validation/discovery_check.py) and resumes the
agent with the problems for a bounded number of attempts. Every attempt is logged under runs/<run_id>/.

Onboard a target first:  python -m analysis.js_extract --out targets/<name>/extracted ARCHIVE...
Usage:
  python -m orchestrator.run_analysis --target-dir targets/firefox --app-name "Firefox 158.0.1 x64" \
      --request "Find the internal function that opens a new tab for a given URL" [--attempts 3]
"""
from __future__ import annotations

import argparse
import json
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
from orchestrator.run_m1 import (CLAUDE, DENY_READ, VENV_PY, audit, posix, rea_server, run_native_agent,  # noqa: E402
                                 write_workbench)  # noqa: E402
from validation import discovery_check  # noqa: E402

ALLOWED = ["Read", "Glob", "Grep", "Edit(./analysis/**)", "Bash(./check:*)", "Bash(./check)", "mcp__rea"]


def write_workspace(run_dir: Path, extracted: Path, a) -> Path:
    ws = run_dir / "workspace"
    (ws / "analysis").mkdir(parents=True)
    task = (ROOT / "orchestrator" / "task_template_analysis.md").read_text(encoding="utf-8").format(
        request=a.request, app_name=a.app_name, root=extracted, manifest=extracted / "extract_manifest.json")
    (ws / "TASK.md").write_text(task, encoding="utf-8")
    (ws / "check").write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH="{posix(ROOT)}" exec "{posix(VENV_PY)}" -m validation.discovery_check --root "{posix(extracted)}" '
        f'"{posix(ws / "analysis" / "discovery.json")}"\n', encoding="utf-8", newline="\n")
    rea = rea_server()
    (run_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"rea": rea}}, indent=2), encoding="utf-8")
    settings = {"permissions": {"allow": ALLOWED,
                                "deny": [f"Read(/{posix(p)}/**)" for p in DENY_READ] + ["WebFetch", "WebSearch", "Agent"]}}
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    check = [str(VENV_PY), str(ROOT / "validation" / "discovery_check.py"), "--root", str(extracted),
             str(ws / "analysis" / "discovery.json")]
    write_workbench(run_dir, ws, [extracted], ["analysis"], {"check": check})
    return ws


def run_agent(run_dir: Path, ws: Path, extracted: Path, prompt: str, attempt: int, session_id: str, a) -> dict:
    if a.harness == "native":
        return run_native_agent(run_dir, prompt, attempt, attempt > 1, a)
    log = run_dir / f"attempt-{attempt}.jsonl"
    cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(run_dir / "mcp.json"), "--strict-mcp-config",
           "--settings", str(run_dir / "settings.json"), "--setting-sources", "project",
           "--permission-mode", "dontAsk", "--allowedTools", *ALLOWED,
           "--add-dir", str(extracted), "--model", a.model]
    cmd += ["--resume", session_id] if attempt > 1 else ["--session-id", session_id]
    if a.max_budget_usd:
        cmd += ["--max-budget-usd", str(a.max_budget_usd)]
    started = time.monotonic()
    with open(log, "w", encoding="utf-8") as f:
        try:
            rc = subprocess.run(cmd, cwd=ws, input=prompt.encode("utf-8"), stdout=f, stderr=subprocess.STDOUT,
                                timeout=a.attempt_timeout, env=a.env).returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    return {"log": log.name, "returncode": rc, "seconds": round(time.monotonic() - started)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-dir", required=True, help="targets/<name> containing extracted/ from analysis.js_extract")
    ap.add_argument("--app-name", required=True)
    ap.add_argument("--request", required=True)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--attempt-timeout", type=int, default=3600)
    ap.add_argument("--max-budget-usd", type=float, default=0)
    provider.add_args(ap)
    a = ap.parse_args(argv)
    try:
        a.model = provider.resolve_model(a)
        a.env = provider.agent_env(a.provider, a.model, a.harness)
        rea_server()
    except provider.ProviderError as e:
        print(f"refusing: {e}")
        return 2

    target_dir = (ROOT / a.target_dir).resolve() if not Path(a.target_dir).is_absolute() else Path(a.target_dir)
    extracted = target_dir / "extracted"
    if not (extracted / "extract_manifest.json").exists():
        print(f"missing {extracted / 'extract_manifest.json'}; run analysis.js_extract first")
        return 2
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{target_dir.name}-analysis"
    run_dir = ROOT / "runs" / run_id
    ws = write_workspace(run_dir, extracted, a)
    session_id = str(uuid.uuid4())
    log = {"run_id": run_id, "mode": "static-analysis", "request": a.request, "target_dir": str(target_dir),
           "session_id": session_id, "provider": a.provider, "model": a.model,
           "harness": a.harness, "started": datetime.now(timezone.utc).isoformat(), "attempts": []}

    prompt = f"Read ./TASK.md and carry out the task it describes. The user's request: {a.request}"
    passed = False
    for attempt in range(1, a.attempts + 1):
        info = run_agent(run_dir, ws, extracted, prompt, attempt, session_id, a)
        info["audit"] = audit(run_dir / info["log"], run_dir, session_id)
        info["est_cost_usd"] = provider.estimate_cost((info["audit"]["result"] or {}).get("usage"), a.provider, a.model)
        out = ws / "analysis"
        if (out / "UNRESOLVED.md").exists():
            info["outcome"] = "agent_declared_unresolved"
            log["attempts"].append(info)
            break
        try:
            report = json.loads((out / "discovery.json").read_text(encoding="utf-8"))
            problems = discovery_check.check(extracted, report)
        except (OSError, json.JSONDecodeError) as e:
            problems = [f"analysis/discovery.json unreadable: {e}"]
        if not (out / "DISCOVERY.md").exists():
            problems.append("analysis/DISCOVERY.md was not written")
        info.update(passed=not problems, problems=problems)
        shutil.copytree(out, run_dir / f"analysis-attempt-{attempt}")
        log["attempts"].append(info)
        (run_dir / "run.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
        if not problems:
            passed = True
            break
        prompt = ("The orchestrator's independent check of ./analysis/discovery.json FAILED:\n- "
                  + "\n- ".join(problems) + "\nFix the report (re-verify each citation), then run ./check.")

    log["contaminated"] = any(att["audit"]["forbidden_access"] for att in log["attempts"])
    log["passed"] = passed and not log["contaminated"]
    log["finished"] = datetime.now(timezone.utc).isoformat()
    (run_dir / "run.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    print(json.dumps({k: log[k] for k in ("run_id", "passed", "contaminated")} | {"attempts": [
        {k: v for k, v in at.items() if k != "audit"} | {"tools": at["audit"]["tool_counts"]}
        for at in log["attempts"]]}, indent=2))
    return 0 if log["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

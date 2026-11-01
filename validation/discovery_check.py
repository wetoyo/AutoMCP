"""Mechanical check of a static-analysis discovery report (discovery.json) against extracted sources.

Checks (all must pass):
  - required fields are present and well-typed
  - every cited file exists under the extracted root (no traversal) and every cited line is in range
  - the target function is *defined* at or just after its cited line
  - every call-chain and entry-point citation mentions its symbol within +/-3 lines
This verifies the evidence is real; it does not verify that the reasoning connecting it is correct.

Usage: python -m validation.discovery_check --root targets/<name>/extracted path/to/discovery.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath

REQUIRED = {
    "operation": str, "function": dict, "call_chain": list, "entry_points": list,
    "execution_context": str, "inputs": list, "observable_effects": list, "confidence": str, "unknowns": list,
}
FUNCTION_FIELDS = {"name": str, "file": str, "line": int, "signature": str}
CITATION_FIELDS = {"symbol": str, "file": str, "line": int}
CONFIDENCE = {"high", "medium", "low"}
WINDOW = 3


def _lines(root: Path, rel: str, cache: dict) -> list[str]:
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if not parts or rel.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
        raise ValueError(f"path must be relative to the extracted root without '..': {rel!r}")
    if rel not in cache:
        path = root.joinpath(*parts)
        if not path.is_file():
            raise FileNotFoundError(f"cited file does not exist: {rel}")
        cache[rel] = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return cache[rel]


def _bare(symbol: str) -> str:
    """'gBrowser.addTab' / 'Tabbrowser#addTab' / 'addTab()' -> 'addTab'."""
    return re.split(r"[.#:]", symbol.strip().rstrip("()").strip())[-1]


def _definition_re(name: str) -> re.Pattern:
    n = re.escape(name)
    return re.compile(
        rf"(\bfunction\s*\*?\s*{n}\s*\()"            # function name(
        rf"|(^\s*(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?\*?\s*{n}\s*\([^;]*$)"  # class/object method name(...) {{
        rf"|(\b{n}\s*[:=]\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>))"  # name = function / arrow
    )


def check(root: Path, report: dict) -> list[str]:
    problems: list[str] = []
    cache: dict = {}
    for key, typ in REQUIRED.items():
        if not isinstance(report.get(key), typ):
            problems.append(f"{key}: missing or not a {typ.__name__}")
    if problems:
        return problems
    if report["confidence"] not in CONFIDENCE:
        problems.append(f"confidence must be one of {sorted(CONFIDENCE)}")
    if not report["call_chain"]:
        problems.append("call_chain must cite at least one caller/route to the function")
    if not report["entry_points"]:
        problems.append("entry_points must cite at least one UI/command/IPC entry point")

    fn = report["function"]
    for key, typ in FUNCTION_FIELDS.items():
        if not isinstance(fn.get(key), typ):
            problems.append(f"function.{key}: missing or not a {typ.__name__}")
    if not problems:
        try:
            lines = _lines(root, fn["file"], cache)
            ln = fn["line"]
            if not 1 <= ln <= len(lines):
                problems.append(f"function.line {ln} out of range for {fn['file']} ({len(lines)} lines)")
            else:
                name = _bare(fn["name"])
                pat = _definition_re(name)
                if not any(pat.search(lines[i]) for i in range(ln - 1, min(len(lines), ln + WINDOW))):
                    problems.append(f"function {name!r} is not defined at {fn['file']}:{ln} (checked {WINDOW + 1} lines)")
        except (OSError, ValueError) as e:
            problems.append(f"function: {e}")

    for section in ("call_chain", "entry_points"):
        for i, c in enumerate(report[section]):
            where = f"{section}[{i}]"
            if not isinstance(c, dict) or any(not isinstance(c.get(k), t) for k, t in CITATION_FIELDS.items()):
                problems.append(f"{where}: needs symbol (str), file (str), line (int)")
                continue
            try:
                lines = _lines(root, c["file"], cache)
            except (OSError, ValueError) as e:
                problems.append(f"{where}: {e}")
                continue
            ln = c["line"]
            if not 1 <= ln <= len(lines):
                problems.append(f"{where}: line {ln} out of range for {c['file']}")
                continue
            name = _bare(c["symbol"])
            lo, hi = max(0, ln - 1 - WINDOW), min(len(lines), ln + WINDOW)
            if not any(re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", lines[j]) for j in range(lo, hi)):
                problems.append(f"{where}: symbol {name!r} not found within +/-{WINDOW} lines of {c['file']}:{ln}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("report")
    a = ap.parse_args(argv)
    try:
        report = json.loads(Path(a.report).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"passed": False, "problems": [f"cannot read report: {e}"]}, indent=2))
        return 1
    problems = check(Path(a.root), report)
    print(json.dumps({"passed": not problems, "problems": problems}, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())

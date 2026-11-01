"""Set up REA + Ghidra for the agent loops and write .mcp.json for this checkout.

REA (the `rea-agents` npm package) is the MCP server the discovery agent uses for reverse engineering. For native
binaries it drives Ghidra 12.1.4, which needs a JDK 21+. Only the agent loops (`orchestrator.run_m1`,
`orchestrator.run_analysis`) need it; the harness, demos and MCP server don't.

  python -m analysis.setup_rea                      install REA, find Ghidra + Java, write .mcp.json, smoke-test
  python -m analysis.setup_rea --download-ghidra    also download Ghidra 12.1.4 (~570 MB, sha256-checked)
  python -m analysis.setup_rea --ghidra-dir D:/ghidra_12.1.4_PUBLIC --java-home "C:/Program Files/Java/jdk-25"

Everything it installs stays in git-ignored folders: analysis/rea-runtime/node_modules/ and analysis/tools/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "analysis" / "rea-runtime"
REA_SCRIPT = RUNTIME / "node_modules" / "rea-agents" / "scripts" / "rea.mjs"
TOOLS = ROOT / "analysis" / "tools"
MCP_JSON = ROOT / ".mcp.json"

GHIDRA_VERSION = "12.1.4"
GHIDRA_DIR_NAME = f"ghidra_{GHIDRA_VERSION}_PUBLIC"
GHIDRA_ZIP = "ghidra_12.1.4_PUBLIC_20260921.zip"
GHIDRA_URL = f"https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_{GHIDRA_VERSION}_build/{GHIDRA_ZIP}"
GHIDRA_SHA256 = "ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db"  # from the GitHub release
MIN_JAVA = 21


class SetupError(RuntimeError):
    pass


def step(msg: str) -> None:
    print(f"[setup_rea] {msg}", flush=True)


def check_node() -> str:
    node = shutil.which("node")
    if not node or not shutil.which("npm"):
        raise SetupError("Node.js and npm are required: install Node 20+ from https://nodejs.org")
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    if int(version.lstrip("v").split(".")[0]) < 20:
        raise SetupError(f"Node {version} is too old; REA needs Node 20+")
    step(f"node {version}")
    return node


def install_rea() -> None:
    if REA_SCRIPT.exists():
        step(f"REA already installed ({REA_SCRIPT.parent.parent})")
        return
    step("installing REA from analysis/rea-runtime/package-lock.json (npm ci)")
    npm = shutil.which("npm")
    subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=RUNTIME, check=True)
    if not REA_SCRIPT.exists():
        raise SetupError(f"npm ci finished but {REA_SCRIPT} is missing")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_ghidra() -> Path:
    dl = TOOLS / "dl"
    dl.mkdir(parents=True, exist_ok=True)
    archive = dl / GHIDRA_ZIP
    if not archive.exists() or sha256(archive) != GHIDRA_SHA256:
        step(f"downloading {GHIDRA_URL}")
        tmp = archive.with_suffix(".part")
        urllib.request.urlretrieve(GHIDRA_URL, tmp)
        tmp.replace(archive)
    digest = sha256(archive)
    if digest != GHIDRA_SHA256:
        raise SetupError(f"{archive.name} sha256 {digest} does not match the release ({GHIDRA_SHA256})")
    step(f"sha256 ok, extracting to {TOOLS}")
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():  # refuse path traversal
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise SetupError(f"unsafe path in archive: {name}")
        z.extractall(TOOLS)
    return TOOLS / GHIDRA_DIR_NAME


def find_ghidra(arg: str | None, download: bool) -> Path:
    candidates = [Path(arg)] if arg else [TOOLS / GHIDRA_DIR_NAME]
    if not arg and os.environ.get("GHIDRA_INSTALL_DIR"):
        candidates.insert(0, Path(os.environ["GHIDRA_INSTALL_DIR"]))
    for c in candidates:
        props = c / "Ghidra" / "application.properties"
        if props.exists():
            m = re.search(r"application\.version=(\S+)", props.read_text(encoding="utf-8", errors="replace"))
            if m and m.group(1) == GHIDRA_VERSION:
                step(f"Ghidra {GHIDRA_VERSION} at {c}")
                return c.resolve()
            step(f"skipping {c}: Ghidra {m.group(1) if m else '?'}, REA needs {GHIDRA_VERSION}")
    if download:
        return find_ghidra(str(download_ghidra()), download=False)
    raise SetupError(f"Ghidra {GHIDRA_VERSION} not found. Rerun with --download-ghidra, or unpack {GHIDRA_URL} "
                     f"into {TOOLS} (or pass --ghidra-dir)")


def java_major(home: Path) -> int | None:
    release = home / "release"
    if release.exists():
        m = re.search(r'JAVA_VERSION="(\d+)', release.read_text(encoding="utf-8", errors="replace"))
        if m:
            return int(m.group(1))
    return None


def find_java(arg: str | None) -> Path:
    candidates = []
    if arg:
        candidates.append(Path(arg))
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]))
    java = shutil.which("java")
    if java:
        out = subprocess.run([java, "-XshowSettings:properties", "-version"], capture_output=True, text=True)
        m = re.search(r"java\.home = (.+)", out.stderr + out.stdout)
        if m:
            candidates.append(Path(m.group(1).strip()))
    for c in candidates:
        major = java_major(c)
        if major and major >= MIN_JAVA and (c / "bin").exists():
            step(f"Java {major} at {c}")
            return c.resolve()
        if major:
            step(f"skipping {c}: Java {major}, Ghidra {GHIDRA_VERSION} needs {MIN_JAVA}+")
    raise SetupError(f"no JDK {MIN_JAVA}+ found; install one (e.g. https://adoptium.net) and set JAVA_HOME, "
                     "or pass --java-home")


def write_mcp_json(node: str, ghidra: Path, java: Path) -> None:
    config = json.loads(MCP_JSON.read_text(encoding="utf-8")) if MCP_JSON.exists() else {}
    config.setdefault("mcpServers", {})["rea"] = {
        "command": "node",
        "args": [str(REA_SCRIPT), "mcp"],
        "env": {"GHIDRA_INSTALL_DIR": str(ghidra), "JAVA_HOME": str(java), "REA_ANALYSIS_PROVIDER": "ghidra",
                "REA_GHIDRA_STARTUP_TIMEOUT_MS": "600000"},
    }
    MCP_JSON.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    step(f"wrote {MCP_JSON}")


def smoke_test(node: str) -> None:
    rea = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"]["rea"]
    env = dict(os.environ) | rea["env"]
    out = subprocess.run([node, str(REA_SCRIPT), "capabilities", "--json"], capture_output=True, text=True,
                         env=env, timeout=300)
    if out.returncode != 0:
        raise SetupError(f"`rea capabilities` failed ({out.returncode}): {(out.stderr or out.stdout)[-1500:]}")
    step("REA responds (`rea capabilities` ok)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Set up REA + Ghidra and write .mcp.json")
    ap.add_argument("--download-ghidra", action="store_true", help=f"download Ghidra {GHIDRA_VERSION} if missing")
    ap.add_argument("--ghidra-dir", help=f"an extracted Ghidra {GHIDRA_VERSION} release")
    ap.add_argument("--java-home", help=f"a JDK {MIN_JAVA}+ home")
    ap.add_argument("--skip-smoke-test", action="store_true")
    a = ap.parse_args(argv)
    try:
        node = check_node()
        install_rea()
        ghidra = find_ghidra(a.ghidra_dir, a.download_ghidra)
        java = find_java(a.java_home)
        write_mcp_json(node, ghidra, java)
        if not a.skip_smoke_test:
            smoke_test(node)
    except (SetupError, subprocess.CalledProcessError) as e:
        print(f"[setup_rea] FAILED: {e}", file=sys.stderr)
        return 1
    step("done. Restart Claude Code (or your MCP client) so it picks up the rea server.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

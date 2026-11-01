"""Launch a registered Electron target with 127.0.0.1 debugging ports so the MCP server can reach it.

The instance keeps running after this command exits. Its pid and ports are recorded in
runtime/live/<target>.json, which the server reads (WAM_TARGET_PID + WAM_CDP_PORT override it).
Any local process can drive an app through its debugging port, so use this only for authorized,
non-sensitive instances. By default the user-data dir is a fresh temp dir (disposable state).

Usage: python -m runtime.electron_launch marktext [--user-data-dir DIR] [--inspect]
       python -m runtime.electron_launch marktext --stop
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runtime.bridge import pid_alive  # noqa: E402
from runtime.electron_bridge import ElectronInstance  # noqa: E402

LIVE_DIR = ROOT / "runtime" / "live"


def state_path(target: str) -> Path:
    return LIVE_DIR / f"{target}.json"


def read_state(target: str) -> dict | None:
    p = state_path(target)
    if not p.exists():
        return None
    st = json.loads(p.read_text(encoding="utf-8"))
    return st if pid_alive(st["pid"]) else None


def main(argv=None) -> int:
    from validation.harness import TARGETS, kind_of

    ap = argparse.ArgumentParser()
    ap.add_argument("target", choices=[t for t in TARGETS if kind_of(t) == "electron"])
    ap.add_argument("--user-data-dir")
    ap.add_argument("--inspect", action="store_true", help="also open the Node inspector (main-process adapters)")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args(argv)

    running = read_state(a.target)
    if a.stop:
        if running:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(running["pid"])], capture_output=True)
            if running.get("disposable"):
                for _ in range(10):  # children may hold files for a moment after taskkill
                    shutil.rmtree(running["user_data_dir"], ignore_errors=True)
                    if not Path(running["user_data_dir"]).exists():
                        break
                    time.sleep(0.3)
        state_path(a.target).unlink(missing_ok=True)
        print(json.dumps({"stopped": bool(running)}))
        return 0
    if running:
        print(json.dumps({"already_running": running}, indent=2))
        return 0
    spec = TARGETS[a.target]
    inst = ElectronInstance(spec["app_dir"] / spec["exe"], spec.get("args"), spec.get("user_data_arg", "--user-data-dir={dir}"),
                            inspect=a.inspect, user_data_dir=Path(a.user_data_dir) if a.user_data_dir else None)
    pid = inst.start()
    if not inst.wait_endpoint():
        inst.stop()
        print("remote debugging endpoint did not come up")
        return 1
    state = {"pid": pid, **inst.ports, "user_data_dir": str(inst.user_data_dir), "disposable": not a.user_data_dir}
    LIVE_DIR.mkdir(exist_ok=True)
    state_path(a.target).write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

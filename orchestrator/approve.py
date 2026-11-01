"""Record a user's approval to publish a verified, state-mutating adapter as an MCP tool.

Usage: python -m orchestrator.approve notepadpp new_document
"""
from __future__ import annotations

import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runtime.bridge import Adapter  # noqa: E402


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    adapter = Adapter.load(*argv)
    m = adapter.manifest
    if m.get("status") != "verified":
        print(f"refusing: {argv[0]}/{argv[1]} status is {m.get('status')!r}; run validation first")
        return 1
    if m["tool"].get("destructive"):
        print("refusing: destructive tools are never published")
        return 1
    m["approval"] = {"by": getpass.getuser(), "at": datetime.now(timezone.utc).isoformat(),
                     "sha256": m["app"]["sha256"]}
    (adapter.path / "manifest.json").write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    print(f"approved {m['tool']['name']} for {m['app']['name']} {m['app']['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

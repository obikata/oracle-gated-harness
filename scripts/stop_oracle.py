#!/usr/bin/env python3
"""Stop hook: in unattended mode, the agent may not finish with a red fast oracle.

If the fast oracle fails and this is the first stop attempt, block the stop and
hand the failure tail back to the agent. On a second attempt
(stop_hook_active=true) let it stop; the orchestrator gate records the failure.
Interactive sessions are never blocked.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zonelib  # noqa: E402


def main() -> None:
    if zonelib.mode() != zonelib.UNATTENDED:
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if data.get("stop_hook_active"):
        sys.exit(0)
    root = zonelib.project_dir(data)
    if not (root / zonelib.CONFIG_REL).exists():
        sys.exit(0)  # project not using the harness
    cfg = zonelib.load_config(root)
    cmd = cfg.get("oracle_fast_cmd", "python -m pytest -m fast -q -x")
    try:
        r = subprocess.run(cmd, shell=True, cwd=root, capture_output=True,
                           text=True, timeout=cfg.get("oracle_timeout_s", 300))
    except subprocess.TimeoutExpired:
        print(json.dumps({"decision": "block",
                          "reason": "Fast oracle timed out. Make it pass before finishing."}))
        sys.exit(0)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-40:])
        print(json.dumps({
            "decision": "block",
            "reason": ("Fast oracle is RED. Fix the cause (not the tests: tests are Z0) "
                       "or, if you cannot, write what blocks you in your final report "
                       f"and stop.\n--- oracle tail ---\n{tail}"),
        }))
    sys.exit(0)


if __name__ == "__main__":
    main()

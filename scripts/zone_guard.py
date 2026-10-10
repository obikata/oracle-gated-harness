#!/usr/bin/env python3
"""PreToolUse hook: enforce autonomy zones (Z0-Z3) before any write.

Reads the hook JSON on stdin. Emits a PreToolUse deny decision when a write
targets a protected zone. Fails closed in unattended mode.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zonelib  # noqa: E402

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def targets_for(tool: str, tool_input: dict) -> list[str]:
    if tool in FILE_TOOLS:
        p = tool_input.get("file_path") or tool_input.get("notebook_path")
        return [p] if p else []
    if tool == "Bash":
        return zonelib.bash_write_targets(tool_input.get("command", ""))
    return []


def main() -> None:
    run_mode = zonelib.mode()
    try:
        data = json.load(sys.stdin)
        root = zonelib.project_dir(data)
        if not (root / zonelib.CONFIG_REL).exists():
            sys.exit(0)  # project not using the harness
        cfg = zonelib.load_config(root)
        tool = data.get("tool_name", "")
        for t in targets_for(tool, data.get("tool_input") or {}):
            rel = zonelib.rel_path(t, root)
            if rel is None and zonelib.is_agent_scratch(t):
                continue
            is_new = None
            if rel and rel != zonelib.WHOLE_TREE and zonelib.is_append_only(rel, cfg):
                is_new = not zonelib.exists_at_head(rel, root)
                if tool == "Bash" and is_new is True:
                    is_new = None  # shell can't be trusted to only create (rm/mv/sed -i); diff check decides
            ok, reason = zonelib.decide(rel, cfg, run_mode, is_new)
            if not ok:
                deny(f"[zone_guard] {reason}")
    except SystemExit:
        raise
    except Exception as e:  # fail closed only when nobody is watching
        if run_mode == zonelib.UNATTENDED:
            deny(f"[zone_guard] internal error, failing closed: {e!r}")
        print(f"[zone_guard] warning: {e!r}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()

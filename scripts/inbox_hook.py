#!/usr/bin/env python3
"""PostToolUse hook: deliver messages the user typed in the browser app to the running agent.

Only active when OGH_THREAD_DIR is set (unattended runs started by run_task.py).
Unread inbox messages are injected as additionalContext after the agent's next tool call,
and a 'delivered' event is posted to the thread.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
import threads  # noqa: E402


def main() -> None:
    d = threads.from_env()
    if d is None:
        sys.exit(0)
    try:
        sys.stdin.read()
    except Exception:
        pass
    msgs = threads.take_unread(d)
    if not msgs:
        sys.exit(0)
    cfg = {"language": threads.read_meta(d).get("language", "en")}
    for m in msgs:
        threads.post(d, "harness", "delivered", threads.t(cfg, "delivered", text=m))
    body = "\n".join(f"- {m}" for m in msgs)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (
            "MESSAGE FROM THE HUMAN OWNER, sent live while you work. Read it now, acknowledge it in one short "
            "line addressed to them, and adjust your plan accordingly (it overrides earlier instructions unless "
            "it would violate the zone rules):\n" + body),
    }}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()

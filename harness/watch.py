#!/usr/bin/env python3
"""Live view of what the agent is doing in an unattended run.

    python3 <plugin>/harness/watch.py            # follow the newest run of the repo you're in
    python3 <plugin>/harness/watch.py --all      # also print assistant reasoning text in full
    python3 <plugin>/harness/watch.py path/to/generator_1.jsonl

Reads the stream-json log run_task.py writes to .harness/<task>/generator_<i>.jsonl
inside the task's worktree, and keeps following newer runs/iterations as they appear.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

C = {"dim": "\033[2m", "b": "\033[1m", "r": "\033[31m", "g": "\033[32m", "y": "\033[33m",
     "c": "\033[36m", "m": "\033[35m", "x": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_line(name: str, inp: dict) -> str:
    key = {"Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
           "MultiEdit": "file_path", "NotebookEdit": "notebook_path", "Glob": "pattern",
           "Grep": "pattern", "WebSearch": "query", "WebFetch": "url", "Task": "description",
           "Agent": "description"}.get(name)
    arg = inp.get(key, "") if key else json.dumps(inp)[:120]
    color = {"Bash": C["y"], "Edit": C["m"], "Write": C["m"], "MultiEdit": C["m"]}.get(name, C["c"])
    return f"{color}{name:<9}{C['x']} {short(arg, 150)}"


def render(ev: dict, show_text: bool) -> list[str]:
    t = ev.get("type")
    out: list[str] = []
    if t == "system" and ev.get("subtype") == "init":
        out.append(f"{C['dim']}session start · model {ev.get('model', '?')} · cwd {ev.get('cwd', '')}{C['x']}")
    elif t == "assistant":
        for blk in (ev.get("message") or {}).get("content", []):
            if blk.get("type") == "text" and blk.get("text", "").strip():
                txt = blk["text"].strip()
                out.append(f"{C['dim']}» {txt if show_text else short(txt, 160)}{C['x']}")
            elif blk.get("type") == "tool_use":
                out.append(tool_line(blk.get("name", "?"), blk.get("input") or {}))
    elif t == "user":
        for blk in (ev.get("message") or {}).get("content", []):
            if isinstance(blk, dict) and blk.get("type") == "tool_result" and blk.get("is_error"):
                c = blk.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                tag = "BLOCKED" if "zone_guard" in str(c) else "error"
                out.append(f"{C['r']}  ✗ {tag}: {short(c, 200)}{C['x']}")
    elif t == "result":
        cost = ev.get("total_cost_usd")
        out.append(f"{C['g']}{C['b']}■ agent finished · {ev.get('subtype')} · turns {ev.get('num_turns')}"
                   f"{f' · ${cost:.2f}' if isinstance(cost, (int, float)) else ''}{C['x']}"
                   f"{C['dim']}  (harness now runs oracles + evaluator){C['x']}")
    elif t == "harness":
        out.append(f"{C['r']}{C['b']}■ harness: {ev}{C['x']}")
    return out


def newest_log(repo: Path) -> Path | None:
    wt_root = repo.parent / f".{repo.name}-agent-worktrees"
    logs = sorted(wt_root.glob("*/.harness/*/generator_*.jsonl"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def follow(path: Path, show_text: bool, repo: Path | None) -> None:
    print(f"{C['b']}=== {path}{C['x']}")
    f = open(path, encoding="utf-8")
    idle = 0.0
    while True:
        line = f.readline()
        if line:
            idle = 0.0
            try:
                for l in render(json.loads(line), show_text):
                    print(time.strftime("%H:%M:%S ") + l, flush=True)
            except json.JSONDecodeError:
                pass
            continue
        time.sleep(0.5)
        idle += 0.5
        if repo is not None and idle >= 2:
            nxt = newest_log(repo)
            if nxt and nxt != path:
                f.close()
                return follow(nxt, show_text, repo)
            idle = 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?")
    ap.add_argument("--all", action="store_true", help="print assistant text in full")
    a = ap.parse_args()
    if a.log:
        follow(Path(a.log), a.all, None)
        return
    repo = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL).stdout.strip() or ".")
    print(f"{C['dim']}waiting for a run in {repo} … (Ctrl+C to quit){C['x']}", flush=True)
    while (p := newest_log(repo)) is None:
        time.sleep(2)
    try:
        follow(p, a.all, repo)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

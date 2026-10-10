#!/usr/bin/env python3
"""Resume runs that were killed mid-flight (reboot, full disk, OOM) and the queues they belonged to.

    cd <project> && python3 <plugin>/harness/recover.py            # resume everything interrupted, in order
    python3 <plugin>/harness/recover.py --dry-run                  # show what would be resumed

run_task.py and run_queue.py leave a marker in .harness/active/ while they run and delete it when they
finish (a STOP file, time budget or disk halt counts as finishing). A marker whose process is gone means
the run was killed. For each such task run this script:

  1. marks its thread interrupted,
  2. makes sure the worktree has a progress file for the next agent: if the dead agent never wrote one,
     a note listing the files it left and its last messages; if it did, an appended "interrupted" line,
  3. re-runs it with run_task.py --resume-worktree (one at a time, in the foreground).

Then every interrupted queue is continued with its remaining tasks and remaining time budget.
Meant to be run by hand after a crash, or from cron (@reboot). Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_ext as hx  # noqa: E402
import threads as th  # noqa: E402
import zonelib  # noqa: E402

TAIL_MESSAGES = 10


def git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return r.stdout


def progress_note(task_id: str, rec: dict, thread_dir: Path | None) -> str:
    wt = Path(rec["worktree"])
    changed = git(["status", "--short"], wt).strip() or "(no uncommitted changes)"
    last = []
    if thread_dir and thread_dir.is_dir():
        evs = [e for e in th.read_thread(thread_dir) if e.get("role") == "generator" and e.get("kind") == "text"]
        last = [f"- {e['ts']}: {' '.join(str(e['text']).split())[:400]}" for e in evs[-TAIL_MESSAGES:]]
    return "\n".join([
        f"# Progress: {task_id} (INTERRUPTED, resumed by recover.py {th.now()})",
        "",
        "The previous run was killed from outside (host reboot, full disk or similar) before it could write",
        "this file, so this note was generated from what is on disk. Inspect and verify the files below and",
        "reuse finished work instead of regenerating it. Anything that was mid-write when the run died may be",
        "incomplete: check it before trusting it.",
        "",
        "## Files the previous run left (git status in the worktree)",
        "```", changed, "```",
        "",
        "## Its last messages",
        *(last or ["(none recorded)"]),
        "",
    ])


def prepare(task_id: str, rec: dict, cfg: dict, dry_run: bool) -> list[str] | None:
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        print(f"[{task_id}] worktree {wt} is gone; cannot resume (marker left for inspection)")
        return None
    td = Path(rec["thread"]) if rec.get("thread") else None
    prog = wt / hx.dirs(cfg)["progress_dir"] / f"{task_id}.md"
    if not dry_run:
        if td and td.is_dir() and th.read_meta(td).get("status") == "running":
            th.post(td, "harness", "status", "Interrupted (the run was killed from outside). Resuming in a new thread.")
            th.write_meta(td, {"status": "interrupted", "verdict": None})
        prog.parent.mkdir(parents=True, exist_ok=True)
        if prog.exists():
            with open(prog, "a", encoding="utf-8") as f:
                f.write(f"\n\n_Interrupted from outside; resumed by recover.py {th.now()}. Verify files that may have"
                        " been mid-write when it died._\n")
        else:
            prog.write_text(progress_note(task_id, rec, td), encoding="utf-8")
    cmd = [sys.executable, str(HERE / "run_task.py"), "--task", rec["task_path"]]
    if rec.get("task_id"):
        cmd += ["--id", rec["task_id"]]
    return cmd + ["--resume-worktree", str(wt)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print what would be resumed; change nothing")
    a = ap.parse_args()
    repo = Path(git(["rev-parse", "--show-toplevel"], Path.cwd()).strip() or ".").resolve()
    cfg = zonelib.load_config(repo)
    markers = hx.read_active(repo)
    dead_tasks = {k: r for k, r in markers.items()
                  if r.get("kind") == "task" and not hx.active_alive(r, "run_task.py")}
    dead_queues = {k: r for k, r in markers.items()
                   if r.get("kind") == "queue" and not hx.active_alive(r, "run_queue.py")}
    live = sorted(set(markers) - set(dead_tasks) - set(dead_queues))
    if live:
        print("still running, left alone: " + ", ".join(live))
    if not dead_tasks and not dead_queues:
        print("nothing to recover")
        return 0

    for task_id, rec in dead_tasks.items():
        cmd = prepare(task_id, rec, cfg, a.dry_run)
        if cmd is None:
            continue
        print(f"[{task_id}] resume: {' '.join(cmd)}", flush=True)
        if not a.dry_run:
            hx.clear_active(repo, task_id)  # run_task writes a fresh one
            subprocess.run(cmd, cwd=repo, stdin=subprocess.DEVNULL)

    for name, rec in dead_queues.items():
        remaining = [s for s in rec.get("remaining", []) if Path(s).is_file()]
        minutes = (float(rec.get("deadline", 0)) - time.time()) / 60
        if not remaining or minutes <= 0:
            why = "no tasks left" if not remaining else "its time budget has run out"
            print(f"[{name}] not continuing: {why}")
            if not a.dry_run:
                hx.clear_active(repo, name)
            continue
        cmd = [sys.executable, str(HERE / "run_queue.py"), "--tasks", *remaining, "--minutes", f"{minutes:.0f}"]
        if rec.get("model"):
            cmd += ["--model", rec["model"]]
        print(f"[{name}] continue queue: {' '.join(cmd)}", flush=True)
        if not a.dry_run:
            hx.clear_active(repo, name)
            subprocess.run(cmd, cwd=repo, stdin=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

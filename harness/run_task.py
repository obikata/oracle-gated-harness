#!/usr/bin/env python3
"""Run ONE task through the full harness: worktree -> generator iterations -> oracle -> zone check -> evaluator -> branch commit.

Standalone the reference orchestrator. Copy or import it for your own loop.

    python <plugin>/harness/run_task.py --task .claude/harness/tasks/<task>.json
    python <plugin>/harness/run_task.py --task ... --dry-run     # validate + print prompt/commands only

Run from inside the project repository.

Never merges. Never touches the base branch.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_ext as hx  # noqa: E402
import zonelib  # noqa: E402  (path inserted by harness_ext)


def sh(cmd: list[str] | str, cwd: Path, env=None, timeout=None, shell=False) -> subprocess.CompletedProcess:
    return subprocess.run(stdin=subprocess.DEVNULL, args=cmd, cwd=cwd, env=env, timeout=timeout, shell=shell, capture_output=True, text=True)


def run_oracle(cmd: str, wt: Path, timeout: int) -> tuple[bool, str]:
    try:
        r = sh(cmd, wt, timeout=timeout, shell=True)
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s"
    return r.returncode == 0, r.stdout + r.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help=".json spec, or .jsonl (with --id) incl. legacy night_run tasks")
    ap.add_argument("--id", help="task id inside a .jsonl file")
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--model", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-worktree", action="store_true")
    a = ap.parse_args()

    repo = Path(sh(["git", "rev-parse", "--show-toplevel"], Path.cwd()).stdout.strip())
    cfg = zonelib.load_config(repo)
    if a.id:
        matches = [t for t in hx.load_tasks(a.task, repo) if t["id"] == a.id]
        if not matches:
            print(f"no task with id {a.id!r} in {a.task}", file=sys.stderr)
            return 2
        task = matches[0]
    else:
        task = hx.load_task(a.task, repo)
    task_timeout = float(cfg.get("task_timeout_min", 60)) * 60
    oracle_timeout = int(cfg.get("oracle_timeout_s", 300))
    full_timeout = int(cfg.get("oracle_full_timeout_s", oracle_timeout * 3))
    base = sh(["git", "rev-parse", a.base], repo).stdout.strip()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    branch = f"agent/{stamp}/{task['id']}"
    prompt = hx.generator_prompt(task, cfg, repo)

    if a.dry_run:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
            tf.write(prompt)
        cmd = hx.generator_cmd(task, tf.name, a.model)[:-1] + ["<prompt>"]
        print(prompt, "\n\n--- generator cmd ---")
        print(shlex.join(cmd))
        print(f"\nbranch would be {branch} from {base[:10]}")
        return 0

    # outside the repo so test discovery in the main tree never sees worktree copies
    wt = repo.parent / f".{repo.name}-agent-worktrees" / f"{stamp}-{task['id']}"
    r = sh(["git", "worktree", "add", "-b", branch, str(wt), base], repo)
    if r.returncode:
        print(r.stderr, file=sys.stderr)
        return 2
    prompt_file = wt / ".harness" / task["id"] / "prompt.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt, encoding="utf-8")

    gutter = hx.GutterDetector()
    status, report, fast_log = "budget_exhausted", "", ""
    t0 = time.time()
    for it in range(1, task["budget"]["max_iterations"] + 1):
        print(f"[{task['id']}] iteration {it}")
        log = prompt_file.parent / f"generator_{it}.jsonl"
        print(f"[{task['id']}] live log: {log}   (watch: python3 {Path(__file__).parent / 'watch.py'})", flush=True)
        _rc, res = hx.run_streaming(hx.generator_cmd(task, prompt_file, a.model), wt, hx.generator_env(),
                                    task_timeout, log)
        report = res or report or f"(no final result; generator rc={_rc}, see {log.name})"
        ok, fast_log = run_oracle(cfg["oracle_fast_cmd"], wt, oracle_timeout)
        if ok:
            status = "oracle_green"
            break
        if gutter.observe(fast_log, ok):
            status = "gutter"
            print(f"[{task['id']}] same failure {gutter.limit}x in a row, stopping")
            break

    full_ok, full_log = run_oracle(cfg["oracle_full_cmd"], wt, full_timeout)
    zc = hx.check_diff_zones(wt, base)
    nochange = hx.no_change_problem(task, zc, cfg)
    logs = {"oracle_fast": fast_log, "oracle_full": full_log, "report": report}
    if not zc["ok"]:
        verdict = hx.run_evaluator(wt, task, base, logs, zc)  # short-circuits to fail, writes evidence
    elif nochange:
        hx.write_evidence(wt, task, base, logs, zc)
        verdict = {"verdict": "fail", "summary": nochange, "criteria": [], "issues": [
            {"severity": "blocker", "where": "(diff)", "what": nochange}], "ratchet_candidates": []}
    elif not full_ok:
        hx.write_evidence(wt, task, base, logs, zc)
        verdict = {"verdict": "fail", "summary": "Full oracle is red.", "criteria": [], "issues": [
            {"severity": "blocker", "where": "oracle_full.log", "what": "\n".join(full_log.strip().splitlines()[-8:])}],
            "ratchet_candidates": []}
    else:
        verdict = hx.run_evaluator(wt, task, base, logs, zc)

    summary = {"task": task["id"], "type": task["type"], "branch": branch, "status": status,
               "oracle_full": "green" if full_ok else "red", "zones_ok": zc["ok"],
               "verdict": verdict["verdict"], "summary": verdict.get("summary", ""),
               "minutes": round((time.time() - t0) / 60, 1)}
    (wt / ".harness" / task["id"] / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # commit everything (evidence included) to the task branch; base is never touched
    sh(["git", "add", "-A"], wt)
    sh(["git", "add", "-f", f".harness/{task['id']}"], wt)
    msg = f"agent({task['id']}): {status}, full={summary['oracle_full']}, verdict={verdict['verdict']}\n\n{summary['summary']}"
    sh(["git", "commit", "-q", "--no-verify", "-m", msg], wt)

    out = repo / hx.dirs(cfg)["runs_dir"]
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stamp}-{task['id']}.json").write_text(json.dumps({**summary, "verdict_detail": verdict}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if not a.keep_worktree:
        sh(["git", "worktree", "remove", "--force", str(wt)], repo)
    return 0 if verdict["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())

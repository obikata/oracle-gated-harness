#!/usr/bin/env python3
"""Run a queue of tasks unattended and write a morning triage report.

    python <plugin>/harness/run_queue.py                       # every .claude/harness/tasks/*.json (not examples/)
    python <plugin>/harness/run_queue.py --queue queue.txt     # task paths, one per line, '#' comments
    python <plugin>/harness/run_queue.py --dry-run             # validate all, run nothing

Tasks run one after another, each through run_task.py (own worktree + branch).
A task that crashes or fails never stops the queue. Run it from the project root,
e.g. from cron at night: `cd ~/pdg && python $OGH/harness/run_queue.py >> reports/agent-runs/queue.log 2>&1`
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
import zonelib  # noqa: E402

ORDER = {"pass": 0, "needs_human": 1, "fail": 2, "crash": 3, "invalid": 4}


def collect(repo: Path, queue: str | None) -> list[Path]:
    if queue:
        lines = Path(queue).read_text(encoding="utf-8").splitlines()
        return [repo / l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    return sorted((repo / ".claude" / "harness" / "tasks").glob("*.json"))


def report_md(rows: list[dict], started: str, minutes: float) -> str:
    rows = sorted(rows, key=lambda r: (ORDER.get(r["verdict"], 9), r["task"]))
    n = {k: sum(r["verdict"] == k for r in rows) for k in ORDER}
    out = [f"# Agent run triage — {started}", "",
           f"{len(rows)} tasks in {minutes:.0f} min · **{n['pass']} pass** · {n['needs_human']} needs_human · "
           f"{n['fail']} fail · {n['crash'] + n['invalid']} crash/invalid", "",
           "| verdict | task | status | full oracle | zones | branch |", "|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['verdict']} | {r['task']} | {r.get('status', '')} | {r.get('oracle_full', '')} | "
                   f"{'ok' if r.get('zones_ok') else ('VIOLATION' if 'zones_ok' in r else '')} | `{r.get('branch', '')}` |")
    out += ["", "## Details", ""]
    for r in rows:
        out += [f"### {r['verdict'].upper()} · {r['task']}", "", r.get("summary", "") or "(no summary)", ""]
        d = r.get("verdict_detail") or {}
        for i in d.get("issues", []):
            out.append(f"- **{i.get('severity')}** `{i.get('where')}`: {i.get('what')}")
        for c in d.get("ratchet_candidates", []):
            out.append(f"- ratchet? ({c.get('proposed_guard')}) {c.get('pattern')}")
        if r.get("error"):
            out += ["```", r["error"][-1500:], "```"]
        out.append("")
    out += ["## Next", "", "- `pass` → review branch, merge by hand",
            "- `fail` → would a guard have caught it earlier? → ratchet",
            "- check `proposals/` for guidance-core patches", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue")
    ap.add_argument("--model")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    repo = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                               text=True, check=True).stdout.strip())
    cfg = zonelib.load_config(repo)
    runs_dir = repo / hx.dirs(cfg)["runs_dir"]
    runs_dir.mkdir(parents=True, exist_ok=True)
    started, t0 = time.strftime("%Y-%m-%d %H:%M"), time.time()

    tasks, rows = [], []
    for p in collect(repo, a.queue):
        try:
            tasks.append((p, hx.load_task(p, repo)))
        except Exception as e:  # invalid spec: report it, keep going
            rows.append({"task": p.stem, "verdict": "invalid", "summary": f"Invalid task spec: {e}"})
    print(f"{len(tasks)} valid task(s), {len(rows)} invalid")
    if a.dry_run:
        for p, t in tasks:
            print(f"  ok  {t['id']:<32} {t['type']:<12} {p.relative_to(repo)}")
        for r in rows:
            print(f"  BAD {r['task']:<32} {r['summary']}")
        return 0 if not rows else 1

    for p, t in tasks:
        print(f"[{time.strftime('%H:%M:%S')}] start {t['id']}", flush=True)
        before = set(runs_dir.glob(f"*-{t['id']}.json"))
        cmd = [sys.executable, str(HERE / "run_task.py"), "--task", str(p)] + (["--model", a.model] if a.model else [])
        try:
            r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=6 * 3600)
            new = sorted(set(runs_dir.glob(f"*-{t['id']}.json")) - before)
            if new:
                rows.append(json.loads(new[-1].read_text(encoding="utf-8")))
            else:
                rows.append({"task": t["id"], "verdict": "crash", "summary": "run_task produced no summary",
                             "error": (r.stdout + r.stderr)})
        except subprocess.TimeoutExpired as e:
            rows.append({"task": t["id"], "verdict": "crash", "summary": "timeout (6h)", "error": str(e)})
        print(f"[{time.strftime('%H:%M:%S')}] done  {t['id']} -> {rows[-1]['verdict']}", flush=True)

    md = report_md(rows, started, (time.time() - t0) / 60)
    out = runs_dir / f"{time.strftime('%Y%m%d-%H%M')}-triage.md"
    out.write_text(md, encoding="utf-8")
    print(f"\ntriage report: {out.relative_to(repo)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""oracle-gated-harness: building blocks for an unattended agent loop.

Use run_task.py as-is, or call these from your own orchestrator:

    task = harness_ext.load_task(path, repo_root)        # before the run
    cmd  = harness_ext.generator_cmd(task, prompt_file)  # per iteration, cwd=worktree, env=generator_env()
    gut  = harness_ext.GutterDetector()                  # per task; gut.observe(oracle_log) -> True means stop
    zc   = harness_ext.check_diff_zones(worktree, base)  # after the generator, before commit
    v    = harness_ext.run_evaluator(worktree, task, base, logs)  # final judgement

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
import zonelib  # noqa: E402

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,48}$")


class TaskError(ValueError):
    pass


def dirs(cfg: dict) -> dict:
    d = {"research_dir": "reports/research/", "progress_dir": "reports/progress/",
         "proposals_dir": "proposals/", "runs_dir": "reports/agent-runs/"}
    d.update({k: v for k, v in cfg.get("dirs", {}).items() if k in d})
    return {k: v if v.endswith("/") else v + "/" for k, v in d.items()}


# ----------------------------------------------------------------------------- task spec

def validate_task(task: dict, cfg: dict) -> dict:
    errs: list[str] = []
    for k in ("id", "type", "title", "goal", "acceptance", "scope", "budget"):
        if k not in task:
            errs.append(f"missing '{k}'")
    if errs:
        raise TaskError("; ".join(errs))
    if not TASK_ID_RE.match(task["id"]):
        errs.append("id must be lowercase kebab-case, 3-49 chars")
    if task["type"] not in ("engineering", "research"):
        errs.append("type must be engineering|research")
    acc = task["acceptance"]
    if not isinstance(acc, list) or not acc or not all(isinstance(a, str) and a.strip() for a in acc):
        errs.append("acceptance must be a non-empty list of strings")
    allow = task["scope"].get("allow") if isinstance(task["scope"], dict) else None
    if not isinstance(allow, list) or not allow:
        errs.append("scope.allow must be a non-empty list of globs")
    else:
        for g in allow:
            z = zonelib.classify(g.rstrip("*").rstrip("/") or g, cfg)
            if z == "Z0":
                errs.append(f"scope.allow '{g}' is Z0 (frozen)")
            elif z == "Z1":
                errs.append(f"scope.allow '{g}' is Z1; unattended tasks reach Z1 only via {dirs(cfg)['proposals_dir']}")
    b = task["budget"]
    for k, lo, hi in (("max_iterations", 1, 10), ("max_turns", 5, 200), ("max_budget_usd", 0.01, 50)):
        v = b.get(k) if isinstance(b, dict) else None
        if not isinstance(v, (int, float)) or not (lo <= v <= hi):
            errs.append(f"budget.{k} must be in [{lo}, {hi}]")
    if task["type"] == "research":
        r = task.get("research")
        if not isinstance(r, dict) or not r.get("hypothesis") or not r.get("deliverable_dir"):
            errs.append("research tasks need research.hypothesis and research.deliverable_dir")
        elif not str(r["deliverable_dir"]).startswith(dirs(cfg)["research_dir"]):
            errs.append(f"research.deliverable_dir must be under {dirs(cfg)['research_dir']}")
    if errs:
        raise TaskError(f"task {task.get('id')!r}: " + "; ".join(errs))
    return task


def load_task(path: str | Path, repo_root: str | Path) -> dict:
    cfg = zonelib.load_config(Path(repo_root))
    with open(path, encoding="utf-8") as f:
        return validate_task(json.load(f), cfg)


# ----------------------------------------------------------------------------- generator

def generator_prompt(task: dict, cfg: dict | None = None) -> str:
    """The per-iteration prompt. Fresh context every iteration; state lives in files."""
    d = dirs(cfg or {})
    progress = f"{d['progress_dir']}{task['id']}.md"
    lines = [
        f"# Task {task['id']} ({task['type']}): {task['title']}",
        "", "## Goal", task["goal"], "", "## Acceptance criteria",
        *[f"- {a}" for a in task["acceptance"]],
        "", "## Scope", "You may edit: " + ", ".join(task["scope"]["allow"]),
    ]
    if task["scope"].get("context"):
        lines.append("Read first: " + ", ".join(task["scope"]["context"]))
    if task["type"] == "research":
        r = task["research"]
        lines += ["", "## Research", f"Hypothesis: {r['hypothesis']}",
                  f"Conditions: {r.get('conditions', '(see goal)')}",
                  f"Write data, figures and report.md to {r['deliverable_dir']}. "
                  "A clear negative result is a valid outcome. Every number in the report must come from a file you saved."]
    lines += [
        "", "## Working rules",
        f"- Handoff file: {progress}. Read it first if it exists (earlier iterations wrote it).",
        f"- Before you stop, update {progress}: what you did, what is left, what blocked you.",
        "- Your final message is your report: changes, evidence for each acceptance criterion, open problems.",
        "- Zones are enforced by hooks. Oracles and harness config are frozen (Z0); never try to edit them.",
        f"- Core changes (Z1) go into {d['proposals_dir']}{task['id']}/*.patch (git diff format), never applied directly.",
        "- Follow CLAUDE.md.",
    ]
    return "\n".join(lines)


def generator_env() -> dict:
    env = dict(os.environ)
    env[zonelib.MODE_ENV] = zonelib.UNATTENDED
    env.pop(zonelib.ALLOW_Z0_ENV, None)
    return env


def generator_cmd(task: dict, prompt_file: str | Path, model: str | None = None) -> list[str]:
    b = task["budget"]
    cmd = [
        "claude", "-p",
        "--plugin-dir", str(PLUGIN_ROOT),
        "--permission-mode", "acceptEdits",
        "--permission-prompts", "none",          # nobody is there to answer
        "--allowedTools", "Bash(python *)", "Bash(python3 *)", "Bash(pytest *)",
        "Bash(git diff *)", "Bash(git status *)", "Bash(git log *)", "Bash(ls *)",
        "--max-turns", str(b["max_turns"]),
        "--max-budget-usd", str(b["max_budget_usd"] / max(1, b["max_iterations"])),
        "--output-format", "json",
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(Path(prompt_file).read_text(encoding="utf-8"))
    return cmd


# ----------------------------------------------------------------------------- gutter

def failure_signature(oracle_log: str) -> str:
    failed = sorted(set(re.findall(r"^(?:FAILED|ERROR) (\S+)", oracle_log, re.M)))
    basis = "\n".join(failed) if failed else "\n".join(oracle_log.strip().splitlines()[-5:])
    basis = re.sub(r"\d+\.\d+s", "", basis)  # drop timings
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


@dataclass
class GutterDetector:
    """Stop a task when the same oracle failure repeats `limit` times in a row."""
    limit: int = 3
    history: list[str] = field(default_factory=list)

    def observe(self, oracle_log: str, passed: bool) -> bool:
        if passed:
            self.history.clear()
            return False
        self.history.append(failure_signature(oracle_log))
        tail = self.history[-self.limit:]
        return len(tail) == self.limit and len(set(tail)) == 1


# ----------------------------------------------------------------------------- zone check

def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


DEFAULT_IGNORE = (".harness/*", "__pycache__/*", "*/__pycache__/*", "*.pyc",
                  ".pytest_cache/*", "*/.pytest_cache/*", ".ipynb_checkpoints/*", "*/.ipynb_checkpoints/*")


def is_ignored(rel: str, cfg: dict) -> bool:
    import fnmatch
    pats = list(DEFAULT_IGNORE) + list(cfg.get("ignore", []))
    return any(fnmatch.fnmatchcase(rel, p) for p in pats)


def check_diff_zones(worktree: str | Path, base: str, run_mode: str = zonelib.UNATTENDED) -> dict:
    """Backstop for the hook: classify every file touched since `base` (committed or not)."""
    wt = Path(worktree).resolve()
    cfg = zonelib.load_config(wt)
    files = set(_git(["diff", "--name-only", f"{base}"], wt).split())
    files |= set(_git(["ls-files", "--others", "--exclude-standard"], wt).split())
    touched, violations = [], []
    for f in sorted(files):
        if is_ignored(f, cfg):
            continue  # evidence and build artifacts
        z = zonelib.classify(f, cfg)
        ok, reason = zonelib.decide(f, cfg, run_mode)
        touched.append({"file": f, "zone": z})
        if not ok:
            violations.append({"file": f, "zone": z, "reason": reason})
    return {"touched": touched, "violations": violations, "ok": not violations}


# ----------------------------------------------------------------------------- evaluator

def write_evidence(worktree: str | Path, task: dict, base: str, logs: dict[str, str], zone_check: dict) -> Path:
    wt = Path(worktree)
    d = wt / ".harness" / task["id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.json").write_text(json.dumps(task, indent=2), encoding="utf-8")
    _git(["add", "-A", "--intent-to-add"], wt)  # so new files show up in the diff
    (d / "diff.patch").write_text(_git(["diff", base, "--", ".", ":(exclude).harness"], wt), encoding="utf-8")
    (d / "zone_check.json").write_text(json.dumps(zone_check, indent=2), encoding="utf-8")
    for name, text in logs.items():  # e.g. {"oracle_fast": ..., "oracle_full": ..., "report": ...}
        ext = ".md" if name == "report" else ".log"
        (d / f"{name}{ext}").write_text(text, encoding="utf-8")
    return d


def evaluator_system_prompt(worktree: str | Path) -> str:
    """Plugin evaluator instructions (frontmatter stripped) + optional project domain rules."""
    body = (PLUGIN_ROOT / "agents" / "evaluator.md").read_text(encoding="utf-8")
    if body.startswith("---"):
        body = body.split("---", 2)[2].lstrip()
    domain = Path(worktree) / ".claude" / "harness" / "evaluator-domain.md"
    if domain.exists():
        body += "\n\n## Project domain rules\n\n" + domain.read_text(encoding="utf-8")
    return body


def evaluator_cmd(task: dict, schema_path: str | Path, system_prompt_file: str | Path,
                  max_turns: int = 30, max_budget_usd: float = 2.0) -> list[str]:
    prompt = (f"Evaluate task {task['id']}. Evidence is in .harness/{task['id']}/. "
              "Follow your instructions exactly and return the verdict JSON.")
    return [
        "claude", "-p",
        "--append-system-prompt-file", str(system_prompt_file),
        "--tools", "Read,Grep,Glob",
        "--permission-prompts", "none",
        "--json-schema", Path(schema_path).read_text(encoding="utf-8"),
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--max-budget-usd", str(max_budget_usd),
        "--no-session-persistence",
        prompt,
    ]


def parse_evaluator_output(stdout: str) -> dict:
    data = json.loads(stdout)
    v = data.get("structured_output")
    if v is None:
        res = data.get("result", "")
        m = re.search(r"\{.*\}", res, re.S)
        v = json.loads(m.group(0)) if m else None
    if not isinstance(v, dict) or v.get("verdict") not in ("pass", "fail", "needs_human"):
        return {"verdict": "needs_human", "summary": "Evaluator returned no valid verdict.",
                "criteria": [], "issues": [], "ratchet_candidates": [], "_raw": stdout[-2000:]}
    return v


def run_evaluator(worktree: str | Path, task: dict, base: str, logs: dict[str, str],
                  zone_check: dict | None = None, timeout_s: int = 1200) -> dict:
    wt = Path(worktree).resolve()
    zone_check = zone_check or check_diff_zones(wt, base)
    write_evidence(wt, task, base, logs, zone_check)
    if not zone_check["ok"]:  # hard fail, don't spend tokens
        return {"verdict": "fail", "summary": "Zone violation; see zone_check.json.",
                "criteria": [], "issues": [{"severity": "blocker", "where": v["file"], "what": v["reason"]}
                                           for v in zone_check["violations"]],
                "ratchet_candidates": []}
    schema = PLUGIN_ROOT / "schemas" / "evaluator_verdict.schema.json"
    sp = wt / ".harness" / task["id"] / "evaluator_system.md"
    sp.write_text(evaluator_system_prompt(wt), encoding="utf-8")
    env = dict(os.environ, **{zonelib.MODE_ENV: zonelib.UNATTENDED})
    r = subprocess.run(evaluator_cmd(task, schema, sp), cwd=wt, capture_output=True,
                       text=True, timeout=timeout_s, env=env)
    if r.returncode != 0:
        return {"verdict": "needs_human", "summary": f"Evaluator exited {r.returncode}.",
                "criteria": [], "issues": [], "ratchet_candidates": [], "_raw": (r.stdout + r.stderr)[-2000:]}
    verdict = parse_evaluator_output(r.stdout)
    (wt / ".harness" / task["id"] / "verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return verdict

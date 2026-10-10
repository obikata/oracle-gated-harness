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

def _scope_hits_append_only(g: str, cfg: dict) -> bool:
    """A scope glob in Z0 is acceptable if it can only mean 'add new files' in an append-only area."""
    pats = cfg.get("append_only", [])
    if not pats:
        return False
    import fnmatch
    probe = g.replace("*", "test_new_x.py") if "*" in g else g
    return any(fnmatch.fnmatchcase(probe, p) for p in pats)


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
            if z == "Z0" and not _scope_hits_append_only(g, cfg):
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


def from_legacy(t: dict, cfg: dict) -> dict:
    """Convert a night_run-style task {id, title, prompt, scope, max_turns} to a full spec.

    Acceptance is derived from the prompt (the prompt is the contract), and the task is
    typed research when every scope entry is under the research dir.
    """
    d = dirs(cfg)
    scope = [g if not g.endswith("/") else g + "*" for g in t.get("scope", [])] or ["*"]
    research = all(g.startswith(d["research_dir"]) for g in scope)
    tid = re.sub(r"[^a-z0-9-]+", "-", str(t["id"]).lower()).strip("-")[:49].rstrip("-")
    spec = {
        "id": tid if len(tid) >= 3 else f"task-{tid}",
        "type": "research" if research else "engineering",
        "title": t.get("title", t["id"]),
        "goal": t.get("title", t["id"]),
        "prompt": t["prompt"],
        "acceptance": t.get("acceptance") or [
            "Every numbered/explicit deliverable in the task instructions is completed, each with evidence (file, command output, or test)",
            "Anything that could not be completed is stated explicitly with the reason, not silently skipped",
            "The fast oracle passes and no Z0/Z1 file is modified",
        ],
        "scope": {"allow": scope},
        "budget": {"max_iterations": 1, "max_turns": int(t.get("max_turns", 40)),
                   "max_budget_usd": float(t.get("max_budget_usd", 10.0))},
        "_legacy": True,
    }
    if research:
        spec["research"] = {"hypothesis": "(see instructions)", "deliverable_dir": d["research_dir"]}
    return spec


def load_tasks(path: str | Path, repo_root: str | Path) -> list[dict]:
    """Load one .json spec, or a .jsonl file of specs / legacy night_run tasks."""
    cfg = zonelib.load_config(Path(repo_root))
    p = Path(path)
    raw = []
    if p.suffix == ".jsonl":
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(json.loads(line))
    else:
        raw.append(json.loads(p.read_text(encoding="utf-8")))
    out = []
    for t in raw:
        if "type" not in t and "prompt" in t:
            t = from_legacy(t, cfg)
        out.append(validate_task(t, cfg))
    return out


def load_task(path: str | Path, repo_root: str | Path) -> dict:
    tasks = load_tasks(path, repo_root)
    if len(tasks) != 1:
        raise TaskError(f"{path} holds {len(tasks)} tasks; use run_queue.py for multi-task files")
    return tasks[0]


# ----------------------------------------------------------------------------- generator

def project_preamble(repo_root: str | Path | None) -> str:
    if repo_root is None:
        return ""
    f = Path(repo_root) / ".claude" / "harness" / "agent-preamble.md"
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


def generator_prompt(task: dict, cfg: dict | None = None, repo_root: str | Path | None = None) -> str:
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
    if task.get("prompt"):
        lines += ["", "## Instructions", task["prompt"]]
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
        "- Do not git commit, push or switch branches; the harness commits your work.",
        "- You are a ONE-SHOT headless run: nobody will resume you or check on anything you leave running. "
        "Run slow sweeps in the foreground to completion; if something cannot finish within your turn budget, "
        "scope it down (fewer trials, narrower grid) and say so in your report. Never background a job.",
        "- Follow CLAUDE.md.",
    ]
    nar = (cfg or {}).get("narrate")
    if nar:
        lines += ["", "## Reporting to your human (shown live in their task thread)",
                  f"- At each milestone (plan decided, tool built, sweep started with ETA, result in, blocker hit), write ONE or TWO short "
                  f"sentences in {nar.get('language', 'English')}, addressed to {nar.get('addressee', 'the owner')}, like a teammate's "
                  "status update: what you just did, what you found, what's next. Be concrete (numbers, file names). No filler.",
                  "- If you are unsure about a judgement call, say so in the update; they may reply, and their reply will be "
                  "delivered to you after your next tool call."]
    pre = project_preamble(repo_root)
    if pre:
        lines += ["", "## Project environment", pre]
    return "\n".join(lines)


GENERATOR_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch")


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
        # Unattended runs ignore the user's personal ~/.claude/settings.json (its allow rules
        # and hooks are tuned for interactive work elsewhere); project + local settings still apply.
        "--setting-sources", "project,local",
        "--permission-mode", "acceptEdits",
        "--permission-prompts", "none",          # nobody is there to answer
        # Broad tools on purpose: narrow Bash prefix allowlists burn turns on denied
        # legitimate commands. Safety comes from the zone hook + post-run diff check.
        "--allowedTools", *GENERATOR_TOOLS,
        "--max-turns", str(b["max_turns"]),
        "--max-budget-usd", str(b["max_budget_usd"] / max(1, b["max_iterations"])),
        # stream-json: events are written live to .harness/<id>/generator_<i>.jsonl (see harness/watch.py)
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(Path(prompt_file).read_text(encoding="utf-8"))
    return cmd


# ----------------------------------------------------------------------------- active-run markers
# run_task.py / run_queue.py write a marker when they start and delete it when they finish normally.
# A marker whose process is gone means the run was killed (reboot, ENOSPC, OOM...): recover.py resumes it.

ACTIVE_DIR = Path(".harness") / "active"


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def write_active(repo: Path, name: str, rec: dict) -> Path:
    d = Path(repo) / ACTIVE_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "boot_id": boot_id(), **rec}, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def clear_active(repo: Path, name: str) -> None:
    (Path(repo) / ACTIVE_DIR / f"{name}.json").unlink(missing_ok=True)


def read_active(repo: Path) -> dict[str, dict]:
    d = Path(repo) / ACTIVE_DIR
    out = {}
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def active_alive(rec: dict, needle: str) -> bool:
    """The marker's process is still the run that wrote it (same boot, pid alive, same program)."""
    if rec.get("boot_id") and rec["boot_id"] != boot_id():
        return False
    pid = int(rec.get("pid", 0))
    if pid <= 0:
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return False
    return needle in cmd


def disk_problem(repo: Path, cfg: dict) -> str | None:
    """Refuse to start a task on a nearly full disk: a run dies mid-way on ENOSPC otherwise."""
    import shutil
    need = float(cfg.get("min_free_gb", 5))
    free = shutil.disk_usage(repo).free / 1e9
    if free >= need:
        return None
    return f"only {free:.1f} GB free on the repository's disk (min_free_gb = {need:g}); not starting"


def _die_with_parent() -> None:
    """preexec_fn: SIGTERM the child if this process dies (Linux PR_SET_PDEATHSIG), so a crashed
    run_task.py doesn't leave an orphaned agent running with nobody to commit its work."""
    try:
        import ctypes
        import signal
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


def _descendants(pid: int) -> list[int]:
    """All descendant pids via /proc. Tool subprocesses run in their own process groups,
    so killing the agent's group alone would miss them."""
    children: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(d.name))
    out, stack = [], [pid]
    while stack:
        for c in children.get(stack.pop(), []):
            out.append(c)
            stack.append(c)
    return out


def kill_tree(p: subprocess.Popen) -> None:
    import signal
    victims = [p.pid, *(_descendants(p.pid) if sys.platform.startswith("linux") else [])]
    for pid in victims:  # collected before killing, or they'd be reparented out of reach
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_streaming(cmd: list[str], cwd: Path, env: dict, timeout: float, log_path: Path,
                  thread_dir: Path | None = None, cfg: dict | None = None) -> tuple[int, str]:
    """Run a stream-json `claude -p`, appending every event to `log_path` as it arrives and,
    if `thread_dir` is given, posting human-readable events to the task's thread.

    A watchdog kills the agent on timeout or when <thread_dir>/ABORT appears.
    Returns (returncode, final result text). Never inherits the terminal's stdin.
    """
    import threading
    import time as _time
    sys.path.insert(0, str(PLUGIN_ROOT / "harness"))
    import threads as _th
    result = ""
    err_path = log_path.with_suffix(".stderr.log")
    why = {"v": None}
    with open(log_path, "w", encoding="utf-8") as out, open(err_path, "w", encoding="utf-8") as err:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=err, text=True, bufsize=1,
                             preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None)
        deadline = _time.time() + timeout

        def watchdog() -> None:
            while p.poll() is None:
                if _time.time() > deadline:
                    why["v"] = "timeout"
                elif thread_dir is not None and (Path(thread_dir) / "ABORT").exists():
                    why["v"] = "abort"
                if why["v"]:
                    kill_tree(p)
                    return
                _time.sleep(1.0)

        threading.Thread(target=watchdog, daemon=True).start()
        assert p.stdout is not None
        try:
            for line in p.stdout:
                out.write(line)
                out.flush()
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "result":
                    result = ev.get("result") or result
                if thread_dir is not None:
                    for role, kind, text, meta in _th.stream_event_to_posts(ev, cfg):
                        _th.post(thread_dir, role, kind, text, **meta)
        except BaseException:
            kill_tree(p)  # e.g. ENOSPC writing the log: don't leave the agent running unwatched
            raise
        rc = p.wait()
    if why["v"]:
        with open(log_path, "a", encoding="utf-8") as out:
            out.write(json.dumps({"type": "harness", "event": why["v"], "after_s": timeout}) + "\n")
        if why["v"] == "abort":
            _th.post(thread_dir, "harness", "status", _th.t(cfg, "aborted"))
        return (130 if why["v"] == "abort" else 124), result
    return rc, result


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
    return subprocess.run(stdin=subprocess.DEVNULL, args=["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


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
    status: dict[str, str] = {}
    for line in _git(["diff", "--name-status", "--no-renames", f"{base}"], wt).splitlines():
        st, _, f = line.partition("\t")
        status[f] = st[:1]
    for f in _git(["ls-files", "--others", "--exclude-standard"], wt).split():
        status.setdefault(f, "A")
    touched, violations = [], []
    for f in sorted(status):
        if is_ignored(f, cfg):
            continue  # evidence and build artifacts
        z = zonelib.classify(f, cfg)
        ok, reason = zonelib.decide(f, cfg, run_mode, is_new=status[f] == "A")
        touched.append({"file": f, "zone": z, "change": status[f]})
        if not ok:
            violations.append({"file": f, "zone": z, "reason": reason})
    return {"touched": touched, "violations": violations, "ok": not violations}


def no_change_problem(task: dict, zone_check: dict, cfg: dict) -> str | None:
    """False-green guard: a no-op passes every oracle. Require real output."""
    files = [t["file"] for t in zone_check["touched"]]
    progress = dirs(cfg)["progress_dir"]
    work = [f for f in files if not f.startswith(progress)]
    if not work:
        return "agent produced no file changes (false-green guard)"
    if task["type"] == "research":
        dd = task["research"]["deliverable_dir"]
        if not any(f.startswith(dd) for f in work):
            return f"research task produced nothing under {dd}"
    return None


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
        "--setting-sources", "project,local",
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
    r = subprocess.run(stdin=subprocess.DEVNULL, args=evaluator_cmd(task, schema, sp), cwd=wt, capture_output=True,
                       text=True, timeout=timeout_s, env=env)
    if r.returncode != 0:
        return {"verdict": "needs_human", "summary": f"Evaluator exited {r.returncode}.",
                "criteria": [], "issues": [], "ratchet_candidates": [], "_raw": (r.stdout + r.stderr)[-2000:]}
    verdict = parse_evaluator_output(r.stdout)
    (wt / ".harness" / task["id"] / "verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return verdict

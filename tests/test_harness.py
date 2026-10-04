"""Self-tests for oracle-gated-harness (zone guard, stop hook, task specs, diff check, gutter, init).

Run from the plugin root:  python -m pytest tests -q
Each test builds a throwaway git project, so nothing real is touched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "harness"))
sys.path.insert(0, str(PLUGIN / "scripts"))
import harness_ext as hx  # noqa: E402
import zonelib  # noqa: E402


ZONES = {
    "zones": {
        "Z0": ["tests/*", "orchestrator/*", ".claude/*", ".harness/*", "CLAUDE.md"],
        "Z1": ["guidance.py", "loop_common.py", "parameters.py"],
        "Z2": ["plant.py", "analysis/*"],
        "Z3": ["viewer/*", "docs/*", "reports/*", "proposals/*", "scratch/*"],
    },
    "default_zone": "Z2",
    "oracle_fast_cmd": "exit 0",
    "oracle_full_cmd": "exit 0",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "pdg"
    r.mkdir()
    (r / ".claude/harness/tasks").mkdir(parents=True)
    (r / ".claude/harness/zones.json").write_text(json.dumps(ZONES))
    shutil.copytree(PLUGIN / "tests/fixtures/tasks", r / ".claude/harness/tasks", dirs_exist_ok=True)
    for f in ("guidance.py", "plant.py", "viewer/app.js", "tests/test_oracle.py", "tests/conftest.py"):
        (r / f).parent.mkdir(parents=True, exist_ok=True)
        (r / f).write_text("# orig\n")
    run = lambda *a: subprocess.run(a, cwd=r, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return r


def hook(script: str, payload: dict, repo: Path, mode: str = "unattended", **env) -> dict | None:
    e = dict(os.environ, CLAUDE_PROJECT_DIR=str(repo), OGH_MODE=mode, **env)
    e.pop("OGH_ALLOW_Z0", None) if "OGH_ALLOW_Z0" not in env else None
    r = subprocess.run([sys.executable, str(PLUGIN / "scripts" / script)],
                       input=json.dumps(payload), capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def edit(path: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"}}


def bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def denied(out: dict | None) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ------------------------------------------------------------------ zone guard: file tools

@pytest.mark.parametrize("path,mode,expect_deny", [
    ("tests/test_oracle.py", "unattended", True),     # Z0
    ("tests/test_oracle.py", "interactive", True),    # Z0 even interactive
    (".claude/harness/zones.json", "unattended", True),  # harness config is frozen
    ("orchestrator/night_run.py", "unattended", True),
    ("guidance.py", "unattended", True),              # Z1 unattended
    ("guidance.py", "interactive", False),            # Z1 paired
    ("plant.py", "unattended", False),                # Z2
    ("viewer/app.js", "unattended", False),           # Z3
    ("proposals/t1/guidance.patch", "unattended", False),
    ("new_module.py", "unattended", False),           # default Z2
    ("/etc/passwd", "unattended", True),              # outside repo
])
def test_file_tools(repo, path, mode, expect_deny):
    p = path if path.startswith("/") else str(repo / path)
    assert denied(hook("zone_guard.py", edit(p), repo, mode)) is expect_deny


def test_relative_and_dotdot_paths(repo):
    assert denied(hook("zone_guard.py", edit("tests/../tests/test_oracle.py"), repo))
    assert denied(hook("zone_guard.py", edit(str(repo / "viewer/../guidance.py")), repo))


def test_z0_override_only_interactive(repo):
    p = str(repo / "tests/test_oracle.py")
    assert not denied(hook("zone_guard.py", edit(p), repo, "interactive", OGH_ALLOW_Z0="1"))
    assert denied(hook("zone_guard.py", edit(p), repo, "unattended", OGH_ALLOW_Z0="1"))


def test_write_and_notebook_tools(repo):
    assert denied(hook("zone_guard.py", {"tool_name": "Write", "tool_input": {"file_path": str(repo / "guidance.py"), "content": "x"}}, repo))
    assert denied(hook("zone_guard.py", {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": str(repo / "tests/n.ipynb")}}, repo))


# ------------------------------------------------------------------ zone guard: bash

@pytest.mark.parametrize("cmd,expect_deny", [
    ("python -m pytest -m fast -q", False),
    ("echo hi > viewer/out.txt", False),
    ("echo hi > tests/test_oracle.py", True),
    ("cat x >> guidance.py", True),
    ("sed -i 's/a/b/' guidance.py", True),
    ("sed 's/a/b/' guidance.py", False),               # read-only sed
    ("rm -f tests/test_oracle.py", True),
    ("mv plant.py tests/plant.py", True),
    ("cp scratch/g.py guidance.py", True),
    ("git checkout -- tests/test_oracle.py", True),
    ("git restore guidance.py", True),
    ("git reset --hard", True),                        # whole tree
    ("git stash", True),
    ("git diff main", False),
    ("ls tests && cat guidance.py", False),
    ("cd analysis && python run.py | tee reports/out.log", False),
    ("echo x | tee tests/x.py", True),
    ("FOO=1 rm guidance.py", True),
    ("python -c \"open('guidance.py','w').write('')\"", True),
    ("cat reports/x.md 2>/dev/null", False),            # stderr discard, not a write
    ("ls guidance.py 1>/dev/null 2>&1", False),
])
def test_bash(repo, cmd, expect_deny):
    assert denied(hook("zone_guard.py", bash(cmd), repo)) is expect_deny


def test_fail_closed_unattended(repo):
    (repo / ".claude/harness/zones.json").write_text("{broken")
    assert denied(hook("zone_guard.py", edit(str(repo / "plant.py")), repo, "unattended"))
    assert hook("zone_guard.py", edit(str(repo / "plant.py")), repo, "interactive") is None


# ------------------------------------------------------------------ stop hook

def _set_oracle(repo: Path, cmd: str):
    cfg = json.loads((repo / ".claude/harness/zones.json").read_text())
    cfg["oracle_fast_cmd"] = cmd
    (repo / ".claude/harness/zones.json").write_text(json.dumps(cfg))


def test_stop_blocks_red_once(repo):
    _set_oracle(repo, "echo 'FAILED tests/test_oracle.py::test_lcvx' && exit 1")
    out = hook("stop_oracle.py", {"stop_hook_active": False}, repo)
    assert out["decision"] == "block" and "test_lcvx" in out["reason"]
    assert hook("stop_oracle.py", {"stop_hook_active": True}, repo) is None   # no infinite loop


def test_stop_green_and_interactive(repo):
    _set_oracle(repo, "exit 0")
    assert hook("stop_oracle.py", {"stop_hook_active": False}, repo) is None
    _set_oracle(repo, "exit 1")
    assert hook("stop_oracle.py", {"stop_hook_active": False}, repo, "interactive") is None


# ------------------------------------------------------------------ task specs

def test_example_tasks_validate(repo):
    files = list((repo / ".claude/harness/tasks").glob("*.json"))
    assert len(files) >= 2
    for f in files:
        hx.load_task(f, repo)


def test_template_task_validates_against_template_zones(tmp_path):
    (tmp_path / ".claude/harness").mkdir(parents=True)
    shutil.copy(PLUGIN / "templates/zones.json", tmp_path / ".claude/harness/zones.json")
    hx.load_task(PLUGIN / "templates/tasks/eng-example.json", tmp_path)


@pytest.mark.parametrize("mutate,msg", [
    (lambda t: t.update(acceptance=[]), "acceptance"),
    (lambda t: t["scope"].update(allow=["tests/*"]), "Z0"),
    (lambda t: t["scope"].update(allow=["guidance.py"]), "Z1"),
    (lambda t: t["budget"].update(max_budget_usd=500), "max_budget_usd"),
    (lambda t: t.update(id="Bad Id"), "kebab"),
    (lambda t: t.update(type="research"), "research tasks need"),
])
def test_task_rejections(repo, mutate, msg):
    t = json.loads((repo / ".claude/harness/tasks/eng-cross-solver.json").read_text())
    mutate(t)
    with pytest.raises(hx.TaskError, match=msg):
        hx.validate_task(t, zonelib.load_config(repo))


def test_research_dir_must_be_under_reports(repo):
    t = json.loads((repo / ".claude/harness/tasks/research-dv-wall.json").read_text())
    t["research"]["deliverable_dir"] = "tmp/out/"
    with pytest.raises(hx.TaskError, match="reports/research"):
        hx.validate_task(t, zonelib.load_config(repo))


def test_prompt_contains_contract(repo):
    t = hx.load_task(repo / ".claude/harness/tasks/research-dv-wall.json", repo)
    p = hx.generator_prompt(t, zonelib.load_config(repo))
    assert "Hypothesis" in p and "reports/progress/research-dv-wall-ignition.md" in p
    assert all(a in p for a in t["acceptance"])


def test_generator_cmd_budget_split(repo, tmp_path):
    t = hx.load_task(repo / ".claude/harness/tasks/eng-cross-solver.json", repo)
    pf = tmp_path / "p.md"
    pf.write_text("hello")
    cmd = hx.generator_cmd(t, pf)
    assert cmd[-1] == "hello"
    assert cmd[cmd.index("--max-budget-usd") + 1] == str(4.0 / 3)
    assert "--permission-prompts" in cmd and hx.generator_env()["OGH_MODE"] == "unattended"
    assert cmd[cmd.index("--plugin-dir") + 1] == str(PLUGIN)


# ------------------------------------------------------------------ diff check (hook backstop)

def test_diff_check_catches_what_hook_missed(repo):
    (repo / "plant.py").write_text("# changed\n")          # Z2 ok
    (repo / "tests/test_oracle.py").write_text("# weakened\n")  # Z0 — e.g. via a python script the hook couldn't see
    (repo / "proposals/t").mkdir(parents=True)
    (repo / "proposals/t/g.patch").write_text("diff")      # new untracked Z3 ok
    zc = hx.check_diff_zones(repo, "HEAD")
    assert not zc["ok"]
    assert [v["file"] for v in zc["violations"]] == ["tests/test_oracle.py"]
    assert {t["file"] for t in zc["touched"]} == {"plant.py", "tests/test_oracle.py", "proposals/t/g.patch"}


def test_diff_check_clean(repo):
    (repo / "viewer/app.js").write_text("// new\n")
    assert hx.check_diff_zones(repo, "HEAD")["ok"]


# ------------------------------------------------------------------ gutter + evaluator parsing

def test_gutter_detector():
    g = hx.GutterDetector(limit=3)
    red = "FAILED tests/test_oracle.py::test_lcvx_tight - 0.12s\n1 failed"
    assert not g.observe(red, False)
    assert not g.observe(red.replace("0.12s", "0.30s"), False)  # timing noise ignored
    assert g.observe(red, False)
    g2 = hx.GutterDetector(limit=3)
    g2.observe(red, False); g2.observe("FAILED tests/x.py::other", False)  # noqa: E702
    assert not g2.observe(red, False)
    assert not g2.observe("", True) and g2.history == []


def test_parse_evaluator_output():
    v = {"verdict": "pass", "summary": "ok", "criteria": [], "issues": [], "ratchet_candidates": []}
    assert hx.parse_evaluator_output(json.dumps({"structured_output": v}))["verdict"] == "pass"
    assert hx.parse_evaluator_output(json.dumps({"result": "here: " + json.dumps(v)}))["verdict"] == "pass"
    assert hx.parse_evaluator_output(json.dumps({"result": "no json"}))["verdict"] == "needs_human"


def test_evaluator_short_circuits_on_violation(repo):
    (repo / "tests/test_oracle.py").write_text("# weakened\n")
    t = hx.load_task(repo / ".claude/harness/tasks/eng-cross-solver.json", repo)
    v = hx.run_evaluator(repo, t, "HEAD", {"oracle_fast": "", "oracle_full": "", "report": ""})
    assert v["verdict"] == "fail" and v["issues"][0]["where"] == "tests/test_oracle.py"
    assert (repo / ".harness/eng-cross-solver/diff.patch").exists()


# ------------------------------------------------------------------ hooks are inert in projects without the harness

def test_hooks_noop_without_config(tmp_path):
    e = dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp_path), OGH_MODE="unattended")
    for script, payload in (("zone_guard.py", edit(str(tmp_path / "x.py"))), ("stop_oracle.py", {})):
        r = subprocess.run([sys.executable, str(PLUGIN / "scripts" / script)], input=json.dumps(payload),
                           capture_output=True, text=True, env=e)
        assert r.returncode == 0 and r.stdout.strip() == ""


def test_evaluator_prompt_includes_domain(repo):
    (repo / ".claude/harness/evaluator-domain.md").write_text("Thrust lower bound is sacred.")
    p = hx.evaluator_system_prompt(repo)
    assert not p.startswith("---") and "Thrust lower bound is sacred." in p and "Hard fails" in p


# ------------------------------------------------------------------ init

def test_init_project_idempotent(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "CLAUDE.md").write_text("# Project\n")
    for _ in range(2):
        r = subprocess.run([sys.executable, str(PLUGIN / "harness/init_project.py")], cwd=tmp_path,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    assert (tmp_path / ".claude/harness/zones.json").exists()
    assert (tmp_path / "CLAUDE.md").read_text().count("## Agent rules (oracle-gated-harness)") == 1
    assert (tmp_path / ".gitignore").read_text().count(".harness/") == 1
    assert (tmp_path / ".claude/harness/tasks/examples/eng-example.json").exists()


def test_diff_check_ignores_build_artifacts(repo):
    (repo / "tests/__pycache__").mkdir()
    (repo / "tests/__pycache__/test_oracle.cpython-313.pyc").write_bytes(b"x")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache/v").write_text("x")
    (repo / "plant.py").write_text("# changed\n")
    zc = hx.check_diff_zones(repo, "HEAD")
    assert zc["ok"] and [t["file"] for t in zc["touched"]] == ["plant.py"]


def test_evidence_diff_includes_new_files(repo):
    (repo / "analysis").mkdir()
    (repo / "analysis/new.py").write_text("print(1)\n")
    t = hx.load_task(repo / ".claude/harness/tasks/eng-cross-solver.json", repo)
    d = hx.write_evidence(repo, t, "HEAD", {}, hx.check_diff_zones(repo, "HEAD"))
    patch = (d / "diff.patch").read_text()
    assert "analysis/new.py" in patch and ".harness" not in patch


# ------------------------------------------------------------------ append-only (new tests allowed, existing frozen)

def _append_only(repo):
    cfg = json.loads((repo / ".claude/harness/zones.json").read_text())
    cfg["append_only"] = ["tests/test_*.py"]
    (repo / ".claude/harness/zones.json").write_text(json.dumps(cfg))
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "append_only"],
                   cwd=repo, check=True)


def test_append_only_hook(repo):
    _append_only(repo)
    assert not denied(hook("zone_guard.py", {"tool_name": "Write", "tool_input": {
        "file_path": str(repo / "tests/test_new_regression.py"), "content": "def test_x(): pass"}}, repo))
    assert denied(hook("zone_guard.py", edit(str(repo / "tests/test_oracle.py")), repo))      # existing: frozen
    assert denied(hook("zone_guard.py", edit(str(repo / "tests/conftest.py")), repo))         # not a test_*.py
    assert denied(hook("zone_guard.py", bash("rm tests/test_oracle.py"), repo))
    assert denied(hook("zone_guard.py", bash("echo x > tests/test_brand_new.py"), repo))       # shell: diff check decides


def test_append_only_diff_check(repo):
    _append_only(repo)
    (repo / "tests/test_new_regression.py").write_text("def test_x(): pass\n")
    assert hx.check_diff_zones(repo, "HEAD")["ok"]
    (repo / "tests/test_oracle.py").write_text("# weakened\n")
    zc = hx.check_diff_zones(repo, "HEAD")
    assert [v["file"] for v in zc["violations"]] == ["tests/test_oracle.py"]


def test_append_only_delete_is_violation(repo):
    _append_only(repo)
    (repo / "tests/test_oracle.py").unlink()
    zc = hx.check_diff_zones(repo, "HEAD")
    assert not zc["ok"] and zc["violations"][0]["file"] == "tests/test_oracle.py"


def test_append_only_scope_validates(repo):
    _append_only(repo)
    t = json.loads((repo / ".claude/harness/tasks/eng-cross-solver.json").read_text())
    t["scope"]["allow"] = ["tests/*", "tools/*"]
    hx.validate_task(t, zonelib.load_config(repo))
    t["scope"]["allow"] = ["tests/conftest.py"]
    with pytest.raises(hx.TaskError, match="Z0"):
        hx.validate_task(t, zonelib.load_config(repo))


# ------------------------------------------------------------------ legacy night_run tasks

def test_legacy_conversion(repo, tmp_path):
    cfg = zonelib.load_config(repo)
    cfg["dirs"] = {"research_dir": "reports/"}
    leg = {"id": "multi-engine-research", "title": "T", "prompt": "Do X. Write reports/x.md", "scope": ["reports/"], "max_turns": 50}
    s = hx.validate_task(hx.from_legacy(leg, cfg), cfg)
    assert s["type"] == "research" and s["scope"]["allow"] == ["reports/*"] and s["budget"]["max_turns"] == 50
    assert s["research"]["deliverable_dir"] == "reports/"
    eng = hx.from_legacy({"id": "x_fix", "title": "T", "prompt": "p", "scope": ["tools/a.py"]}, cfg)
    assert eng["type"] == "engineering" and eng["id"] == "x-fix"
    assert "## Instructions\nDo X." in hx.generator_prompt(s, cfg)


def test_load_tasks_jsonl(repo):
    f = repo / "tasks.legacy.jsonl"
    f.write_text('# comment\n{"id": "aaa-1", "title": "A", "prompt": "p", "scope": ["plant.py"]}\n'
                 '{"id": "bbb-2", "title": "B", "prompt": "p", "scope": ["viewer/"]}\n')
    ids = [t["id"] for t in hx.load_tasks(f, repo)]
    assert ids == ["aaa-1", "bbb-2"]
    with pytest.raises(hx.TaskError, match="run_queue"):
        hx.load_task(f, repo)


def test_false_green_guard(repo):
    cfg = zonelib.load_config(repo)
    t = hx.load_task(repo / ".claude/harness/tasks/eng-cross-solver.json", repo)
    assert "no file changes" in hx.no_change_problem(t, hx.check_diff_zones(repo, "HEAD"), cfg)
    (repo / "reports/progress").mkdir(parents=True)
    (repo / "reports/progress/eng-cross-solver.md").write_text("did nothing")
    assert "no file changes" in hx.no_change_problem(t, hx.check_diff_zones(repo, "HEAD"), cfg)  # progress note alone isn't work
    (repo / "plant.py").write_text("# real change\n")
    assert hx.no_change_problem(t, hx.check_diff_zones(repo, "HEAD"), cfg) is None
    r = hx.load_task(repo / ".claude/harness/tasks/research-dv-wall.json", repo)
    assert "nothing under reports/research/" in hx.no_change_problem(r, hx.check_diff_zones(repo, "HEAD"), cfg)


def test_generator_prompt_has_preamble_and_oneshot(repo):
    (repo / ".claude/harness/agent-preamble.md").write_text("Always use `uv run`.")
    t = hx.load_task(repo / ".claude/harness/tasks/eng-cross-solver.json", repo)
    p = hx.generator_prompt(t, zonelib.load_config(repo), repo)
    assert "Always use `uv run`." in p and "ONE-SHOT" in p and "Do not git commit" in p
    pf = repo / "p.md"; pf.write_text(p)  # noqa: E702
    cmd = hx.generator_cmd(t, pf)
    assert "Bash" in cmd and not any(c.startswith("Bash(") for c in cmd)  # broad Bash, no prefix allowlist


def test_unattended_commands_isolate_user_settings(repo, tmp_path):
    t = hx.load_task(repo / ".claude/harness/tasks/eng-cross-solver.json", repo)
    pf = tmp_path / "p.md"; pf.write_text("x")  # noqa: E702
    for cmd in (hx.generator_cmd(t, pf), hx.evaluator_cmd(t, pf, pf)):
        assert cmd[cmd.index("--setting-sources") + 1] == "project,local"


def test_subprocesses_never_inherit_terminal_stdin():
    """claude -p blocks waiting on a terminal stdin; every subprocess must use DEVNULL."""
    import re as _re
    for f in ("harness/run_task.py", "harness/run_queue.py", "harness/harness_ext.py",
              "scripts/stop_oracle.py", "scripts/zonelib.py"):
        src = (PLUGIN / f).read_text()
        for call in _re.findall(r"subprocess\.run\((.{0,200})", src):
            assert "DEVNULL" in call, f"{f}: subprocess.run without stdin=DEVNULL: {call[:80]}"


# ------------------------------------------------------------------ live streaming + watch.py

def test_run_streaming_writes_live_and_extracts_result(tmp_path):
    fake = tmp_path / "fake.py"
    fake.write_text(
        "import json,sys,time\n"
        "print(json.dumps({'type':'system','subtype':'init','model':'m'}),flush=True)\n"
        "print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Bash','input':{'command':'ls'}}]}}),flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'success','result':'DONE REPORT','num_turns':2,'total_cost_usd':0.1}),flush=True)\n")
    log = tmp_path / "g.jsonl"
    rc, res = hx.run_streaming([sys.executable, str(fake)], tmp_path, dict(os.environ), 30, log)
    assert rc == 0 and res == "DONE REPORT"
    assert len(log.read_text().splitlines()) == 3


def test_run_streaming_timeout(tmp_path):
    fake = tmp_path / "slow.py"
    fake.write_text("import time,sys\nfor i in range(100):\n    print('{}', flush=True); time.sleep(0.1)\n")
    rc, _ = hx.run_streaming([sys.executable, str(fake)], tmp_path, dict(os.environ), 0.5, tmp_path / "g.jsonl")
    assert rc == 124 and '"timeout"' in (tmp_path / "g.jsonl").read_text()


def test_watch_render():
    sys.path.insert(0, str(PLUGIN / "harness"))
    import watch
    lines = watch.render({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Let me check the scenario"},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/guidance.py"}}]}}, False)
    assert "Let me check" in lines[0] and "guidance.py" in lines[1]
    blocked = watch.render({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True,
        "content": "[zone_guard] guidance.py is in zone Z1"}]}}, False)
    assert "BLOCKED" in blocked[0]
    assert "finished" in watch.render({"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": 1.5}, False)[0]


def test_generator_uses_stream_json(repo, tmp_path):
    t = hx.load_task(repo / ".claude/harness/tasks/eng-cross-solver.json", repo)
    pf = tmp_path / "p.md"; pf.write_text("x")  # noqa: E702
    cmd = hx.generator_cmd(t, pf)
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd


# ------------------------------------------------------------------ task threads + browser app

def _th():
    sys.path.insert(0, str(PLUGIN / "harness"))
    import threads
    return threads


def test_thread_create_post_read_and_inbox(tmp_path):
    th = _th()
    d = th.create(tmp_path, "20261004-120000-x", {"task": "x", "title": "X", "language": "ja"})
    th.post(d, "harness", "status", "hello")
    th.send_to_agent(d, "まず基準値を出して")
    evs = th.read_thread(d)
    assert [e["role"] for e in evs] == ["harness", "user"]
    assert th.take_unread(d) == ["まず基準値を出して"] and th.take_unread(d) == []
    th.send_to_agent(d, "二通目")
    assert th.take_unread(d) == ["二通目"]
    assert th.read_meta(d)["status"] == "running"


def test_stream_events_map_to_thread_posts():
    th = _th()
    cfg = {"language": "ja"}
    posts = th.stream_event_to_posts({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "基準値の計測を始めます"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "uv run python3 tools/nav_noise_sweep.py"}}]}}, cfg)
    assert posts[0][:2] == ("generator", "text") and posts[1][:2] == ("generator", "tool") and posts[1][3]["tool"] == "Bash"
    blk = th.stream_event_to_posts({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True,
        "content": [{"type": "text", "text": "[zone_guard] guidance.py is in zone Z1"}]}]}}, cfg)
    assert blk[0][:2] == ("harness", "blocked") and "止めました" in blk[0][2]
    done = th.stream_event_to_posts({"type": "result", "num_turns": 7, "total_cost_usd": 1.25}, cfg)
    assert done[0][0] == "harness" and "$1.25" in done[0][2]


def test_inbox_hook_delivers_and_posts(tmp_path):
    th = _th()
    d = th.create(tmp_path, "run-1", {"task": "t", "title": "T", "language": "ja"})
    env = dict(os.environ, OGH_THREAD_DIR=str(d))
    hook = [sys.executable, str(PLUGIN / "scripts/inbox_hook.py")]
    r = subprocess.run(hook, input="{}", capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == ""          # nothing queued: silent
    th.send_to_agent(d, "N=200は無理ならN=100でいい")
    r = subprocess.run(hook, input="{}", capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse" and "N=100でいい" in out["additionalContext"]
    assert th.read_thread(d)[-1]["kind"] == "delivered"
    r = subprocess.run(hook, input="{}", capture_output=True, text=True, env=env)
    assert r.stdout.strip() == ""                                  # delivered once only
    r = subprocess.run(hook, input="{}", capture_output=True, text=True,
                       env={k: v for k, v in env.items() if k != "OGH_THREAD_DIR"})
    assert r.returncode == 0 and r.stdout.strip() == ""          # inert outside harness runs


def test_hooks_json_registers_inbox():
    h = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    assert "inbox_hook.py" in h["PostToolUse"][0]["hooks"][0]["command"]


def test_run_streaming_abort(tmp_path):
    th = _th()
    d = th.create(tmp_path, "run-ab", {"task": "t", "title": "T"})
    fake = tmp_path / "slow.py"
    fake.write_text("import time\nfor i in range(600):\n    print('{}', flush=True); time.sleep(0.1)\n")
    import threading
    threading.Timer(1.0, lambda: (d / "ABORT").write_text("x")).start()
    rc, _ = hx.run_streaming([sys.executable, str(fake)], tmp_path, dict(os.environ), 60, tmp_path / "g.jsonl",
                             thread_dir=d, cfg={})
    assert rc == 130 and th.read_thread(d)[-1]["text"].startswith("Aborted")


def test_app_api(tmp_path):
    th = _th()
    sys.path.insert(0, str(PLUGIN / "harness"))
    import app
    from http.server import ThreadingHTTPServer
    import threading
    import urllib.request
    d = th.create(tmp_path, "20261004-140000-demo", {"task": "demo", "title": "Demo", "type": "research",
                                                      "branch": "agent/x/demo", "language": "ja"})
    th.post(d, "generator", "text", "始めます")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(tmp_path))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    get = lambda p: json.loads(urllib.request.urlopen(base + p).read())  # noqa: E731
    def post(p, body):
        req = urllib.request.Request(base + p, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())
    assert b"Task Threads" in urllib.request.urlopen(base + "/").read()
    lst = get("/api/threads")["threads"]
    assert lst[0]["run"] == "20261004-140000-demo" and lst[0]["events"] == 1
    t1 = get("/api/thread/20261004-140000-demo?after=0")
    assert t1["next"] == 1 and t1["events"][0]["text"] == "始めます"
    assert post("/api/thread/20261004-140000-demo/message", {"text": "了解、続けて"})["ok"]
    assert th.take_unread(d) == ["了解、続けて"]
    t2 = get("/api/thread/20261004-140000-demo?after=1")
    assert t2["events"][0]["role"] == "user"
    assert t2["events"][1]["role"] == "harness" and "届きます" in t2["events"][1]["text"]
    assert post("/api/thread/20261004-140000-demo/abort", {})["ok"] and (d / "ABORT").exists()
    th.write_meta(d, {"status": "done"})
    try:
        post("/api/thread/20261004-140000-demo/message", {"text": "late"})
        assert False, "should refuse"
    except urllib.error.HTTPError as e:
        assert e.code == 409
    try:
        get("/api/thread/..%2F..%2Fetc")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 404
    srv.shutdown()


def test_narration_prompt(repo):
    t = hx.load_task(repo / ".claude/harness/tasks/eng-cross-solver.json", repo)
    cfg = zonelib.load_config(repo)
    assert "Reporting to your human" not in hx.generator_prompt(t, cfg)
    cfg["narrate"] = {"language": "Japanese", "addressee": "Yohei"}
    p = hx.generator_prompt(t, cfg)
    assert "in Japanese, addressed to Yohei" in p

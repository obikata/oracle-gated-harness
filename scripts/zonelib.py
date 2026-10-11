"""Shared zone logic for oracle-gated-harness.

Used by the PreToolUse hook (zone_guard.py), the Stop hook (stop_oracle.py)
and the orchestrator post-run diff check (harness/harness_ext.py).
Pure stdlib so it runs in any interpreter the hook happens to get.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
from pathlib import Path

ZONE_ORDER = ("Z0", "Z1", "Z2", "Z3")

WHOLE_TREE = "<whole-tree>"
UNRESOLVED = "<unresolved>"   # a write target the shell would only know at run time ($VAR, $(...), cd into one)
UNATTENDED = "unattended"
INTERACTIVE = "interactive"


def project_dir(hook_input: dict | None = None) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    if hook_input and hook_input.get("cwd"):
        return Path(hook_input["cwd"]).resolve()
    return Path.cwd().resolve()


CONFIG_REL = Path(".claude") / "harness" / "zones.json"
MODE_ENV = "OGH_MODE"
ALLOW_Z0_ENV = "OGH_ALLOW_Z0"


def load_config(root: Path) -> dict:
    with open(root / CONFIG_REL, encoding="utf-8") as f:
        return json.load(f)


def mode() -> str:
    m = os.environ.get(MODE_ENV, INTERACTIVE).strip().lower()
    return UNATTENDED if m == UNATTENDED else INTERACTIVE


def rel_path(path: str, root: Path) -> str | None:
    """Repo-relative POSIX path, or None if the path is outside the repo."""
    if path in (WHOLE_TREE, UNRESOLVED):
        return path
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    try:
        # resolve() without strict so not-yet-existing files work
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def is_agent_scratch(path: str) -> bool:
    """Claude Code's per-session scratchpad, <tmp>/claude-<uid>/.../scratchpad/...

    Resolved first, so `..` or a symlink pointing back into the repo doesn't qualify.
    """
    import tempfile
    p = Path(path)
    if not p.is_absolute():
        return False
    try:
        rel = p.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0].startswith("claude-") and "scratchpad" in rel.parts[1:-1]


def classify(rel: str, cfg: dict) -> str:
    zones = cfg.get("zones", {})
    for z in ZONE_ORDER:
        for pat in zones.get(z, []):
            if fnmatch.fnmatchcase(rel, pat) or rel == pat.rstrip("/*"):
                return z
    return cfg.get("default_zone", "Z2")


def is_append_only(rel: str, cfg: dict) -> bool:
    return any(fnmatch.fnmatchcase(rel, p) for p in cfg.get("append_only", []))


def exists_at_head(rel: str, root: Path) -> bool:
    """True if `rel` is tracked at HEAD (i.e. it existed before this agent run)."""
    import subprocess
    r = subprocess.run(["git", "cat-file", "-e", f"HEAD:{rel}"], cwd=root, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
    return r.returncode == 0


def decide(rel: str | None, cfg: dict, run_mode: str, is_new: bool | None = None) -> tuple[bool, str]:
    """Return (allowed, reason) for a write to `rel` in `run_mode`.

    `is_new` (file absent at the base commit) unlocks `append_only` patterns:
    agents may ADD e.g. new test files but never modify or delete existing ones.
    """
    if rel is None:
        if run_mode == UNATTENDED:
            return False, "Write outside the repository is not allowed in unattended mode."
        return True, "outside repo (interactive)"
    if rel == UNRESOLVED:
        if run_mode == UNATTENDED:
            return False, ("Can't tell where this command writes: the target uses a shell variable, command "
                           "substitution or a cd into one. Use a literal path.")
        return True, "unresolved target (interactive)"
    if rel == WHOLE_TREE:
        if run_mode == UNATTENDED:
            return False, ("Whole-tree git operations (reset/stash/clean) are not allowed in "
                           "unattended mode; they can revert frozen files. Edit files explicitly.")
        return True, "whole-tree op (interactive)"
    z = classify(rel, cfg)
    if z == "Z0" and is_new and is_append_only(rel, cfg):
        return True, f"{rel} is a new file in an append-only area"
    if z == "Z0":
        if run_mode == INTERACTIVE and os.environ.get(ALLOW_Z0_ENV) == "1":
            return True, f"{rel} is Z0, allowed by OGH_ALLOW_Z0=1 (interactive)"
        return False, (
            f"{rel} is in zone Z0 (frozen: oracles, harness config, invariants). "
            "Agents never edit Z0. If you believe a Z0 change is needed, "
            "describe it in your final report instead."
            + (" You MAY add a NEW file matching: " + ", ".join(cfg.get("append_only", []))
               + " (existing ones are frozen)." if is_append_only(rel, cfg) else "")
        )
    if z == "Z1" and run_mode == UNATTENDED:
        return False, (
            f"{rel} is in zone Z1 (core, human-paired only). "
            "In unattended mode, write your change as a patch file under "
            "proposals/<task-id>/ (git diff format) and explain it in the report."
        )
    return True, f"{rel} is {z}"


# --- Bash write detection (best effort; the post-run diff check is the backstop)

_WRITE_CMDS = {"rm", "mv", "cp", "tee", "truncate", "touch", "chmod", "ln", "install", "dd", "patch"}
_GIT_WRITE = {"checkout", "restore", "reset", "rm", "mv", "apply", "stash", "clean"}
# Redirect targets that never represent a real write worth gating (discarding
# output, not persisting it). Without this, `2>/dev/null` -- an extremely
# common idiom -- reads as "write outside the repository" and gets denied.
_REDIRECT_NOOP = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/zero"}


_DEST_ONLY_CMDS = {"cp", "install", "ln"}   # only the last path is written
_ASSIGN = re.compile(r"^(?:export\s+)?([A-Za-z_]\w*)=(.*)$")
_VAR = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")


def _resolve(t: str, cwd: str | None, env: dict[str, str]) -> str:
    """Make a write target absolute where the shell would: ~, variables set earlier in the same
    command (or in the environment), and the effect of an earlier `cd`. Anything still dynamic
    ($(...), `...`, unknown variables) can't be judged, so it becomes UNRESOLVED."""
    t = t.strip("'\"")
    t = _VAR.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), t)
    if t.startswith("~"):
        t = os.path.expanduser(t)
    if "$" in t or "`" in t:
        return UNRESOLVED
    if os.path.isabs(t) or cwd is None:
        return t
    return UNRESOLVED if cwd == UNRESOLVED else os.path.join(cwd, t)


def bash_write_targets(command: str) -> list[str]:
    """Paths a shell command is likely to modify. Conservative, not complete: subshells and
    `bash -c` bodies aren't followed (the post-run diff check is the backstop inside the repo)."""
    targets: list[str] = []
    cwd: str | None = None                       # None = the project root the hook resolves against
    env = dict(os.environ)
    for segment in re.split(r"&&|\|\||;|\||\n", command.replace("\\\n", " ")):
        for m in re.finditer(r"(?<![<>&\d])\d?>>?\s*([^\s;&|<>]+)", segment):
            t = m.group(1)
            if t.strip("'\"") not in _REDIRECT_NOOP:
                targets.append(_resolve(t, cwd, env))
        try:
            toks = shlex.split(segment, posix=True)
        except ValueError:
            toks = segment.split()
        if not toks:
            continue
        if toks[0] == "export":
            toks = toks[1:]
        while toks and (_ASSIGN.match(toks[0]) or toks[0] == "sudo"):
            m = _ASSIGN.match(toks[0])
            if m:
                env[m.group(1)] = _resolve(m.group(2), cwd, env) if m.group(2).startswith(("~", "$")) else m.group(2)
            toks = toks[1:]
        if not toks:
            continue
        cmd, args = os.path.basename(toks[0]), toks[1:]
        paths = [a for a in args if not a.startswith("-")]
        res = lambda ps: [_resolve(p, cwd, env) for p in ps]  # noqa: E731
        if cmd in ("cd", "pushd"):
            cwd = _resolve(paths[0] if paths else "~", cwd, env)
        elif cmd in _DEST_ONLY_CMDS:
            targets.extend(res(paths[-1:]))
        elif cmd in _WRITE_CMDS:
            targets.extend(res(paths))
        elif cmd == "sed" and any(a.startswith("-i") or a == "--in-place" for a in args):
            targets.extend(res(paths[1:] if paths else []))
        elif cmd == "git" and args and args[0] in _GIT_WRITE:
            targets.extend(res(p for p in paths[1:] if p != "--"))
            if args[0] in {"reset", "stash", "clean"} and len(paths) <= 1:
                targets.append(WHOLE_TREE)  # whole-tree operation
        elif cmd in {"python", "python3"} and "-c" in args:
            # can't see inside; flag protected names mentioned literally
            body = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
            if re.search(r"open\(|write_text|write_bytes|unlink|rename|remove", body):
                targets.extend(res(re.findall(r"['\"]([^'\"]+\.\w+)['\"]", body)))
    return targets

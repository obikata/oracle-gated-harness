#!/usr/bin/env python3
"""Set up a project for oracle-gated-harness. Run from the project root.

    python <plugin>/harness/init_project.py

Creates .claude/harness/{zones.json, evaluator-domain.md, tasks/}, docs/ratchet-log.md,
appends the agent rules to CLAUDE.md and the evidence dir to .gitignore.
Never overwrites existing files.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
T = PLUGIN_ROOT / "templates"
MARK = "## Agent rules (oracle-gated-harness)"


def copy_if_absent(src: Path, dst: Path, log: list[str]) -> None:
    if dst.exists():
        log.append(f"keep   {dst}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    log.append(f"create {dst}")


def append_once(path: Path, text: str, marker: str, log: list[str]) -> None:
    cur = path.read_text(encoding="utf-8") if path.exists() else ""
    if marker in cur:
        log.append(f"keep   {path}")
        return
    path.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + text, encoding="utf-8")
    log.append(f"update {path}")


def main() -> None:
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                               text=True, check=True).stdout.strip())
    log: list[str] = []
    h = root / ".claude" / "harness"
    copy_if_absent(T / "zones.json", h / "zones.json", log)
    copy_if_absent(T / "evaluator-domain.md", h / "evaluator-domain.md", log)
    (h / "tasks").mkdir(parents=True, exist_ok=True)
    for ex in (T / "tasks").glob("*.json"):
        copy_if_absent(ex, h / "tasks" / "examples" / ex.name, log)
    copy_if_absent(T / "ratchet-log.md", root / "docs" / "ratchet-log.md", log)
    append_once(root / "CLAUDE.md", (T / "CLAUDE.section.md").read_text(encoding="utf-8"), MARK, log)
    append_once(root / ".gitignore", "\n# oracle-gated-harness\n.harness/\n__pycache__/\n.pytest_cache/\n", ".harness/", log)
    print("\n".join(log))
    print("\nNext: edit .claude/harness/zones.json for your layout, then run the checks in the README.")


if __name__ == "__main__":
    main()

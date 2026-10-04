# Methodology

How autonomous agents work in a project using oracle-gated-harness, and why. Humans read this to change the
system; agents read it to understand the rules they are held to.
The rules here are **enforced by configuration**, not by this text:
plugin hooks, project config in `.claude/harness/`, gates in `harness/run_task.py`. This file explains them.

## 1. Principles

1. **The generator never judges itself.** Work is done by one agent, judged by
   deterministic oracles and a separate read-only Evaluator.
2. **Gates the agent cannot modify.** Tests, the harness and safety invariants are
   frozen (Z0). An agent that could edit its own grader would eventually do so.
3. **Fresh context per iteration.** State lives in files and git, never in a long
   conversation. Each iteration reads the handoff file, works, writes it back.
4. **Evidence over claims.** A report is a claim. A log, CSV or figure is evidence.
5. **Every failure becomes a rule (ratchet).** A failure seen in triage is turned into
   a test, a hook or a CLAUDE.md rule, and logged in `docs/ratchet-log.md`.
6. **Never merge unattended.** Agents commit to `agent/<stamp>/<task-id>` branches.
   A human merges.

## 2. Layers

| # | Layer | Where it lives |
|---|---|---|
| 1 | Intent: task specs with testable acceptance criteria | `.claude/harness/tasks/*.json`, schema `schemas/task.schema.json` |
| 2 | Autonomy zones | `.claude/harness/zones.json`, hook `scripts/zone_guard.py` |
| 3 | Execution (generator, fresh context, budgets) | `harness/harness_ext.py: generator_*` |
| 4 | Verification pyramid | fast oracle (Stop hook) → full oracle → zone diff check → Evaluator |
| 5 | Evaluator | `agents/evaluator.md` + project `evaluator-domain.md` |
| 6 | Ratchet | `docs/ratchet-log.md` |
| 7 | Observability | `reports/agent-runs/*.json`, `.harness/<task>/` on each branch |

## 3. Autonomy zones

| Zone | Meaning | Unattended | Interactive |
|---|---|---|---|
| **Z0** frozen | oracle tests, harness config, CLAUDE.md, ratchet log | denied | denied (unless `OGH_ALLOW_Z0=1`) |
| **Z1** core | the code whose correctness everything rests on | denied → write `proposals/<task-id>/*.patch` | allowed (human paired) |
| **Z2** reviewed | supporting code, analysis, new modules | allowed, Evaluator reviews | allowed |
| **Z3** free | docs, reports, proposals, scratch, viewers | allowed | allowed |

**Append-only areas** (`append_only` in zones.json, e.g. `tests/test_*.py`): agents may
add new files there (new regression tests are how the ratchet grows) but any
modification or deletion of an existing file is a violation.

Mode comes from `OGH_MODE` (`unattended` is set by the orchestrator; default
is `interactive`). Enforcement is layered:

- **PreToolUse hook** blocks Edit/Write/MultiEdit/NotebookEdit and detectable Bash
  writes (redirects, `rm`, `mv`, `cp`, `sed -i`, `tee`, `git checkout/restore/reset/stash`…)
  before they happen. Fails closed in unattended mode.
- **Post-run diff check** (`check_diff_zones`) classifies every file changed since
  base, committed or not. This catches writes the hook cannot see (e.g. a Python
  script that writes a file). Any violation fails the task without calling the Evaluator.

## 4. Task types

**Engineering** — refactors, new features, tooling. Done = acceptance criteria met,
full oracle green, no zone violation, Evaluator `pass`.

**Research** — questions about the problem. The deliverable is
a directory under `reports/research/<name>/` with data, figures and `report.md`.
Done = every claim is backed by a saved file. **A well-evidenced negative result
passes.** Research tasks never change Z1; ideas for the core go to `proposals/`.

Writing good acceptance criteria: observable and checkable by someone who did not
do the work. "Faster" is not a criterion. "p95 solve time < 50 ms over 200 cycles,
printed by `python -m analysis.timing`" is.

## 5. Run lifecycle

```
task.json ──validate──▶ worktree + branch agent/<stamp>/<id>
   │
   ├─ iteration i (fresh `claude -p`, OGH_MODE=unattended)
   │     reads <progress_dir>/<id>.md → works → updates it
   │     Stop hook: fast oracle red → told to fix, once
   │     orchestrator: fast oracle; green → exit loop
   │     same failure 3× in a row → stop ("gutter")
   │
   ├─ full oracle
   ├─ zone diff check ── violation ──▶ verdict fail (no Evaluator call)
   ├─ Evaluator (read-only, JSON verdict) on .harness/<id>/ evidence
   └─ commit to branch (incl. .harness/<id>/), summary to <runs_dir>
```

Budgets per task: `max_iterations`, `max_turns` per iteration, `max_budget_usd`
split evenly across iterations.

## 6. Morning triage (human)

1. Read `reports/agent-runs/` newest first. Sort: `pass` → review & merge;
   `needs_human` → read; `fail` → look at *why*.
2. For each failure, ask: **would a guard have caught this earlier?**
   If yes, add the guard and a line to `docs/ratchet-log.md`. The Evaluator suggests
   candidates in `ratchet_candidates`.
3. Review `proposals/` for Z1 ideas. Apply by hand in an interactive session if good.
4. Queue the next tasks.

## 7. Changing the system

Project harness config is Z0 and the plugin lives outside the repo, so agents can't
change either. Humans change config interactively with `OGH_ALLOW_Z0=1`, change the
plugin in its own repo (`python -m pytest tests -q`), and add a ratchet-log entry
when the change was caused by a failure.

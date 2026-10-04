
## Agent rules (oracle-gated-harness)

Enforced by hooks; working around them wastes your budget.

- **Zones** are defined in `.claude/harness/zones.json`. Z0 (oracles, harness config,
  this file) is never edited. Z1 in unattended mode: write a `git diff`-format patch
  to the proposals dir under your task id and explain it; do not apply it.
- **Never weaken a check** to get green: no loosened tolerances, skipped tests,
  widened bounds, or broad try/except. If the oracle is red and you can't fix the
  cause, stop and say why. That is a useful outcome.
- **Handoff.** Read your task's progress file first; update it before stopping.
- **Evidence.** Every number in a report comes from a file you saved.
- **Scope.** Edit only what the task's `scope.allow` names. Report unrelated problems instead of fixing them.
- **Whole-tree git ops** (`reset`, `stash`, `clean`) are blocked unattended.

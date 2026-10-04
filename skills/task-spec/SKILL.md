---
name: task-spec
description: Write or review a task spec (.claude/harness/tasks/*.json) for an unattended oracle-gated run. Use when the user wants to queue agent work, or asks whether a task is ready to run overnight.
---

A task spec is a contract the Evaluator will hold the generator to. Schema: `schemas/task.schema.json` in the plugin.

1. Pick the type. **engineering** = code change, done when acceptance is met and oracles are green. **research** = a question; the deliverable is data + figures + report under the research dir. A clear negative result passes.
2. Write acceptance criteria an outsider can check: a command to run, a file that must exist, a number with a threshold. Reject "faster", "cleaner", "more robust".
3. Keep `scope.allow` minimal. Never Z0. Z1 work is phrased as "write a proposal patch".
4. Budget small first: 1-2 iterations, 25-60 turns. Raise only after a run shows it is needed.
5. Validate: `python <plugin>/harness/run_task.py --task <file> --dry-run`.

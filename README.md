# oracle-gated-harness

Run Claude Code **unattended** on code where being subtly wrong is worse than being slow.

- **Autonomy zones** (Z0 frozen → Z3 free), enforced by a PreToolUse hook *and* a post-run diff check;
  `append_only` areas let agents add new tests but never modify existing ones
- **Oracle gates**: a Stop hook that won't let the agent finish red; fast + full oracle after each run
- **Read-only Evaluator** that judges evidence, not the generator's claims, with project domain rules
- **Research tasks** whose deliverable is evidence (data, figures, report), where a clean negative result passes
- **Ratchet**: every failure seen in triage becomes a permanent guard, logged
- **False-green guard**: a no-op (or a research task with no deliverable) fails, even with green oracles
- Runs legacy `{id,title,prompt,scope,max_turns}` `.jsonl` backlogs unchanged; global time budget + STOP file
- Never merges. Every run lands on its own `agent/<stamp>/<task-id>` branch with its evidence

Stdlib Python only. Requires Claude Code v2.1.259+ (`--permission-prompts none`).

## Quick start

```bash
# 1. get the plugin (outside your project)
git clone git@github.com:obikata/oracle-gated-harness.git ~/dev/oracle-gated-harness
OGH=~/dev/oracle-gated-harness

# 2. set up your project
cd ~/dev/your-project
python $OGH/harness/init_project.py
$EDITOR .claude/harness/zones.json        # map your files to Z0..Z3, set oracle commands

# 3. work interactively with the hooks on
claude --plugin-dir $OGH

# 4. run one task unattended
python $OGH/harness/run_task.py --task .claude/harness/tasks/examples/eng-example.json --dry-run
python $OGH/harness/run_task.py --task .claude/harness/tasks/examples/eng-example.json
```

Results: branch `agent/<stamp>/<task-id>`, evidence in `.harness/<task-id>/` on that branch,
summary in `reports/agent-runs/`.

```bash
# 5. overnight: run every task in .claude/harness/tasks/ and write a triage report
python $OGH/harness/run_queue.py --dry-run
python $OGH/harness/run_queue.py        # -> reports/agent-runs/<stamp>-triage.md
```

## Layout

```
.claude-plugin/plugin.json   hooks/hooks.json       agents/evaluator.md
scripts/   zonelib.py, zone_guard.py (PreToolUse), stop_oracle.py (Stop)
harness/   harness_ext.py (building blocks), run_task.py (one task), run_queue.py (overnight queue + triage), init_project.py
skills/    task-spec, triage, ratchet
schemas/   task + evaluator verdict
templates/ project config copied by init_project.py
docs/methodology.md
```

## Modes

`OGH_MODE=unattended` is set by `run_task.py`. Interactive sessions (default) may edit Z1;
Z0 needs `OGH_ALLOW_Z0=1`. The hooks do nothing in repos without `.claude/harness/zones.json`.

## Tests

```bash
python -m pytest tests -q
```

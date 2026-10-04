---
name: ratchet
description: Turn an observed agent failure into a permanent guard (test, hook, CLAUDE.md rule, zone or evaluator rule) and log it. Use after triage finds a failure that could have been caught earlier.
---

Pick the cheapest guard that would have caught the failure *deterministically*:
1. **test** – the failure is a wrong result an oracle could detect. Preferred.
2. **zone** – the agent touched something it should not; tighten `.claude/harness/zones.json`.
3. **evaluator** – a review-level problem; add a line to `.claude/harness/evaluator-domain.md`.
4. **hook / orchestrator** – a process failure (looping, wrong command). Change the plugin.
5. **claude_md_rule** – last resort; instructions are the weakest guard.

Then append one row to `docs/ratchet-log.md`: date, failure seen, guard added, type.
These files are Z0: the human applies the change interactively with `OGH_ALLOW_Z0=1`.

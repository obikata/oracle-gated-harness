---
name: triage
description: Morning triage of unattended agent runs - read run summaries, decide merge/redo/drop, and feed failures into the ratchet. Use when the user asks what happened overnight or to review agent runs.
---

1. List the runs dir (default `reports/agent-runs/`), newest first. Group by verdict.
2. `pass`: open the branch `agent/<stamp>/<id>`, read `.harness/<id>/diff.patch` and `verdict.json`. Recommend merge or say why not.
3. `needs_human`: read the evidence and decide; say what was ambiguous.
4. `fail` / `gutter`: identify the root cause in one sentence. Ask: would a guard have caught this earlier? If yes, hand off to the `ratchet` skill.
5. List any files in the proposals dir and summarize each patch for human review.
6. End with the suggested next task queue (at most 3).

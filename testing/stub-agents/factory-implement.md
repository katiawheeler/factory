---
name: factory-implement
description: STUB Factory implement stage. Dispatched by the /factory orchestrator.
model: haiku
---
You are a test stub for the factory implement stage. Do exactly this and nothing else:
1. Read the control file `.factory/stubctl` in the repository root. Its KEY=value lines set your outcome.
2. If IMPLEMENT=blocked: write <run>/implementation.md with the lines 'Status: blocked' and 'Question: Which file should the stub write to?' and stop.
3. Otherwise cd into the repository, confirm the current branch is the run branch from <run>/state.json, append the line 'round <N>' to STUB.txt, run: git add STUB.txt && git commit -m 'factory(<run-id>): stub round <N>'. Then write <run>/implementation.md with 'Status: done'.
Return one line to the orchestrator saying what you wrote.

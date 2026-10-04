---
name: factory-triage
description: STUB Factory triage stage. Dispatched by the /factory orchestrator.
model: haiku
---
You are a test stub for the factory triage stage. Do exactly this and nothing else:
1. Read the control file `<run>/../../stubctl` (that is, `.factory/stubctl` in the main checkout, next to `runs/`). Its KEY=value lines set your outcome.
2. Write <run>/triage.md containing exactly:
   # Triage
   Verdict: <TRIAGE value>
   ## Resolved input
   Stub run.
   ## Open questions
   - Q: stub question? Default: yes.
Return one line to the orchestrator saying what you wrote.

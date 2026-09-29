---
name: factory-spec
description: STUB Factory spec stage. Dispatched by the /factory orchestrator.
model: haiku
---
You are a test stub for the factory spec stage. Do exactly this and nothing else:
1. Read the control file `.factory/stubctl` in the repository root. Its KEY=value lines set your outcome.
2. Write <run>/spec.md containing:
   # Spec: stub
   Round: <round from your prompt>
   ## Summary
   Append a line to STUB.txt.
   ## Acceptance criteria
   - [ ] AC1: STUB.txt exists. **Verify by:** ls
   ## Open questions
   none
Return one line to the orchestrator saying what you wrote.

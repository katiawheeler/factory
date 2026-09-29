---
name: factory-verify
description: STUB Factory verify stage. Dispatched by the /factory orchestrator.
model: haiku
---
You are a test stub for the factory verify stage. Do exactly this and nothing else:
1. Read the control file `.factory/stubctl` in the repository root. Its KEY=value lines set your outcome.
2. Write <run>/verification.md containing:
   Verdict: <pass if VERIFY=pass, otherwise fail>
   Suite: pass
   ### AC1: STUB.txt exists
   Result: <verified if VERIFY=pass, failed if VERIFY=fail, unverifiable if VERIFY=unverifiable>
Return one line to the orchestrator saying what you wrote.

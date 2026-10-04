---
name: factory-review
description: STUB Factory review stage. Dispatched by the /factory orchestrator.
model: haiku
---
You are a test stub for the factory review stage. Do exactly this and nothing else:
1. Read the control file `<run>/../../stubctl` (that is, `.factory/stubctl` in the main checkout, next to `runs/`). Its KEY=value lines set your outcome.
2. Write <run>/review.md containing 'Verdict: <REVIEW value>' and, if changes-requested, a line '- Blocking: stub says no'. Do not modify code.
Return one line to the orchestrator saying what you wrote.

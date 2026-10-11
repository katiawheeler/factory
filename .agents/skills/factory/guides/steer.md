# Checkpoint 3: steer (HUMAN)

_Part of the factory orchestrator. `SKILL.md` in this skill's base directory defines `STATE`, `<id>`, the run folder, and the rules, which all apply here._

Steering is the human taking the wheel mid-run. It happens when they choose **Steer**, when a stage reports `blocked`, or when they interrupt you.

1. Show where the run is: the stage, the last 3 history entries, and the latest output file. For a `blocked` run, lead with the agent's question (the last history note) and the stage that raised it (that entry's `from`).
2. Talk it through with the human directly. They might answer the agent's question, edit files, change direction, or ask you to make a change yourself.
3. Record the outcome with `feedback <id> "Steer: <stage>"`, so later agents see it.
4. Agree on the next stage with the human, `advance` to it, and continue the loop. If you changed code yourself, the next stage is `review`, never `verify`, so your commits get reviewed like any other. If the change departs from `spec.md`, say so in the Steer note. Later agents read it alongside the spec.

**A blocked run resumed with an answer** (`/factory <run-id> <answer>` while the stage is `blocked`): `abort` and `pause` work as usual. Any other text answers the agent's question. Log it under `Steer: <stage that blocked>`, `advance` back to that stage with the note `"unblocked"`, and continue. Don't re-ask. A run blocked by land is the exception: follow its `needs-human` outcome under **land**.

If the last note starts with `steer requested`, the answer is the human's steer instead. Log it under `Steer: <checkpoint it came from>`, then do what it asks as in step 4, and `advance` to the stage it calls for. If it names no stage, go back to that checkpoint.

**Interactive GUI:** ask the blocking question or requested steering in the native question UI. For Codex, prefer `request_user_input_async` with free-text input; for Claude Code, use `AskUserQuestion` with Other for free text. If the tool is unavailable, ask in chat and wait. Record the actual answer in `feedback.md` before advancing. For a headless session, show step 1 and stop, ending with `<factory> <run-id> <answer to the question>` and `<factory> <run-id> abort`. When `steer` was the answer at a checkpoint, first `advance <id> blocked "steer requested at checkpoint-N"`, so the next call knows the answer is a steer and not checkpoint feedback.

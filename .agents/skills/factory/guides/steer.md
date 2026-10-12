# Checkpoint 3: steer (HUMAN)

_Part of the factory orchestrator. `SKILL.md` in this skill's base directory defines `STATE`, `<id>`, the run folder, and the rules, which all apply here._

Steering is the human taking the wheel mid-run. `next` sends you here with an `ask` of kind `blocked` (a stage asked a question) or `steer` (the human chose **Steer** at a checkpoint, or interrupted you and you ran `STATE answer <id> steer`).

1. Show where the run is: for `blocked`, lead with the `question` and the stage that `raised_by` it, and point to its `output` file. For `steer`, say which stop it came from (the `note`) and show the last few history entries (`STATE show <id>`).
2. Talk it through with the human directly. They might answer the question, edit files, change direction, or ask you to make a change yourself.
3. Record the outcome and move on in one step: `STATE answer <id> [--to <stage>] <what was decided>` (or `-` with the text on stdin). It logs the text under `Steer: <stage>` in `feedback.md`, so later agents see it, and prints the next instruction.
   - Without `--to`, a blocked stage goes back to the stage that blocked, and a steer goes back to the checkpoint it came from.
   - With `--to`, it goes where you and the human agreed. If you changed code yourself, use `--to review`, never `verify`, so your commits get reviewed like any other. If the change departs from `spec.md`, say so in the text: later agents read it alongside the spec.
   - A question from land needs `--to`: `implement` for a change, `spec` if the requirements change, or `land` to keep waiting.

**Interactive GUI:** ask the blocking question or the requested steering in the native question UI. For Codex, prefer `request_user_input_async` with free-text input; for Claude Code, use `AskUserQuestion` with Other for free text. If the tool is unavailable, ask in chat and wait. Apply the actual answer with `STATE answer`. For a headless session, show step 1 and stop, ending with `<factory> <run-id> <answer to the question>` and `<factory> <run-id> abort`. A headless steer at a checkpoint parks the run at `blocked` (`STATE answer <id> steer` does this), so the next call knows the answer is a steer and not checkpoint feedback.

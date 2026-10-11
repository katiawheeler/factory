# Signals after merge

_Part of the factory orchestrator. `SKILL.md` in this skill's base directory defines `STATE`, `<id>`, the run folder, and the rules, which all apply here._

| Command | What it does |
|---|---|
| `STATE signals [<id>] [--days N]` | For merged runs (by default, merged in the last 14 days): a revert on the base branch, failing checks on the merge commit, and lines from the repo's signal command (`git config factory.signals`). Records the result in `state.json` and prints JSON with `regression` true or false per run. |

`/factory signals` runs `STATE signals`, which checks merged runs from the last 14 days (or one run: `STATE signals <id>`). For each run with `regression: true`, present:
- the PR
- the revert commits, failing checks on the merge commit, and custom signal lines
- the notes

Then offer a follow-up run. In an interactive session, ask with the question UI; for each one the human accepts, start it as `follow-up <run-id> <details>` from Step 0 in `SKILL.md`. Headless, print `<factory> follow-up <run-id> <details>` for each one and stop. A failing check on the merge commit can be unrelated; the follow-up's triage decides. Runs without a regression get one summary line. Never revert anything yourself.

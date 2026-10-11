# Finish

_Part of the factory orchestrator. `SKILL.md` in this skill's base directory defines `STATE`, `<id>`, the run folder, and the rules, which all apply here._

Read this when a run ends: `merged`, `closed`, or `aborted` at any stage, or on resuming an ended run that has no retro (`get <id> learned_at` fails).

| Command | What it does |
|---|---|
| `STATE learn <id>` | Merges `retro.md`'s **Learnings** into `.factory/learnings.md`: adds new lessons with their run ID, skips duplicates, removes retired ones. Once per finished run. |
| `STATE release <id>` | Removes a merged, closed, done, or aborted run's worktree and keeps its branch. Refuses if the worktree has uncommitted changes. |

First the retro, so the next run benefits. Skip it only for a run aborted before implement with nothing in `feedback.md`. Dispatch the retro stage (its round is 1). Then run `STATE learn <id>` and pass on its warning if it prints one. Show the human the retro's **Proposed repo changes**, one line each, with the command that would start each as a run (`<factory> <proposal>`); never start them yourself. If `learn` fails, say why and carry on.

Then leave the human where they started. With a worktree, `STATE release <id>`, run from the human's checkout rather than from inside the worktree it removes (the branch is pushed, so nothing is lost). For an in-place run, `git switch <base_branch>`, so the next run doesn't branch off this one. If the harness subscribed to PR events, unsubscribe. Print the run ID, the PR link, the outcome, and `STATE stats <id>`.

The same applies to `abort` at any stage: the retro, then `release` the worktree, or for an in-place run switch back to `base_branch`, and leave the run branch in place. If `release` refuses because the worktree has uncommitted changes, leave it and tell the human its path.

The factory never merges or deploys. Merging is the human's final act; land only follows the PR there.

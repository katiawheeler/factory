# Measuring and learning

How the factory reports on itself, learns from each run, and checks merged work for regressions.

## The run ledger

`state.py stats` reports how the factory is doing across runs:

- outcomes and merge rate
- how often the spec and the ship decision are approved without a revision
- loop caps hit
- PR fix rounds per PR
- median time to PR and to merge, and how long runs wait on humans

`state.py stats <run-id>` breaks one run down by stage. When the host reports a subagent's token usage, the orchestrator records it with `state.py usage`, and `stats` adds tokens and cost. `--json` gives machine-readable output.

## Learning from each run

When a run ends (merged, closed, or aborted after work began), a retro stage looks for where it lost time or needed a human to correct it: spec revisions, send-backs, review rounds, failed verification, PR fix rounds, steers, and problems after merge. It records the causes that will recur as short lessons, grouped as Commands, Conventions, Pitfalls, Review, Verification, and Process. `state.py learn` folds them into `.factory/learnings.md` with the run they came from, skips duplicates, and removes lessons a later retro retires. Every stage reads that file before it starts.

Lessons come only from human feedback, CI results, and the code, never from comments by people without write access. Stages treat them as background: the spec and feedback still win. The file is local to your checkout and you can edit it freely. A lesson worth sharing with your team belongs in the repository; the retro proposes those changes, such as a `CLAUDE.md` rule or a missing doc, as one-line requests you can start as factory runs.

## After merge: signals

`<factory> signals` checks runs merged in the last 14 days for signs the change hurt:

- a revert on the base branch, whether a `git revert` of the merge commit or GitHub's **Revert** button
- failing checks on the merge commit
- anything your own signal command prints

The signal command can query an error tracker, logs, or a deploy dashboard. It receives the run as JSON on stdin, plus `FACTORY_RUN_ID` and `FACTORY_MERGE_SHA`; each line it prints is a signal:

```sh
git config factory.signals './scripts/errors-since-deploy.sh'
```

For each regression, the factory offers a follow-up run linked to the original (`--parent`), which starts at triage like any other. It never reverts anything itself. `state.py stats` counts regressions and follow-ups across runs.

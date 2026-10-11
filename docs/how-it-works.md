# How it works

## The factory loop

![A ticket, URL, or prompt enters a loop of Triage, Spec, Implement, Review, Verify, Ship, and Land, with a human approving the spec and deciding to ship, and Land following the PR until it merges](factory-loop.svg)

Agents write their outputs to `.factory/runs/<run-id>/`. Each run works in its own Git worktree, so your checkout stays on your branch with your uncommitted changes untouched. At the two approval points, the host presents those outputs to the human and saves the answer in `feedback.md` for the next stage. Review or verification failures return to implementation, then pass through review again. A human can steer when work is blocked. Shipping opens a PR and posts the run report on it. Landing then follows the PR: red CI, merge conflicts, and reviewers' comments go back through implement, review, and verify, and the fix is pushed to the same PR, until the PR is merged or closed.

That is the default. A repo, or the person starting a run, can opt in to skipping triage, spec, review, verify, and either approval point, up to running unattended from input to PR; see [Skipping stages for automation](automation.md#skipping-stages-for-automation). Implement, ship, and land always run.

| Stage | Output | Human decision | Skippable with `--skip` or `factory.skip` |
|---|---|---|---|
| Triage | `triage.md` with resolved input, risks, questions, and verdict | Clarify or override if needed | `triage` |
| Spec | `spec.md` with testable acceptance criteria, open questions, and diagrams where they help | Checkpoint 1: approve or request a revision; the human may edit `spec.md` directly | `spec` (skips checkpoint 1 too), or just `checkpoint-1` |
| Implement | Code commits and `implementation.md` | Steer if blocked | Never |
| Review | `review.md` with blocking findings and notes | Findings are shown at checkpoint 2 | `review` |
| Verify | `verification.md` and `evidence/`, with a suite result and one result per criterion | Checkpoint 2: inspect evidence, ship, or send work back | `verify`, and `checkpoint-2` for the approval |
| Ship | Pushed branch, PR, and a PR comment with the spec and evidence | Only after the human says **Ship**, unless `checkpoint-2` is skipped | Never |
| Land | `land.md`: the PR's state, CI failures, and review comments sorted into fixes, questions, and things not acted on | Reviewers review and merge; questions come to the human | Never |
| Retro | `retro.md`, folded into `.factory/learnings.md` | Proposed repo changes are offered as new runs | Never |

Review gets three consecutive attempts; verification gets three attempts between human checkpoints; PR fix rounds get three between human checkpoints. A cap sends the run to checkpoint 2 with the problem stated. Failed verification returns to implementation, then review, then verification. An environment limitation can go straight to checkpoint 2 with the affected criteria named.

## Run state

`state.json` and its transitions are managed by [`state.py`](../.agents/skills/factory/scripts/state.py), which requires Python 3. It locks a run while changing it, so a scheduled resume, a GitHub answer, and a PR event arriving at once don't overwrite each other's changes (on Windows, which lacks the lock, keep one session per run). If shipping cannot push or open a PR, the run remains blocked for human steering and doesn't move on to land.

## Worktrees

`state.py new` creates the run branch from your current branch and checks it out in `.factory/worktrees/<run-id>/`. Agents work only there, so you can keep editing, leave your tree dirty, and start more runs while one waits at a checkpoint. A run starts from the last commit on your branch; uncommitted changes aren't included. The worktree stays while the PR is open, so PR fix rounds can work in it, and is removed when the PR is merged or closed or the run is aborted. The run branch is kept.

A fresh worktree has no installed dependencies, so the implement and verify stages install them the way the repo documents. Most tools skip dot-directories such as `.factory/`; if one of yours scans it, exclude `.factory/` there. To work in your checkout instead, ask for an in-place run (`state.py new --in-place`), which needs a clean tree and switches your checkout to the run branch.

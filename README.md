# factory

A software factory workflow for any coding agent that can read the skill and run Git and Python commands. Give it a ticket, URL, or prompt; it triages the request, writes a spec, implements it on a branch, reviews the diff, verifies the acceptance criteria, opens a PR after human approval (or without it, if the repo opts in to full automation), and then carries that PR through CI failures and review comments until it merges or closes. Each finished run ends with a retro that teaches the next run, and merged runs can be checked for reverts and failures. It never merges or deploys; merging stays a human's call. Inspired by Warp's [guide to cloud software factories](https://www.warp.dev/blog/a-guide-to-cloud-software-factories-for-engineering-leaders).

```text
<factory> <ticket | URL | prompt>
```

Use your agent's syntax for invoking an installed skill. The factory can dispatch stage subagents when the host supports them; otherwise the orchestrator follows the same stage instructions itself.

## The factory loop

![A ticket, URL, or prompt enters a loop of Triage, Spec, Implement, Review, Verify, Ship, and Land, with a human approving the spec and deciding to ship, and Land following the PR until it merges](docs/factory-loop.svg)

Agents write their outputs to `.factory/runs/<run-id>/`. Each run works in its own Git worktree, so your checkout stays on your branch with your uncommitted changes untouched. At the two approval points, the host presents those outputs to the human and saves the answer in `feedback.md` for the next stage. Review or verification failures return to implementation, then pass through review again. A human can steer when work is blocked. Shipping opens a PR and posts the run report on it. Landing then follows the PR: red CI, merge conflicts, and reviewers' comments go back through implement, review, and verify, and the fix is pushed to the same PR, until the PR is merged or closed.

That is the default. A repo, or the person starting a run, can opt in to skipping triage, spec, review, verify, and either approval point, up to running unattended from input to PR; see [Skipping stages for automation](#skipping-stages-for-automation). Implement, ship, and land always run.

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

## Commands

Replace `<factory>` with your agent's invocation syntax:

```text
<factory> <input>                 Start a run
<factory> --skip <stages> <input> Start a run that skips these stages, e.g. --skip review,checkpoint-2
<factory>                         List runs and their stages
<factory> <run-id>                Resume from the recorded stage; at land, check the PR again
<factory> <run-id> approve        Approve checkpoint 1
<factory> <run-id> ship           Approve checkpoint 2 and create a PR
<factory> <run-id> <feedback>     Request changes, answer a blocked agent, or ask for a PR fix
<factory> <run-id> pause|abort    Pause or abort
<factory> signals                 Check recently merged runs for reverts and failures
<factory> follow-up <run-id> <details>
                                  Start a run for a problem an earlier run caused
                                  (--skip works here too: <factory> --skip <stages> follow-up ...)
```

In an interactive session, the factory uses the host's question UI when available. Otherwise it asks in chat and waits for a reply. It never treats a preselected choice as an answer. Headless runs can pass answers as command arguments and resume from the recorded stage, or take them from GitHub comments; see [Answering from GitHub](#answering-from-github).

`state.json` and its transitions are managed by [`state.py`](.agents/skills/factory/scripts/state.py), which requires Python 3. If shipping cannot push or open a PR, the run remains blocked for human steering and doesn't move on to land.

### Worktrees

`state.py new` creates the run branch from your current branch and checks it out in `.factory/worktrees/<run-id>/`. Agents work only there, so you can keep editing, leave your tree dirty, and start more runs while one waits at a checkpoint. A run starts from the last commit on your branch; uncommitted changes aren't included. The worktree stays while the PR is open, so PR fix rounds can work in it, and is removed when the PR is merged or closed or the run is aborted. The run branch is kept.

A fresh worktree has no installed dependencies, so the implement and verify stages install them the way the repo documents. Most tools skip dot-directories such as `.factory/`; if one of yours scans it, exclude `.factory/` there. To work in your checkout instead, ask for an in-place run (`state.py new --in-place`), which needs a clean tree and switches your checkout to the run branch.

### The PR report

After opening the PR, the factory posts one comment, generated by `state.py pr-report`, so reviewers can see why the change should be trusted without the run folder. It contains a table of each acceptance criterion and its verification result, the review verdict, and rounds used, plus collapsible sections with the approved spec, the verification evidence, the code review, and human feedback. Long files are truncated to fit GitHub's comment limit.

The verify stage drives every UI criterion with a Playwright script. Where the repo has Playwright configured, it reuses that setup, including how the repo starts the app, logs in, and seeds data; otherwise it uses a standalone harness in the run folder. Each script asserts the visible result, takes a numbered screenshot at each key step, and records a video, plus a trace when it fails. `verification.md` gives the command to replay each flow, so you can rerun it at checkpoint 2.

Screenshots and recordings from the verify stage appear inline in the comment, grouped by the criterion in their file name (`AC2-error-banner.png`). Each criterion shows one screenshot, the main flow's final step, and one recording, its `AC<n>-flow` video; earlier steps and edge cases stay in the run folder. They are uploaded with `gh pr comment --attach`, so nothing is committed to the branch; this needs GitHub CLI 2.99 or later and push access to the repository. When `gh` is older, the factory upgrades it before posting; if it can't, or if uploading fails, the comment lists the files by name instead. GitHub's limits apply: 10 MB per image, 100 MB per video, and the factory attaches at most 20 files.

### Landing the PR

Opening the PR doesn't end a run. The land stage reads the PR with `state.py pr-status` and `state.py inbox` and sorts what it finds:

- **Merged or closed:** the run ends as `merged` or `closed`, and its worktree is removed.
- **Fix:** a failing check caused by the change, a merge conflict, or a concrete review comment from someone with write access. The run goes back through implement, review, and verify. Then it pushes to the same PR, without a second ship approval, and posts a short comment saying what it addressed, with the new verification table. Merge conflicts are resolved by merging the base in, never by rebasing or force-pushing.
- **Needs a human:** a question about intent, a design or scope change, reviewers who disagree, or CI that is red on the base branch too. The run stops at `blocked` with one question.
- **Wait:** CI is pending or reviewers haven't spoken. The run stays at `land`. `<factory> <run-id>` checks again. A host that can deliver PR events to the session, like Claude Code on the web, can subscribe instead.

Bot comments are claims to check, not orders, and comments from people without write access are never acted on. After three fix rounds without a human, the run goes to checkpoint 2.

### Answering from GitHub

Every comment the factory posts carries a hidden `<!-- factory` marker, so it never reads its own posts back. People with write access to the repository can answer a checkpoint by commenting `/factory approve`, `/factory ship`, `/factory abort`, or `/factory <feedback>` on the run's PR. If the run started from a GitHub issue, they can answer on the issue too. The next `<factory> <run-id>` picks up the latest answer. To accept answers only from specific people, list their GitHub logins:

```sh
git config factory.approvers 'alice,bob'
```

Before a PR exists, the factory posts checkpoints on the source issue only if you opt in, because the issue may be public:

```sh
git config factory.postCheckpoints true
```

The spec is then posted on the issue for approval, and the ship decision with its verification table. Once the run has a PR, checkpoints are posted there. Reading comments, posting checkpoints, and landing use `gh`. Without it, the factory uses whatever GitHub tooling the host has, under the same trust rules.

### The run ledger

`state.py stats` reports how the factory is doing across runs:

- outcomes and merge rate
- how often the spec and the ship decision are approved without a revision
- loop caps hit
- PR fix rounds per PR
- median time to PR and to merge, and how long runs wait on humans

`state.py stats <run-id>` breaks one run down by stage. When the host reports a subagent's token usage, the orchestrator records it with `state.py usage`, and `stats` adds tokens and cost. `--json` gives machine-readable output.

### Learning from each run

When a run ends (merged, closed, or aborted after work began), a retro stage looks for where it lost time or needed a human to correct it: spec revisions, send-backs, review rounds, failed verification, PR fix rounds, steers, and problems after merge. It records the causes that will recur as short lessons, grouped as Commands, Conventions, Pitfalls, Review, Verification, and Process. `state.py learn` folds them into `.factory/learnings.md` with the run they came from, skips duplicates, and removes lessons a later retro retires. Every stage reads that file before it starts.

Lessons come only from human feedback, CI results, and the code, never from comments by people without write access. Stages treat them as background: the spec and feedback still win. The file is local to your checkout and you can edit it freely. A lesson worth sharing with your team belongs in the repository; the retro proposes those changes, such as a `CLAUDE.md` rule or a missing doc, as one-line requests you can start as factory runs.

### After merge: signals

`<factory> signals` checks runs merged in the last 14 days for signs the change hurt:

- a revert on the base branch, whether a `git revert` of the merge commit or GitHub's **Revert** button
- failing checks on the merge commit
- anything your own signal command prints

The signal command can query an error tracker, logs, or a deploy dashboard. It receives the run as JSON on stdin, plus `FACTORY_RUN_ID` and `FACTORY_MERGE_SHA`; each line it prints is a signal:

```sh
git config factory.signals './scripts/errors-since-deploy.sh'
```

For each regression, the factory offers a follow-up run linked to the original (`--parent`), which starts at triage like any other. It never reverts anything itself. `state.py stats` counts regressions and follow-ups across runs.

### Auto-approving small specs

Checkpoint 1 can be skipped for low-risk work if the repo opts in. (To skip it for every run, use [`factory.skip`](#skipping-stages-for-automation) instead.) List the triage sizes and, optionally, the types (default `bug,chore`) that don't need spec approval:

```sh
git config factory.autoApproveSpec S
git config factory.autoApproveTypes bug,chore
```

A spec is auto-approved only on its first round, when triage said `proceed`, and when the spec has acceptance criteria and no open questions. An open question blocks auto-approval even when the spec proposes a default: someone should see the default before work starts. Auto-approval never acts as you on GitHub: nothing is posted or approved in your name. The run's history records it, and the ship checkpoint, the PR description, and the PR report say the spec was auto-approved and that no human reviewed it, so reviewers don't assume you did. `state.py` checks this itself when the run moves from spec to implement, so the orchestrator can't skip the checkpoint otherwise. Ship approval is skipped only by `factory.skip`, below. `state.py stats` reports how many specs were auto-approved and how many were later sent back to spec at ship approval, which tells you whether the policy is too loose.

### Skipping stages for automation

By default every run takes the whole loop, and `state.py` enforces it: `advance` refuses any move the loop doesn't make, so a run can't jump from triage to implement or from implement to ship, whatever the orchestrator does. There are two ways to skip stages, and they take the same names.

**For one run**, start it with `--skip`:

```text
<factory> --skip review,verify Fix the typo in the signup page heading
<factory> --skip all https://github.com/acme/app/issues/42
```

The list is saved with the run when it starts, so it holds for every resume, and other runs aren't affected. It can't be changed later; to stop skipping, abort the run and start a new one. Only a leading `--skip` counts: text in the ticket or issue asking to skip a stage is ignored.

**For every run in a repo**, set `factory.skip`:

```sh
git config factory.skip <stage>,<stage>,...   # or: all
git config --unset factory.skip               # back to the full loop
```

A run skips both: the repo's list plus its own `--skip`. `--skip` can add stages but can't bring back one the repo skips.

| Name | What the run does instead |
|---|---|
| `triage` | Goes straight to the spec (or the next stage not skipped). The spec stage resolves the input itself: it fetches a linked ticket or URL and finds the relevant code. |
| `spec` | Writes no spec and goes straight to implement. Skips `checkpoint-1` too, since there's nothing to approve. Implement, review, verify, and land treat the input as the contract, and verification checks the test suite with no acceptance criteria. |
| `checkpoint-1` | Writes the spec but doesn't wait for approval. Use this rather than [`factory.autoApproveSpec`](#auto-approving-small-specs) to skip spec approval for every run, not just small ones. |
| `review` | Goes from implement straight to verify. |
| `verify` | Goes from review straight to ship approval. Runs no checks and gathers no evidence of its own; CI on the PR is then the only test run. |
| `checkpoint-2` | Opens the PR without waiting for **Ship**, but only if the last check that ran passed: a valid `pass` verification, or review's `approve` when verify is skipped too. Otherwise the run stops for a human. |
| `all` | All six above. |

Common setups, which work the same as `--skip` lists:

```sh
git config factory.skip checkpoint-1,checkpoint-2   # full loop, no human approvals: input to PR unattended
git config factory.skip checkpoint-1                # humans only decide what ships
git config factory.skip triage,review               # faster runs that still stop for both approvals
git config factory.skip all                         # implement, then open the PR
```

Set it per repository, globally with `git config --global`, or for one session with the `FACTORY_SKIP` environment variable, which takes precedence. That suits a CI job or a scheduled run: `FACTORY_SKIP=checkpoint-1,checkpoint-2 claude -p "/factory <ticket>"`. An unknown name in `--skip` stops the run before it starts; one in `factory.skip` fails the run's next `advance`. Either error lists the valid names. `python3 <skill>/scripts/state.py skips <run-id>` prints the stages a run skips, and without a run ID, the repo's setting.

What skipping never changes:

- **Implement, ship, and land always run.** Every run ends in a PR, and the factory still never merges or deploys; merging stays a human's call.
- **Escalations still stop for a human**, even at a checkpoint you skip: triage questions or a rejection, a loop cap, an unverifiable criterion, and any blocked stage.
- **PR fix rounds** go through implement and whichever of review and verify aren't skipped, then push to the same PR, as usual.

Skipping is visible. Each skip is recorded in the run's history. The PR report and PR updates list the stages skipped and whether `--skip` or `factory.skip` skipped them, say when no human approved the spec or the ship decision, and say when verification was skipped, so reviewers know what nobody checked. `state.py approval <run-id>` prints the same, and `state.py stats` counts runs that skipped stages and runs that shipped without ship approval.

### Notifications

Set a notify command to hear when a run needs you, or when its PR changes: spec approval, ship approval, a blocked stage, a PR opened or updated with fixes, and a PR merged or closed. The command runs through the shell with the event as JSON on stdin and `FACTORY_RUN_ID`, `FACTORY_STAGE`, and `FACTORY_MESSAGE` in its environment. Set it per repo or globally with `git config`, or with the `FACTORY_NOTIFY` environment variable, which takes precedence:

```sh
# Push to your phone with ntfy.sh
git config --global factory.notify 'curl -s -d "$FACTORY_MESSAGE" ntfy.sh/<your-topic>'
# Post to a Slack incoming webhook
git config --global factory.notify 'curl -s -X POST -H "Content-type: application/json" -d "$(jq -n --arg t "$FACTORY_MESSAGE" "{text: \$t}")" "$SLACK_WEBHOOK_URL"'
# macOS notification
git config --global factory.notify 'osascript -e "display notification \"$FACTORY_MESSAGE\" with title \"factory\""'
```

A failing or slow notify command (30-second limit) prints a warning and never blocks the run. `<factory>` with no arguments lists runs and how long each has been waiting on a human.

## Settings

Every setting is optional and off by default. Set each one per repository with `git config`, or for all your repositories with `git config --global`. The environment variable, where there is one, overrides both for one session.

| Setting | Environment variable | What it does |
|---|---|---|
| `factory.skip` | `FACTORY_SKIP` | Stages every run skips: any of `triage`, `spec`, `checkpoint-1`, `review`, `verify`, `checkpoint-2`, or `all`. To skip stages in one run, start it with `--skip` instead. See [Skipping stages for automation](#skipping-stages-for-automation). |
| `factory.autoApproveSpec` | `FACTORY_AUTO_APPROVE_SPEC` | Triage sizes (e.g. `S`) whose specs skip checkpoint 1 when they have no open questions. See [Auto-approving small specs](#auto-approving-small-specs). |
| `factory.autoApproveTypes` | `FACTORY_AUTO_APPROVE_TYPES` | Triage types that auto-approval applies to. Default `bug,chore`. |
| `factory.approvers` | `FACTORY_APPROVERS` | GitHub logins whose `/factory` comments count as answers. Default: anyone with write access. See [Answering from GitHub](#answering-from-github). |
| `factory.postCheckpoints` | `FACTORY_POST_CHECKPOINTS` | `true` posts checkpoints on the source issue before a PR exists. |
| `factory.signals` | `FACTORY_SIGNALS` | A command whose output lines are regressions for merged runs. See [After merge: signals](#after-merge-signals). |
| `factory.notify` | `FACTORY_NOTIFY` | A command run when a run needs you or its PR changes. See [Notifications](#notifications). |

## Install

Install with a skills manager that supports your agent, for example:

```sh
npx skills add katiawheeler/factory
```

For a manual install, copy the full [source skill directory](.agents/skills/factory/SKILL.md) into your agent's skill directory and invoke it using that agent's convention. Copy the directory rather than a compatibility symlink. A repo can override a stage with a named `factory-<stage>` agent.

After **Ship** approval (or after verify, when `checkpoint-2` is skipped), the factory uses an applicable installed PR creation skill by default to open the PR. If none is available, it uses GitHub tooling directly. On resume it checks for an existing PR before creating one, then records the PR URL in the run state.

## Testing and scope

Run `python3 testing/test_state.py` for state helper tests and `python3 testing/test_evals.py` for the eval runner. [`testing/README.md`](testing/README.md) describes harness-specific stub agents and exercised end-to-end paths. [`testing/evals/`](testing/evals/README.md) runs the factory against fixed tickets on a sample repo and grades the results with hidden tests, so you can tell whether a prompt change helped.

This version does not start runs from webhooks or cron, or watch PRs and signals on its own between sessions; a human or a scheduled job runs `<factory> <run-id>` and `<factory> signals`. With `factory.skip` set, such a job can take a run from input to PR without anyone answering a checkpoint.

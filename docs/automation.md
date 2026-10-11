# Automation

How to take humans out of parts of the loop: skip stages for one run or every run, or auto-approve small specs.

## Skipping stages for automation

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
| `checkpoint-1` | Writes the spec but doesn't wait for approval. Use this rather than [`factory.autoApproveSpec`](automation.md#auto-approving-small-specs) to skip spec approval for every run, not just small ones. |
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

## Auto-approving small specs

Checkpoint 1 can be skipped for low-risk work if the repo opts in. (To skip it for every run, use [`factory.skip`](automation.md#skipping-stages-for-automation) instead.) List the triage sizes and, optionally, the types (default `bug,chore`) that don't need spec approval:

```sh
git config factory.autoApproveSpec S
git config factory.autoApproveTypes bug,chore
```

A spec is auto-approved only on its first round, when triage said `proceed`, and when the spec has acceptance criteria and no open questions. An open question blocks auto-approval even when the spec proposes a default: someone should see the default before work starts. Auto-approval never acts as you on GitHub: nothing is posted or approved in your name. The run's history records it, and the ship checkpoint, the PR description, and the PR report say the spec was auto-approved and that no human reviewed it, so reviewers don't assume you did. `state.py` checks this itself when the run moves from spec to implement, so the orchestrator can't skip the checkpoint otherwise. Ship approval is skipped only by `factory.skip`, below. `state.py stats` reports how many specs were auto-approved and how many were later sent back to spec at ship approval, which tells you whether the policy is too loose.

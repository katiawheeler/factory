# factory

A software factory for any coding agent that can read a skill and run Git and Python. Give it a ticket, URL, or prompt: it triages the request, writes a spec, implements it on a branch, reviews and verifies the work, opens a PR, and follows that PR through CI and review until it merges or closes. A human approves the spec and the decision to ship, unless the repo opts in to automating them. It never merges or deploys; merging stays a human's call. Inspired by Warp's [guide to cloud software factories](https://www.warp.dev/blog/a-guide-to-cloud-software-factories-for-engineering-leaders).

![A ticket, URL, or prompt enters a loop of Triage, Spec, Implement, Review, Verify, Ship, and Land, with a human approving the spec and deciding to ship, and Land following the PR until it merges](docs/factory-loop.svg)

## Install

```sh
npx skills add katiawheeler/factory
```

Or copy [`.agents/skills/factory/`](.agents/skills/factory/SKILL.md) into your agent's skill directory. The factory needs Git and Python 3, and `gh` (or your agent's GitHub tools) for GitHub. See [Install and use](docs/usage.md).

## Use

Replace `<factory>` with your agent's way of invoking a skill (`/factory` in Claude Code, `$factory` in Codex):

```text
<factory> <ticket | URL | prompt>    Start a run
<factory>                            List runs and their stages
<factory> <run-id>                   Resume a run; at land, check the PR again
<factory> <run-id> approve           Approve the spec (checkpoint 1)
<factory> <run-id> ship              Approve shipping and open the PR (checkpoint 2)
<factory> <run-id> <feedback>        Request changes, answer a question, or ask for a PR fix
<factory> <run-id> pause|abort       Pause or abort
```

Each run works in its own Git worktree, so your checkout and uncommitted changes are left alone, and its state lives in `.factory/runs/<run-id>/`, so a run survives a dead session. Checkpoints can also be answered with `/factory approve` comments on the issue or PR. All commands: [Install and use](docs/usage.md).

## Automate it

Skip stages for one run, or for every run in a repo:

```text
<factory> --skip review,checkpoint-2 <input>       # this run only
git config factory.skip checkpoint-1,checkpoint-2  # every run: input to PR with no human approvals
```

Any of `triage`, `spec`, `checkpoint-1`, `review`, `verify`, `checkpoint-2`, or `all`. Implement, ship, and land always run, so every run still ends in a PR that a human merges, and escalations still stop for a human. See [Automation](docs/automation.md) and [Settings](docs/settings.md).

## Architecture

![Architecture of a factory run. A human or CI job invokes the orchestrator, which dispatches stage agents and records every stage move through the state helper. The state helper reads the repo's factory.* settings and is the only writer of the run folder. Stage agents write their outputs to the run folder and commit in the run's worktree, which is pushed to a GitHub PR; CI results and reviews come back to the orchestrator, and checkpoints go back to the human.](docs/factory-architecture.svg)

`state.py` is the only thing that writes run state: it refuses stage moves the loop doesn't make, applies the skip settings, and locks each run while changing it. See [Architecture](docs/architecture.md).

## Documentation

- [How it works](docs/how-it-works.md): stages, outputs, human decisions, loop caps, worktrees
- [Install and use](docs/usage.md): every command and how checkpoints are answered
- [Pull requests](docs/pull-requests.md): the PR report, landing the PR, answering from GitHub
- [Automation](docs/automation.md): `--skip`, `factory.skip`, and auto-approving small specs
- [Settings](docs/settings.md): every `factory.*` setting, and notifications
- [Measuring and learning](docs/learning.md): run stats, retros and learnings, signals after merge
- [Development](docs/development.md): tests, CI, and evals

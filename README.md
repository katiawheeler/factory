<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.png">
    <img alt="factory" src="docs/assets/logo-light.png" width="320">
  </picture>
</p>

<h3 align="center">From ticket to merge-ready PR, with a human on every call that matters.</h3>

<p align="center">
  <a href="https://github.com/katiawheeler/factory/actions/workflows/test.yml"><img alt="tests" src="https://github.com/katiawheeler/factory/actions/workflows/test.yml/badge.svg"></a>
  <a href="testing/evals/README.md"><img alt="eval baseline: 4/4 passing" src="https://img.shields.io/badge/eval_baseline-4%2F4_passing-B7EE42"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&amp;logoColor=white">
  <img alt="dependencies: git and python" src="https://img.shields.io/badge/dependencies-git_%2B_python-1B1E23">
  <br>
  <a href=".agents/skills/factory/SKILL.md"><img alt="Claude Code skill" src="https://img.shields.io/badge/Claude_Code-skill-D97757?logo=claude&amp;logoColor=white"></a>
  <a href=".agents/skills/factory/agents/openai.yaml"><img alt="Codex skill" src="https://img.shields.io/badge/Codex-skill-1B1E23"></a>
  <img alt="never merges: you do" src="https://img.shields.io/badge/merges-never_(you_do)-B7EE42">
  <a href="https://github.com/katiawheeler/factory/commits/main"><img alt="last commit" src="https://img.shields.io/github/last-commit/katiawheeler/factory?color=1B1E23"></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#use">Use</a> ·
  <a href="#automate-it">Automate it</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#documentation">Docs</a>
</p>

---

**factory** is a software factory for any coding agent that can read a skill and run Git and Python. Give it a ticket, URL, or prompt: it triages the request, writes a spec, implements it on a branch, reviews and verifies the work, opens a PR, and follows that PR through CI and review until it merges or closes. A human approves the spec and the decision to ship, unless the repo opts in to automating them. It never merges or deploys; merging stays a human's call.

<p align="center">
  <img alt="A ticket, URL, or prompt enters a loop of Triage, Spec, Implement, Review, Verify, Ship, and Land, with a human approving the spec and deciding to ship, and Land following the PR until it merges" src="docs/factory-loop.svg" width="760">
</p>

## Why factory

<table>
  <tr>
    <td width="50%">📝 <b>Spec before code.</b> Triage grounds the request in your repo, and the spec turns it into testable acceptance criteria you approve. <a href="docs/how-it-works.md">How it works →</a></td>
    <td width="50%">🔬 <b>Proof, not promises.</b> Verify runs the change, drives UI with Playwright, and puts the evidence for every criterion in the PR. <a href="docs/pull-requests.md">Pull requests →</a></td>
  </tr>
  <tr>
    <td width="50%">🛬 <b>Follows the PR to merge.</b> Red CI, merge conflicts, and review comments go back through implement, review, and verify, and land on the same PR. <a href="docs/pull-requests.md">Pull requests →</a></td>
    <td width="50%">💬 <b>Answer from anywhere.</b> Approve with <code>/factory approve</code> on the issue or PR, and get notified wherever you like. <a href="docs/usage.md">Install and use →</a></td>
  </tr>
  <tr>
    <td width="50%">🧠 <b>Gets better every run.</b> Each finished run's retro teaches the next one, and merged runs are checked for reverts and failures. <a href="docs/learning.md">Measuring and learning →</a></td>
    <td width="50%">🛡️ <b>Safe by default.</b> Every run gets its own worktree; it never merges or force-pushes, and it ignores commands from people without write access. <a href="docs/architecture.md">Architecture →</a></td>
  </tr>
  <tr>
    <td width="50%">⚙️ <b>Automate as trust grows.</b> Skip stages for one run or every run, or auto-approve small, clear specs. <a href="docs/automation.md">Automation →</a></td>
    <td width="50%">📊 <b>Measured, not guessed.</b> Run stats track time, rounds, and outcomes, and evals with hidden tests catch prompt regressions. <a href="docs/development.md">Development →</a></td>
  </tr>
</table>

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

| Guide | What's in it |
|---|---|
| [How it works](docs/how-it-works.md) | Stages, outputs, human decisions, loop caps, worktrees |
| [Install and use](docs/usage.md) | Every command and how checkpoints are answered |
| [Pull requests](docs/pull-requests.md) | The PR report, landing the PR, answering from GitHub |
| [Automation](docs/automation.md) | `--skip`, `factory.skip`, and auto-approving small specs |
| [Settings](docs/settings.md) | Every `factory.*` setting, and notifications |
| [Measuring and learning](docs/learning.md) | Run stats, retros and learnings, signals after merge |
| [Development](docs/development.md) | Tests, CI, and evals |

---

<p align="center">
  <sub>Inspired by Warp's <a href="https://www.warp.dev/blog/a-guide-to-cloud-software-factories-for-engineering-leaders">guide to cloud software factories</a>.</sub>
</p>

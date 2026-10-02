# factory

A software factory workflow for any coding agent that can read the skill and run Git and Python commands. Give it a ticket, URL, or prompt; it triages the request, writes a spec, implements it on a branch, reviews the diff, verifies the acceptance criteria, and opens a PR after human approval. It does not merge or deploy. Inspired by Warp's [guide to cloud software factories](https://www.warp.dev/blog/a-guide-to-cloud-software-factories-for-engineering-leaders).

```text
<factory> <ticket | URL | prompt>
```

Use your agent's syntax for invoking an installed skill. The factory can dispatch stage subagents when the host supports them; otherwise the orchestrator follows the same stage instructions itself.

## The factory loop

![A ticket, URL, or prompt enters a loop of Triage, Spec, Implement, Review, Verify, and Ship that opens a PR, with a human approving the spec and deciding to ship](docs/factory-loop.svg)

Agents write their outputs to `.factory/runs/<run-id>/`. At the two approval points, the host presents those outputs to the human and saves the answer in `feedback.md` for the next stage. Review or verification failures return to implementation, then pass through review again. A human can steer when work is blocked. Shipping opens a PR; this version does not monitor or deploy it.

| Stage | Output | Human decision |
|---|---|---|
| Triage | `triage.md` with resolved input, risks, questions, and verdict | Clarify or override if needed |
| Spec | `spec.md` with testable acceptance criteria, open questions, and diagrams where they help | Checkpoint 1: approve or request a revision; the human may edit `spec.md` directly |
| Implement | Code commits and `implementation.md` | Steer if blocked |
| Review | `review.md` with blocking findings and notes | Findings are shown at checkpoint 2 |
| Verify | `verification.md` and `evidence/`, with a suite result and one result per criterion | Checkpoint 2: inspect evidence, ship, or send work back |
| Ship | Pushed branch and PR URL in `state.json` | Only after the human says **Ship** |

Review gets three consecutive attempts; verification gets three attempts between human checkpoints. A cap sends the run to checkpoint 2 with the problem stated. Failed verification returns to implementation, then review, then verification. An environment limitation can go straight to checkpoint 2 with the affected criteria named.

## Commands

Replace `<factory>` with your agent's invocation syntax:

```text
<factory> <input>                 Start a run
<factory>                         List runs and their stages
<factory> <run-id>                Resume from the recorded stage
<factory> <run-id> approve        Approve checkpoint 1
<factory> <run-id> ship           Approve checkpoint 2 and create a PR
<factory> <run-id> <feedback>     Request changes or answer a blocked agent
<factory> <run-id> pause|abort    Pause or abort
```

In an interactive session, the factory uses the host's question UI when available. Otherwise it asks in chat and waits for a reply. It never treats a preselected choice as an answer. Headless runs can pass answers as command arguments and resume from the recorded stage.

`state.json` and its transitions are managed by [`state.py`](.agents/skills/factory/scripts/state.py), which requires Python 3. `state.py new` requires a clean working tree on a named branch. Runs share a checkout and switch branches, so use a separate Git worktree for parallel runs. If shipping cannot push or open a PR, the run remains blocked for human steering and is not marked done.

## Install

Install with a skills manager that supports your agent, for example:

```sh
npx skills add katiawheeler/factory
```

For a manual install, copy the full [source skill directory](.agents/skills/factory/SKILL.md) into your agent's skill directory and invoke it using that agent's convention. Copy the directory rather than a compatibility symlink. A repo can override a stage with a named `factory-<stage>` agent.

After **Ship** approval, the factory uses an applicable installed PR creation skill by default. If none is available, it uses GitHub tooling directly. On resume it checks for an existing PR before creating one, then records the PR URL in the run state.

## Testing and scope

Run `python3 testing/test_state.py` for state helper tests. [`testing/README.md`](testing/README.md) describes harness-specific stub agents and exercised end-to-end paths.

This version does not monitor production signals, start runs from webhooks or cron, or track cost across runs.

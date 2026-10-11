# Architecture

![Architecture of a factory run. A human or CI job invokes the orchestrator, which dispatches stage agents and records every stage move through the state helper. The state helper reads the repo's factory.* settings and is the only writer of the run folder. Stage agents write their outputs to the run folder and commit in the run's worktree, which is pushed to a GitHub PR; CI results and reviews come back to the orchestrator, and checkpoints go back to the human.](factory-architecture.svg)

- **Orchestrator** ([`SKILL.md`](../.agents/skills/factory/SKILL.md), with [`guides/`](../.agents/skills/factory/guides/) for shipping, GitHub, steering, signals, and finishing a run): moves the run from stage to stage and stops at checkpoints. It does no stage work itself.
- **Stage agents** ([`stages/`](../.agents/skills/factory/stages/)): one subagent per stage (triage, spec, implement, review, verify, land, retro). Each reads its inputs from the run folder and writes one output file there; implement also commits in the worktree.
- **State helper** ([`scripts/state.py`](../.agents/skills/factory/scripts/state.py)): the only writer of `state.json`. It refuses stage moves the loop doesn't make, applies the [settings](settings.md) and `--skip`, locks each run while changing it, and builds the GitHub comments.
- **Your repository:** each run gets a worktree on its own `factory/<id>` branch, and a run folder under `.factory/runs/<id>/` that is the source of truth when a session dies and is resumed.
- **GitHub:** ship pushes the branch and opens the PR; land reads its CI, reviews, and `/factory` answers.

The diagram's source is [`docs/factory-architecture.html`](factory-architecture.html), drawn with [diagram-design](https://github.com/cathrynlavery/diagram-design) and exported to SVG.

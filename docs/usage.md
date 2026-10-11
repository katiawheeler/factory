# Install and use

## Install

Install with a skills manager that supports your agent, for example:

```sh
npx skills add katiawheeler/factory
```

For a manual install, copy the full [source skill directory](../.agents/skills/factory/SKILL.md), including `stages/`, `guides/`, and `scripts/`, into your agent's skill directory and invoke it using that agent's convention. Copy the directory rather than a compatibility symlink. A repo can override a stage with a named `factory-<stage>` agent.

After **Ship** approval (or after verify, when `checkpoint-2` is skipped), the factory uses an applicable installed PR creation skill by default to open the PR. If none is available, it uses GitHub tooling directly. On resume it checks for an existing PR before creating one, then records the PR URL in the run state.

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

In an interactive session, the factory uses the host's question UI when available. Otherwise it asks in chat and waits for a reply. It never treats a preselected choice as an answer. Headless runs can pass answers as command arguments and resume from the recorded stage, or take them from GitHub comments; see [Answering from GitHub](pull-requests.md#answering-from-github).

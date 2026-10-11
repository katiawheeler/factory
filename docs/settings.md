# Settings

Every setting is optional and off by default. Set each one per repository with `git config`, or for all your repositories with `git config --global`. The environment variable, where there is one, overrides both for one session.

| Setting | Environment variable | What it does |
|---|---|---|
| `factory.skip` | `FACTORY_SKIP` | Stages every run skips: any of `triage`, `spec`, `checkpoint-1`, `review`, `verify`, `checkpoint-2`, or `all`. To skip stages in one run, start it with `--skip` instead. See [Skipping stages for automation](automation.md#skipping-stages-for-automation). |
| `factory.autoApproveSpec` | `FACTORY_AUTO_APPROVE_SPEC` | Triage sizes (e.g. `S`) whose specs skip checkpoint 1 when they have no open questions. See [Auto-approving small specs](automation.md#auto-approving-small-specs). |
| `factory.autoApproveTypes` | `FACTORY_AUTO_APPROVE_TYPES` | Triage types that auto-approval applies to. Default `bug,chore`. |
| `factory.approvers` | `FACTORY_APPROVERS` | GitHub logins whose `/factory` comments count as answers. Default: anyone with write access. See [Answering from GitHub](pull-requests.md#answering-from-github). |
| `factory.postCheckpoints` | `FACTORY_POST_CHECKPOINTS` | `true` posts checkpoints on the source issue before a PR exists. |
| `factory.signals` | `FACTORY_SIGNALS` | A command whose output lines are regressions for merged runs. See [After merge: signals](learning.md#after-merge-signals). |
| `factory.notify` | `FACTORY_NOTIFY` | A command run when a run needs you or its PR changes. See [Notifications](settings.md#notifications). |

## Notifications

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

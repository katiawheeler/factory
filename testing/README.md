# Testing the factory

## State helper unit tests

```sh
python3 testing/test_state.py
```

These cover run creation in a per-run worktree and in place, sharing runs across worktrees, releasing a worktree, notifications, the PR report (media attached with a gh that supports `--attach`, listed by name without one, oversized files skipped) and its size limit, dirty-tree and detached-HEAD guards, advance and set guards, loop-cap attempt counting and resets, feedback headings, `list`, and verification report validation.

## Forcing loop caps and blocked stages with Claude Code stub agents

Real agents rarely loop to a cap, because they escalate first. To test the orchestrator's cap and blocked handling, swap in stub agents. The stubs return whatever verdict a control file says.

1. In the target repo, install the stubs as project agents. Named `factory-<stage>` agents override the bundled stage prompts in `stages/`. If the repo already has its own `factory-*` agents in `.claude/agents/`, back them up first.
   ```sh
   mkdir -p .claude/agents && cp <factory>/testing/stub-agents/*.md .claude/agents/
   echo '.claude/' >> "$(git rev-parse --git-path info/exclude)"   # keeps the tree clean for `state.py new`
   ```
2. Start a run so `.factory/` exists, then write `.factory/stubctl` in the main checkout (not in the run's worktree):
   ```
   TRIAGE=proceed            # proceed | needs-human | reject
   REVIEW=changes-requested  # approve | changes-requested
   VERIFY=fail               # pass | fail | unverifiable
   IMPLEMENT=done            # done | blocked
   ```
   Edit it between calls to steer what happens next.
3. Drive the run headlessly, e.g. `claude -p "/factory <run-id> approve" --allowedTools "Bash Read Write Edit Glob Grep Agent"`, and check `.factory/runs/<id>/state.json` history.
4. Remove `.claude/agents/factory-*.md` when you're done.

The stubs run on Haiku and only touch `STUB.txt`, so a full cap cycle takes a few minutes.

## End-to-end flows for worktrees, the PR report, and notifications

These additions have unit tests, but no full factory run with real agents has exercised them yet. Run these flows to cover them, then record the results in the coverage table below. Use a sample repo with a remote you can open PRs on. Flows marked **(UI)** need a repo with a web UI and a verify stage that can drive a browser, like `notesy`.

Useful checks, run from the main checkout:

```sh
S=<factory>/.agents/skills/factory/scripts/state.py
python3 $S show <id>                                   # stage, repo, worktree, pr_url, report_url
git worktree list                                      # which worktrees exist
git branch --show-current && git status --short        # your checkout is untouched
git diff --name-only <base>...factory/<id>             # what the run branch actually changes
```

### Worktree per run

1. **Dirty checkout stays untouched.** Make an uncommitted edit on `main`, then start a run. Expect the run to start (no "commit or stash" stop), the orchestrator to say the run starts from the last commit and leaves your changes out, `state.json` `repo` and `worktree` to both be `.factory/worktrees/<id>`, and your checkout to stay on `main` with the edit still in place for the whole run.
2. **Dependencies in a fresh worktree.** On a repo that needs an install step (`npm install`, a virtualenv), run through verify. Expect implement and verify to install dependencies inside the worktree, and `git diff --name-only` to show no `node_modules/`, virtualenv, or build output.
3. **Parallel runs from one checkout.** While run A waits at ②, start run B from the same checkout without `git worktree add`. Expect `/factory` with no arguments to list both runs, each with its own branch and worktree, and approving A not to change B.
4. **Ship releases the worktree.** Ship a run. Expect `git worktree list` to no longer show it, the `factory/<id>` branch to remain locally and on the remote, and your checkout to still be on `main`.
5. **Abort with leftover work.** Abort a run while its worktree has uncommitted changes, e.g. kill the session mid-implement and then send `/factory <id> abort`. Expect `release` to refuse, the orchestrator to report the worktree path instead of deleting anything, and the changes to still be in the worktree.
6. **In-place on request.** Ask for an in-place run. Expect the old behavior: a dirty tree stops the run, the checkout switches to the run branch at implement, and it switches back to `main` after ship or abort.
7. **Runs from before worktrees.** Create a run with `--in-place`, delete the `worktree` key from its `state.json` to mimic a run made before this change, and resume it. Expect it to be handled as in-place, with no attempt to release a worktree.
8. **Stub agents.** Repeat a stub cap cycle from the section above. Expect the stubs to read `.factory/stubctl` from the main checkout while committing `STUB.txt` in the worktree.

### PR report

1. **(UI) Screenshots attached inline.** With `gh` 2.99 or later and push access, run a UI change through ship. Expect `evidence/` to hold `AC<n>-<what>.png` files and a PR comment containing:
   - the hidden `<!-- factory-run: <id> -->` marker
   - the criteria table
   - the four collapsible sections
   - each screenshot rendered inline under its criterion, with a GitHub-hosted URL, not a local `evidence/` path

   Also expect `report_url` in `state.json` to link to that comment, and `git diff --name-only` to show nothing from `evidence/` or `.factory/`.
2. **(UI) Recording.** Have verify save a short `AC<n>-*.mp4` or `.webm` recording. Expect it to play inline in the comment. A link instead of a player means the reference isn't alone in its paragraph.
3. **No duplicate comment on resume.** Before ship completes, post the comment yourself with the command `state.py pr-report <id>` prints, leave `report_url` unset, then `/factory <id>`. Expect the orchestrator to find the marker, record that comment's URL, and post no second comment.
4. **Older `gh`.** Put a `gh` older than 2.99 first on `PATH` and ship. Expect a comment without `--attach` that lists the screenshots by name, and the run to reach `done`.
5. **Upload fails.** Ship to a repo where the token can comment but has no push access. Expect the `--attach` command to fail, the orchestrator to rerun `pr-report --no-media` and post the text-only comment, and the run to reach `done`.
6. **No `gh`.** Ship with `gh` absent and only the GitHub MCP tools available. Expect `pr-report` to print only the file path, and the orchestrator to post `pr-report.md` with the MCP tools.
7. **Oversized files.** Put an image over 10 MB in `evidence/` before ship. Expect it to be named under "Not uploaded" in the comment and left out of the `--attach` list, while the other images still upload.

### Notifications

1. **Every human stop notifies once.** `git config factory.notify 'cat >> /tmp/factory-notify.log; echo >> /tmp/factory-notify.log'`, then run a request vague enough for triage to return `needs-human`, all the way to ship. Expect one JSON line each for `checkpoint-1` (triage questions), `checkpoint-1` (spec), `checkpoint-2`, and `done` with `pr_url`, and none for work stages.
2. **Blocked.** Use the implement stub with `IMPLEMENT=blocked`. Expect a `blocked` event whose `note` is the agent's question.
3. **A real channel.** Set the ntfy or Slack example from the main README. Expect a message on your phone or channel at each stop, saying which run, which checkpoint, and the one-line summary.
4. **Environment variable wins.** With `factory.notify` set, run headlessly with `FACTORY_NOTIFY` set to a different command. Expect only the environment command to run.
5. **Failing command.** Set `factory.notify` to `exit 1`. Expect the orchestrator to mention the warning once and the run to continue normally.
6. **Waiting time.** Leave a run at ② for a few hours, then run `/factory` with no arguments. Expect it to show `waiting on human for <n>h`.

## Test coverage

| Path | How it was tested | Result |
|---|---|---|
| Full loop, spec revision at ①, ship to a remote | real agents, sample repo | ✅ |
| Answering ① and ② interactively (AskUserQuestion, "Other" = feedback) | real agents | ✅ |
| Resume in a fresh session, answers passed as arguments (headless) | real agents | ✅ |
| Triage `needs-human`, and `approve` at triage questions → spec | real agents | ✅ |
| Review catches a real problem → implement → approve | real agents | ✅ |
| Sending the run back from ② to verify | real agents | ✅ |
| Verify `unverifiable` → ② (no implement loop) | real agents, injected criterion | ✅ |
| Abort before and after the branch exists → back on base | real agents | ✅ |
| Review cap: 3 change requests in a row → ② "review did not converge" | stubs | ✅ |
| Send-back from ② resets attempts; verify cap: 3 fails → ② | stubs | ✅ |
| Implement `blocked` → steer; answer via `/factory <id> <answer>` → back to implement | stubs | ✅ |
| Session dies during implement round 1 (branch exists, uncommitted partial work) → resume | real agents, simulated crash | ✅ |
| Ship opens a real GitHub PR (GitHub MCP; `gh` absent) and records `pr_url` | real agents, `katiawheeler/vibe` | ✅ |
| Steer at ① and ②: headless `steer` parks the run at `blocked` ("steer requested"), the next answer is logged under `Steer: checkpoint-N`, and a change the orchestrator commits itself goes through review → verify → ② | real agents | ✅ |
| Triage `reject` → human override → spec narrows scope to a pilot → abort | real agents | ✅ |
| Hand-edit `spec.md` at ①, then send revision feedback: edits kept, feedback applied | real agents | ✅ |
| Real verify failure (build broken) → sent back → implement fixes → review → verify passes | real agents, TypeScript/React (`notesy`) | ✅ |
| Larger TS repo with tsc, vitest and a Vite build; verify drives the UI in headless Chromium via Playwright | real agents, `notesy` | ✅ |
| `kill -9` of a live headless session mid-implement (dirty tree, no commit) → `/factory <id>` resumes without advancing | real agents, live kill | ✅ |
| Parallel runs, one `git worktree` each | real agents, 5 worktrees | ✅ |
| Headless `claude -p "/factory <id> ship"` pushes the run branch, opens a real PR with `gh`, records `pr_url` and stage `done` in `state.json`, and switches the session back to `main` | real agents, `katiawheeler/vibe#10` | ✅ |
| Picking Steer from the AskUserQuestion menu at ①: the steer is logged under `Steer: checkpoint-1`, spec round 2 applies it and returns to ①, then approve → implement | real agents, interactive `claude`, `katiawheeler/vibe` | ✅ |
| Worktree per run: dirty checkout untouched, dependencies installed, parallel runs, release on ship, abort with leftovers, `--in-place`, older runs, stubs (worktree flows 1–8) | not yet run | ⏳ |
| PR report: inline screenshots and recordings, no duplicate on resume, older `gh`, failed upload, no `gh`, oversized files (PR report flows 1–7) | not yet run | ⏳ |
| Notifications: each human stop, blocked, a real channel, environment override, failing command, waiting time (notification flows 1–6) | not yet run | ⏳ |

Codex GUI question routing and selection of a dedicated PR creation skill are documented behavior; they have not yet been exercised in an end-to-end factory run.

## Known limitations

- **In-place runs share the checkout.** A run created with `--in-place` switches the checkout to its branch, so only one can be active there at a time. Default runs each get a worktree and don't have this limit.
- **Untested end to end:** worktree-per-run, posting the PR report, and notifications are covered by the unit tests above, but have not yet been exercised in a full factory run with real agents. See [End-to-end flows for worktrees, the PR report, and notifications](#end-to-end-flows-for-worktrees-the-pr-report-and-notifications).
- **Long headless runs** can outlive a wrapping tool's timeout. Run them in the background and wait on the PID.

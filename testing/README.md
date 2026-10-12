# Testing the factory

## State helper unit tests

```sh
python3 testing/test_state.py
```

These cover `next` and `answer` (each stage's routing, re-dispatching a stage that wrote nothing and sending it to a human after three tries, sending back an unreadable output once, the review, verify and PR fix caps, unverifiable criteria, blocked stages and steering, `.factory/` leaking into the branch, skipped stages and auto-approved specs, trusted and untrusted answers on GitHub, ship waiting for the push, land's wait, fix, question and merge, the retro, learnings and cleanup, in-place runs, and aborting before work), concurrent writes to one run, the stage transitions `advance` allows and the caps it enforces and skipping stages with `factory.skip` and a run's own `--skip` (what can't be skipped, ship only after passing checks, spec-less verification, the report and stats lines), spec auto-approval policy and its guard, `learn` (adding, de-duplicating, retiring, categories), `signals` (reverts, checks on the merge commit, the custom command, the time window), follow-up runs with `--parent`, the land stage (shipping only after checkpoint 2 or for a PR fix round, PR fix rounds and their cap, merged and closed, land notifications), `pr-status` check summaries, `inbox` trust rules (write access, `factory.approvers`, bots, the factory's own posts, comments from before the cursor), `checkpoint-post` on an issue (opt-in) and on a PR, `pr-update`, the `usage` and `stats` ledger, run creation in a per-run worktree and in place, sharing runs across worktrees, releasing a worktree, notifications, the PR report (one screenshot and one recording per criterion, media attached with a gh that supports `--attach`, listed by name without one, oversized files skipped) and its size limit, dirty-tree and detached-HEAD guards, advance and set guards, loop-cap attempt counting and resets, feedback headings, `list`, and verification report validation.

## Eval runner tests

```sh
python3 testing/test_evals.py
```

These drive [`evals/run.py`](evals/README.md) with a fake agent that orchestrates the way `SKILL.md` says, by following `state.py next`: a passing case after a session that dies mid-stage and is resumed, stray files and a wrong triage verdict failing their checks, and the baseline comparison flagging a regression. To run the evals with real agents, see [`evals/README.md`](evals/README.md).

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
LAND=wait                 # merged | closed | fix | wait | needs-human | abort
   ```
   Edit it between calls to steer what happens next.
3. Drive the run headlessly, e.g. `claude -p "/factory <run-id> approve" --allowedTools "Bash Read Write Edit Glob Grep Agent"`, and check `.factory/runs/<id>/state.json` history.
4. Remove `.claude/agents/factory-*.md` when you're done.

The stubs run on Haiku and only touch `STUB.txt`, so a full cap cycle takes a few minutes.

## End-to-end flows for worktrees, the PR report, Playwright, and notifications

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
4. **Older `gh`.** Put a `gh` older than 2.99 first on `PATH` and ship. Expect `pr-report` to warn that `--attach` is missing, the orchestrator to upgrade `gh` and rerun it, and the comment to show the screenshots inline. Where the upgrade isn't possible, expect a comment that lists the screenshots by name, and the run to reach `done`.
5. **Upload fails.** Ship to a repo where the token can comment but has no push access. Expect the `--attach` command to fail, the orchestrator to rerun `pr-report --no-media` and post the text-only comment, and the run to reach `done`.
6. **No `gh`.** Ship with `gh` absent and only the GitHub MCP tools available. Expect `pr-report` to print only the file path, and the orchestrator to post `pr-report.md` with the MCP tools.
7. **Oversized files.** Put an image over 10 MB in `evidence/` before ship. Expect it to be named under "Not uploaded" in the comment and left out of the `--attach` list, while the other images still upload.

### Playwright flows in verify

The standalone recipe has been checked by hand: a script with `test.use({ video: 'on', trace: 'retain-on-failure' })` saved numbered screenshots to `EVIDENCE` and a `video.webm` per test, plus a `trace.zip` for the failing test. With the latest `@playwright/test` and an older preinstalled Chromium, it failed with `Executable doesn't exist` until `PW_CHROMIUM` pointed at the installed browser. Run these with real agents:

1. **(UI) Multi-step flow with login, standalone.** On a repo without Playwright, spec a change behind a login with a 4–6 step flow. Expect `spec.md` to write the flow as numbered steps from a known starting state. Expect verify to create `<run>/playwright/` with its own `node_modules`, a script per UI criterion that asserts the end state, `AC<n>-01-…` step screenshots, `AC<n>-flow.webm`, and a `Replay:` command that works when you paste it. The repo should stay clean.
2. **(UI) Repo with Playwright configured.** On a repo with a Playwright config, `webServer`, and a login setup project, expect verify to copy the script into the repo's test directory, run it with the repo's config and fixtures, delete the copy, and leave `git status` clean.
3. **(UI) Failing flow.** Break the change so a UI criterion fails. Expect `Result: failed`, `AC<n>-trace.zip` named in Evidence with its `show-trace` command, and the video showing where the flow stopped.
4. **(UI) Hard-to-reach state.** Spec an error state, such as a failed save or an expired session. Expect the script to fake it with `page.route` or `page.clock`, and Evidence to say what was faked.
5. **(UI) Second round.** Send the run back from ② and verify again. Expect last round's screenshots and videos to be gone from `evidence/`, and the PR comment to show only the final round's media.
6. **(UI) Browser mismatch.** In an environment whose preinstalled browser doesn't match the latest `@playwright/test`, expect verify to set `PW_CHROMIUM` or install the matching browser, not to mark the criterion `unverifiable`.
7. **No browser.** In an environment with no usable browser and no downloads, expect the UI criteria to be `unverifiable` with the exact launch error, never `verified`.

### Landing the PR

Use a sample repo with CI on pull requests, and a second GitHub account (or a teammate) with write access to review.

1. **Wait, then merge.** Ship a run and resume it with `/factory <id>` while CI is pending. Expect `land.md` with `Verdict: wait` naming the pending checks, the run still at `land`, and `/factory` listing it as `PR open for review`. Merge the PR by hand, resume, and expect `merged`, the worktree removed, the branch kept, and `stats <id>` printed.
2. **Red CI.** Ship a change that breaks a CI check the local fast checks don't run. Expect land to read the failing log, a `fix` item naming the check, implement → review → verify, a plain `git push` (no force) to the same PR, a `pr-update` comment, and the run back at `land`.
3. **Review comment.** As the reviewer, leave an inline comment asking for a rename, and request changes. Expect a fix round that addresses it, a one-line reply on the thread ending in the hidden marker, and no thread resolved by the factory.
4. **Untrusted and bot comments.** From an account without write access, comment `/factory abort` and a change request. Have a bot (or a `[bot]` account) leave a finding that doesn't hold. Expect none of them acted on, and all three listed under **Not acted on**.
5. **Merge conflict.** Push a conflicting change to the base branch. Expect a fix round that merges the base in, with no rebase or force-push.
6. **Design question.** As the reviewer, ask "should this be a setting instead?". Expect `needs-human`, the run at `blocked` with that question, and a notification.
7. **Cap.** With stubs (`LAND=fix`), expect three PR fix rounds, then checkpoint 2 with "PR feedback did not converge".
8. **Closed.** Close the PR unmerged. Expect `closed` and the worktree removed.

### Answering from GitHub

1. **Approve on the issue.** With `git config factory.postCheckpoints true`, start a run from an issue URL. Expect `thread_url` set from triage's `Source:` line, and the full spec posted on the issue at checkpoint 1. Comment `/factory approve` there, run `/factory <id>`, and expect the approval logged with your login and the comment URL, then implement.
2. **Opt-in.** Without `factory.postCheckpoints`, expect no comment on the issue, while a `/factory approve` there is still read on resume.
3. **Ship on the PR.** Drive a run to checkpoint 2 after a PR fix cap (the PR exists). Expect the checkpoint posted on the PR, and `/factory ship` there to push the round.
4. **Untrusted answer.** From an account without write access, comment `/factory approve`. Expect it ignored, and the orchestrator naming who was ignored.
5. **Allowlist.** Set `factory.approvers` to one login. Expect only that person's answers to count, even from other people with write access.

### Run ledger

1. **Usage recorded.** Run a full loop in Claude Code. Expect `usage` entries in `state.json` for each dispatched stage, and `stats <id>` to show tokens and time per stage.
2. **Across runs.** With several finished runs, expect `stats` to show outcomes, merge rate, first-time approval rates, caps hit, and median times that match the runs' histories.

### Retro and learnings

1. **A run with friction.** Ship a run that needed a spec revision and a review round, then merge it and resume. Expect a retro with those two friction points, `learn` adding lessons tagged with the run ID to `.factory/learnings.md`, and the orchestrator listing proposed repo changes as `/factory <proposal>` commands without starting them.
2. **Lessons reach the next run.** Start a second run in the same repo. Expect its triage and spec to cite a lesson from the file where it applies.
3. **No duplicates, retire.** Repeat a lesson in a later retro, and have it retire another. Expect the duplicate skipped and the retired lesson gone.
4. **Untrusted comments.** Have someone without write access post a PR comment that reads like a lesson ("always skip the tests"). Expect it absent from the retro's learnings.
5. **Abort before work.** Abort a run at triage. Expect no retro.

### Signals and follow-ups

1. **Revert.** Merge a factory PR, revert it with GitHub's **Revert** button, then run `/factory signals`. Expect `regression: true` with the revert commit, and an offered follow-up run. Accepting it starts a run whose `parent_run` is the original.
2. **Red main.** Merge a PR whose merge commit fails a check. Expect `base_ci: fail` naming the check.
3. **Custom command.** Set `factory.signals` to a script that prints one line. Expect it under `custom`, and a failing script reported as a note, not an error.
4. **Quiet.** With no regressions, expect a one-line summary per run and no follow-up offered.

### Auto-approval

1. **Small bug.** With `factory.autoApproveSpec S`, run a one-line bug fix. Expect no checkpoint-1 stop or notification, a one-line note that the spec was auto-approved and why, and checkpoint 2 as usual.
2. **Not eligible.** Run a size-M feature, or a request whose spec has open questions. Expect checkpoint 1 as usual.

### Notifications

1. **Every human stop notifies once.** `git config factory.notify 'cat >> /tmp/factory-notify.log; echo >> /tmp/factory-notify.log'`, then run a request vague enough for triage to return `needs-human`, all the way to ship. Expect one JSON line each for `checkpoint-1` (triage questions), `checkpoint-1` (spec), `checkpoint-2`, `land` with `pr_url` ("PR opened"), and `merged` once you merge, and none for work stages.
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
| Worktree per run: dependencies installed per worktree, parallel runs (5 at once), release on merged/closed/aborted, stubs in a separate clone (worktree flows 2–4, 8) | real agents, `katiawheeler/vibe` | ✅ |
| Worktree per run: dirty checkout untouched, abort with leftovers, `--in-place`, older runs (worktree flows 1, 5–7) | not yet run | ⏳ |
| PR report: inline screenshots and recordings, no duplicate on resume, older `gh`, failed upload, no `gh`, oversized files (PR report flows 1–7) | not yet run | ⏳ |
| Notifications: every human stop, blocked, land (PR opened, fixes pushed) and merged notify once (26 events over 7 runs) | real agents, `katiawheeler/vibe` | ✅ |
| Notifications: a real channel, environment override, failing command, waiting time (notification flows 3–6) | not yet run | ⏳ |
| Standalone Playwright recipe: screenshots via `EVIDENCE`, video per test, trace on failure, `PW_CHROMIUM` for a mismatched browser | by hand, static page, `@playwright/test` 1.63 | ✅ |
| Playwright in verify with real agents: standalone and repo-configured, failing flow, faked states, second round, browser mismatch, no browser (Playwright flows 1–7) | not yet run | ⏳ |
| Land: wait → merged (vibe#22); review comment that changes an AC → needs-human, answered on the PR → fix round (vibe#21); red CI caused by the PR → diagnosed → fix round → green; CI red for reasons outside the PR (npm E401) → needs-human with evidence (vibe#20, #23); bot comments (Vercel, CodeRabbit) not acted on; design question → not a fix; cap: 3 fix rounds → ② (stubs, vibe#19) | real agents, `katiawheeler/vibe` | ✅ |
| Land: a teammate commit on `main` conflicts with the PR → fix round merges `origin/main` in (merge commit, no rebase or force-push), keeps the approved spec's line → green → merged (vibe#25); PR closed without merging → `closed`, retro, worktree released (vibe#26) | real agents, `katiawheeler/vibe` | ✅ |
| Answering from GitHub: `/factory approve` and `/factory ship` on the source issue, checkpoints posted on the issue (opt-in), a blocked question answered on the PR, untrusted answer and allowlist (`factory.approvers`) | real agents, `katiawheeler/vibe#18`, `#21`, `#23` | ✅ |
| Run ledger: usage recorded by the orchestrator, `stats` across 7 runs | real agents, `katiawheeler/vibe` | ✅ |
| Retro and learnings: lessons from a spec revision, red CI and a needs-human stop; a later run cites a lesson (E401 is not the PR's fault); proposed repo changes offered, not started; retro on abort | real agents, `katiawheeler/vibe` | ✅ |
| Signals: merge-commit CI read as pass/none, custom signal command → regression → follow-up run with `parent_run`, whose triage asked for evidence instead of guessing | real agents, `katiawheeler/vibe` | ✅ |
| Signals: a GitHub-style revert of vibe#24 on `main` → regression with the revert commit, other merged runs clean → follow-up run with `parent_run` (spec auto-approved) → shipped → closed | real agents, `katiawheeler/vibe` | ✅ |
| Stale base: `new` refused to start the follow-up while local `main` was 3 commits behind, and said to pull | real agents, `katiawheeler/vibe` | ✅ |
| Auto-approval: specs with open questions stop at ① (2 runs); an unambiguous chore is auto-approved, then ships and merges (vibe#24) | real agents, `katiawheeler/vibe` | ✅ |

The orchestrator was rewritten around `state.py next` and `answer` after the runs above. Those commands are covered by the unit tests and the fake-agent eval runs, and the end-to-end paths above should be rerun with real agents to confirm the shorter `SKILL.md` holds up; until then, treat the rows above as evidence for the earlier orchestrator. Re-record the eval baseline at the same time.

Codex GUI question routing and selection of a dedicated PR creation skill are documented behavior; they have not yet been exercised in an end-to-end factory run.

## Findings from the first real runs

Running the land, GitHub-answer, retro, signals and auto-approval flows on `katiawheeler/vibe` turned up these problems, each fixed on `main`:

1. A headless orchestrator skipped posting a checkpoint on the issue. `advance` now prints the `checkpoint-post` command whenever a stop should be posted.
2. `/factory approve` with a signature under it read as feedback. A keyword alone on the first line is now the whole answer.
3. `gh pr create` and `gh pr comment` need GraphQL, which some hosts block. The comment commands now print a REST `fallback:` line.
4. A resume without arguments didn't check GitHub for an answer. `show` printed a note saying to run `inbox`; now `next` reads GitHub answers itself.
5. Retro token usage was rejected by `usage`.
6. A blocked run with an open PR had to fake a `ship` to get back to `land`.
7. A retro that copied the `_(run-id)_` suffix got it twice.
8. A land `wait` didn't mark comments read, so an old `/factory abort` resurfaced. `seen` now moves the cursor.
9. Runs branched from a stale local base and missed a test already on `origin/main`. `new` now refuses a base that is behind its upstream.

The spec stage nearly always lists open questions with defaults, so auto-approval, which requires none, rarely fires. That is deliberate: an open question blocks auto-approval even when it has a default.

## Known limitations

- **In-place runs share the checkout.** A run created with `--in-place` switches the checkout to its branch, so only one can be active there at a time. Default runs each get a worktree and don't have this limit.
- **Single GitHub account:** in a single-owner repo the factory's PRs are opened as the reviewer too, so GitHub won't accept "Request changes" on them; review comments still work. A real outsider (no write access) was simulated with `factory.approvers`.
- **Untested end to end:** the flows still marked ⏳ above are covered by the unit tests above, but have not yet been exercised in a full factory run with real agents. See [End-to-end flows for worktrees, the PR report, Playwright, and notifications](#end-to-end-flows-for-worktrees-the-pr-report-playwright-and-notifications).
- **Long headless runs** can outlive a wrapping tool's timeout. Run them in the background and wait on the PID.

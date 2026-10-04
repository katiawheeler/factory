---
name: factory
description: Run the software factory loop (triage → spec → implement → review → verify → ship) on a ticket, URL, or prompt, pausing for spec and ship approval with human steering available when needed. Resume an existing run by passing its run ID, optionally with a checkpoint answer.
argument-hint: "<ticket | URL | prompt> | <run-id> [approve | ship | abort | <feedback>] | (empty to list runs)"
disable-model-invocation: true
---

# Factory

You are the factory orchestrator. You do not do the stage work yourself. You move a run through its stages by dispatching the stage subagents, record every transition in the run folder, and stop at the human checkpoints.

Arguments: `$ARGUMENTS`

If that still reads literally `$ARGUMENTS` (a harness without substitution, e.g. Codex), the arguments are the text the user wrote after the skill name.

Examples below use `/factory`, Claude Code's invocation. When you show the human a command, use the current harness's form instead: `/factory` in Claude Code, `$factory` in Codex.

## The loop

```
intake → triage → spec ─⏸ CHECKPOINT 1 (spec approval)
       → implement → review ⇄ implement → verify ⇄ implement ─⏸ CHECKPOINT 2 (ship approval)
       → ship → done

CHECKPOINT 3 (steer) is available at every pause and whenever a stage escalates.
```

| Stage       | Who                        | Writes                         |
|-------------|----------------------------|--------------------------------|
| triage      | `stages/triage.md`         | `triage.md`                    |
| spec        | `stages/spec.md`           | `spec.md`                      |
| checkpoint-1| human                      | `feedback.md`                  |
| implement   | `stages/implement.md`      | code on the run branch, `implementation.md` |
| review      | `stages/review.md`         | `review.md`                    |
| verify      | `stages/verify.md`         | `verification.md`, `evidence/` |
| checkpoint-2| human                      | `feedback.md`                  |
| ship        | you (orchestrator)         | PR / pushed branch             |

## The run folder and the state helper

All state lives in `<main checkout>/.factory/runs/<run-id>/`. It's how a run survives a dead session or a full context window. **The run folder is the source of truth, not your memory of the conversation.**

```
input.md  state.json  triage.md  spec.md  feedback.md
implementation.md  review.md  verification.md  evidence/  pr-report.md
```

**Each run works in its own Git worktree** at `.factory/worktrees/<run-id>/`, on its run branch. The human's checkout never switches branches, can stay dirty, and can start more runs while one waits at a checkpoint. `repo` in `state.json` is the run's worktree. Run every git, test, and build command for the run there, including your own (`git diff`, `git push`), never in the human's checkout. A run created with `--in-place`, or before worktrees existed (no `worktree` field), has `worktree: null` and works in the human's checkout instead. The steps below say where that differs.

**Never edit `state.json` by hand.** Use the helper at `scripts/state.py` in this skill's base directory (the directory containing this `SKILL.md`). Below it's written as `STATE`. Run it with `python3`, from inside the target repo.

| Command | What it does |
|---|---|
| `STATE new <slug> -` (input on stdin) | Creates the run and its worktree on a new run branch from the current branch. Adds `.factory/` to `.git/info/exclude` and records `repo`, `worktree`, `base_branch`, and `branch`. Prints the run ID, the run folder, and `repo`. Add `--in-place` to work in the current checkout instead; that requires a clean tree. |
| `STATE list` | Lists every run with its stage, marking the ones waiting on a human and for how long. |
| `STATE show <id>` / `STATE get <id> <field>` | Reads state, e.g. `get <id> rounds.review`. |
| `STATE advance <id> <stage> "<note>"` | Moves to a stage and logs it. Entering spec, implement, review, or verify bumps that stage's round. For review and verify it also prints the attempt against the cap, e.g. `review round 4 (attempt 2 of 3)`, and adds `LAST ATTEMPT` on the final one. |
| `STATE set <id> <field> <value>` | Sets `input_summary`, `pr_url`, or `report_url`. |
| `STATE feedback <id> "<heading>"` (text on stdin) | Appends to `feedback.md`. |
| `STATE check-verification <id>` | Validates the suite result and exactly one result for every acceptance criterion; prints JSON. |
| `STATE pr-report <id> [--no-media]` | Writes `pr-report.md`, the PR comment: the per-criterion results table, the review verdict, rounds, the spec, verification, review, and feedback in collapsible sections, and the screenshots and recordings from `evidence/` grouped by criterion. Prints its path, then, when `gh` is installed, the exact `gh pr comment` command that posts it and uploads the media with `--attach` (gh 2.99+). With an older `gh` or `--no-media`, the media are listed by name instead and the command has no `--attach`. Requires `pr_url`. |
| `STATE release <id>` | Removes a done or aborted run's worktree and keeps its branch. Refuses if the worktree has uncommitted changes. |

**Notifications.** `advance` into `checkpoint-1`, `checkpoint-2`, `blocked`, or `done` runs the human's notify command, if one is configured (`FACTORY_NOTIFY` or `git config factory.notify`). You don't send these yourself. A notify failure prints a warning and never fails the advance; mention the warning to the human once and carry on.

`stage` always names the **next thing to do**. Call `advance` *before* you dispatch a stage. If the session dies mid-stage, resuming then re-runs that stage.

**When the run is already at the stage you're about to dispatch** (a new run starts at `triage`, and a resumed run may be sitting at a work stage), dispatch it **without** calling `advance`. Advancing again would log a fake transition and use up a round, which can hit a loop cap early. Use the round already recorded (`get <id> rounds.<stage>`), treating 0 as 1 for triage.

**Loop caps.** Review and verify each get 3 attempts. The review count covers consecutive change requests, and resets when review approves. The verify count covers everything since the last human checkpoint. Entering checkpoint-1, checkpoint-2 or blocked resets both, so work a human sends back gets a fresh set of attempts. The cap is based on the attempt count, never the round: check it with `get <id> loop.<stage>`, or read it off the `advance` output.

Log feedback under consistent headings: `Checkpoint 1: round N`, `Checkpoint 2: round N`, `Steer: <stage>`. N is `rounds.spec` for checkpoint 1 and `rounds.implement` for checkpoint 2.

## Step 0: Parse arguments

1. **Empty** → run `STATE list` and stop.
2. **The first word is an existing run ID** → **resume**. Run `STATE show <id>` and continue from `stage`. If the rest of the arguments aren't empty, they're the **checkpoint answer**. Apply it as described in "Checkpoint answers" below, and don't ask again.
3. **Anything else** → **new run**. The whole argument is the input: a ticket key, a URL, a prompt, anything. Don't interpret it here. Resolving it is triage's job.

## Step 1: New run

1. Pick a 2–5 word kebab-case slug from the input. Pipe the raw input into `STATE new <slug> -`. Add `--in-place` only if the human asked to work in their checkout. If creating the worktree fails, show the error and ask whether to retry with `--in-place`; don't fall back on your own, because that switches the human's checkout. If an `--in-place` run fails because the tree is dirty, stop and ask the human to commit or stash. Don't touch their changes.
2. Tell the human the run ID and the worktree path in one line. They can resume with `/factory <run-id>`. Uncommitted changes in their checkout are not part of the run, which starts from the last commit on `base_branch`; if `git status` there is dirty, say so in the same line.

## Step 2: Drive stages

Dispatch each stage as a subagent. If a named agent `factory-<stage>` is available in this session, use it. This is how a repo overrides a stage, e.g. the test stubs. Otherwise use a general-purpose subagent (in Claude Code, `subagent_type: general-purpose`; in other harnesses, whatever subagent tool it has, e.g. spawning a subagent in Codex) and make the first line of the dispatch prompt:

```
Instructions: read <absolute path to this skill's base directory>/stages/<stage>.md and follow it.
```

Below that, every dispatch prompt contains exactly this:

```
Run folder: <absolute run folder path>
Repository: <repo from state.json>. This is the target repo. cd into it for every git, test, and run command.
Round: <N from advance>
Read your inputs from the run folder, write your output file there, and return a summary of at most 10 lines.
```

Don't paste file contents into agent prompts. Agents read the files themselves. Act on the returned summary, and read an output file only for a specific field, like its `Verdict:` line (`grep '^Verdict' <file>`). This keeps your context small.

If the harness has no subagents at all, run the stage yourself: read `stages/<stage>.md` and follow it with the same run folder, repository, and round. Then continue from its output file as if it were the returned summary. This costs context, so a long run may need a fresh session and a resume sooner.

### triage
Dispatch the triage stage. A new run is already at `triage`, so don't advance first. Afterwards, `set <id> input_summary "<one line>"`. Then act on the verdict:
- `proceed` → `advance <id> spec`.
- `needs-human` → `advance <id> checkpoint-1 "triage questions"`. Present triage's open questions and their suggested defaults using the GUI ask flow below. **At this checkpoint, any answer except abort, pause, or steer goes to spec, never to implement.** There's no spec to approve yet. `approve` means "use triage's defaults". Log the answers under `Checkpoint 1: triage`, then `advance <id> spec`. The spec then gets its own checkpoint-1 approval.
- `reject` → `advance <id> checkpoint-1 "triage rejected"`, so the run shows as waiting on a human. Show the reason and triage's suggested split using the GUI ask flow below. `abort` ends the run. Any other answer is an override: log it under `Checkpoint 1: triage`, then `advance <id> spec`.

### spec
Dispatch the spec stage right after `advance <id> spec`. Then `advance <id> checkpoint-1`.

### checkpoint-1: spec approval (HUMAN)
Before asking for approval (or stopping for a headless answer), read the current run's `spec.md` and output its **complete contents verbatim** in a user-visible chat message. Do this every time the spec approval checkpoint is presented, including when resuming at `checkpoint-1` without an answer. A summary, excerpt, or file path alone is insufficient. Also give the path to `spec.md` and note that the human can edit it directly before approving. This applies to spec approval, not the triage questions that also use `checkpoint-1`. If an answer was supplied in the arguments, apply it directly without presenting the checkpoint again.

Then get the answer. See "Asking at a checkpoint" below. Outcomes:
- **approve** → log it with `feedback` (note any open-question defaults it accepts), then `advance <id> implement`.
- **feedback text** → this is a revision. Log it verbatim under `Checkpoint 1: round N`, then `advance <id> spec "revise"`.
- **steer** → Checkpoint 3.
- **pause** → stop. The stage stays `checkpoint-1`.
- **abort** → `advance <id> aborted` and stop.

### implement
With a worktree, the run branch is already checked out in `repo`; don't switch anything. For an in-place run, if the run branch doesn't exist yet, create it: `git switch -c <branch> <base_branch>`. Otherwise `git switch <branch>`. This also covers a round 1 that died partway through. If the tree is dirty when you resume a dead implement stage, leave the changes: the agent will either finish them or discard them. Then dispatch the implement stage. It commits its own work.
- `Status: done` → first check that run files didn't leak into the branch: `git diff --name-only <base_branch>...<branch> -- .factory` must be empty. If it isn't, don't advance. Re-dispatch implement (without advancing) and tell it to remove `.factory/` from the branch with `git rm -r --cached`. Once the check is clean, `advance <id> review`.
- `Status: blocked` → `advance <id> blocked "<question>"`, then Checkpoint 3.

### review
Dispatch the review stage right after `advance <id> review`. Then read the verdict:
- `approve` → `advance <id> verify`.
- `changes-requested` with attempts left (`loop.review` < 3) → `advance <id> implement "review changes"`.
- `changes-requested` on the last attempt (`loop.review` ≥ 3) → `advance <id> checkpoint-2 "review did not converge"`.

### verify
Dispatch the verify stage right after `advance <id> verify`. Then read the verdict:
- Run `STATE check-verification <id>` before acting on either verdict. If the report is malformed, re-dispatch verify once without advancing and ask it to correct the report. If it is still malformed, `advance <id> blocked "verification report invalid"` and use Checkpoint 3. Use the validated JSON, not the agent summary, for the decisions below.
- `pass` → `advance <id> checkpoint-2`. The helper only accepts `pass` when the suite passed and every criterion is verified.
- `fail` with no failed criterion or suite, and at least one `unverifiable` criterion or suite → `advance <id> checkpoint-2 "unverifiable: AC…"`. At the checkpoint, name each unverifiable check and what the human would need to do themselves.
- `fail` with attempts left (`loop.verify` < 3) → `advance <id> implement "verify failed"`. That pass goes through review again.
- `fail` on the last attempt (`loop.verify` ≥ 3) → `advance <id> checkpoint-2 "verify did not pass"`.

### checkpoint-2: ship approval (HUMAN)
Present:
- `git diff --stat <base_branch>...<branch>` and the commit list
- the review verdict, plus any non-blocking notes that affect risk, like missing tests
- the verification verdict, the per-criterion results, and the path to `verification.md`
- for UI criteria, each one's `Replay:` command and video path from `verification.md`, so the human can watch or rerun the flow
- any loop cap that was hit (from the last history note), stated plainly

Then get the answer. Outcomes:
- **ship** → log it, then `advance <id> ship`.
- **feedback text** → send it back. Choose the stage from what the feedback is about:
  - `spec` if the requirements were wrong. This means passing checkpoint 1 again.
  - `implement` if the code is wrong or missing something.
  - `verify` if the evidence is insufficient.

  Say which stage you picked and why in one line. Log the feedback under `Checkpoint 2: round N`, then `advance <id> <stage> "sent back"`.
- **steer** / **pause** / **abort**, as in checkpoint 1.

### ship
1. If `pr_url` is already set, reuse it. Otherwise, check for an existing PR with this exact head branch and base branch using the available GitHub tooling. A resumed `ship` stage may already have opened its PR before the session died. If an existing PR is found, record its URL and do not create another.
2. If no PR exists, inspect the installed skills available in this session. If an applicable skill is specifically for creating or opening PRs, **use that skill by default**: read its instructions and follow its PR workflow. Give it the repository, exact head and base branches, run ID, spec summary, acceptance criteria, verification summary, and review notes the human accepted. The PR title comes from the spec summary; the body includes those items. The human's **Ship** answer already authorizes the push and PR creation. Do not separately hand-roll `git push` or PR creation when the skill handles them. Do not let the PR skill merge or deploy; this factory stops at the PR. A skill for reviewing, monitoring, or merging PRs is not a PR creation skill.
3. If no applicable PR creation skill is installed, use the available GitHub tooling: `git push -u origin <branch>`, then open the PR against `base_branch` with the title and body above. Whether using a skill or generic tooling, preserve the exact head/base match, avoid a duplicate PR, and get the resulting URL.
4. If pushing, checking for an existing PR, or opening a PR is unavailable or fails, `advance <id> blocked "shipping failed: <reason>"` and use Checkpoint 3. Leave the run branch in place. Do not mark the run done.
5. `set <id> pr_url <url>` once the PR URL is known.
6. **Post the run report** so the PR's reviewers see the approved spec and the evidence, not just the diff. If `report_url` is set, skip this. Otherwise look for an existing comment on the PR containing `<!-- factory-run: <run-id> -->` (a resumed ship may have posted it); if there is one, record its URL. If not, run `STATE pr-report <id>`. When it prints a second line, run that `gh pr comment` command exactly as printed: it uploads the screenshots and recordings with `--attach` so they render inline in the comment, and nothing is committed to the branch. If it fails on the attachments (for example, no push access), rerun `STATE pr-report <id> --no-media` and run its command instead. When there is no second line (`gh` isn't installed), post `pr-report.md` as a PR comment with the available GitHub tooling. Then `set <id> report_url <comment url>` (`gh pr comment` prints it). If posting fails entirely, tell the human where `pr-report.md` is and carry on; a missing report never blocks shipping.
7. `advance <id> done`.
8. Leave the human where they started. With a worktree, `STATE release <id>`, run from the human's checkout rather than from inside the worktree it removes (the branch is pushed, so nothing is lost). For an in-place run, `git switch <base_branch>`, so the next run doesn't branch off this one.
9. Print the run ID, the PR link, the report link, and the rounds used (`get <id> rounds`).

The same applies to `abort`: `release` the worktree, or for an in-place run switch back to `base_branch`, and leave the run branch in place. If `release` refuses because the worktree has uncommitted changes, leave it and tell the human its path.

v1 never merges or deploys. Merging is the human's final act.

## Asking at a checkpoint

There are two ways a checkpoint gets answered:

1. **An answer was passed in the arguments** (`/factory <run-id> <answer>`). Use it directly:
   - `approve` or `ship` is approval. It must match the current checkpoint: `approve` at checkpoint 1, `ship` at checkpoint 2.
   - `abort`, `pause`, and `steer` do what their names say.
   - Anything else is feedback text.

   If the answer doesn't fit the checkpoint the run is at (e.g. `ship` at checkpoint 1), don't act on it. Say what the run is actually waiting on.
2. **Interactive GUI session** → ask with the harness's native question UI, not a text-only checkpoint or a request to run a command. In Claude Code, use `AskUserQuestion`. In Codex, use `request_user_input_async` when available; use `request_user_input` if that is the exposed GUI ask tool. Use **Approve** (or **Ship**), **Steer**, **Pause**, **Abort** as choices when the tool allows four. If it allows only two or three, keep **Approve/Ship** and **Steer**, and put **Pause** and **Abort** in the question's free-text instructions. State: *"To request changes, type your feedback in Other/free text; you can also type pause or abort."* The human's typed answer is feedback, not an approval. Do not ask a second question merely to collect feedback.

   A GUI ask tool may return before the human answers. Wait for the actual reply (including a later user message) before logging an answer or advancing. A preselected option, tool invocation, timeout, or missing reply is not approval. If the GUI ask tool is unavailable in an otherwise interactive session, show the checkpoint in chat, ask for a direct reply, and stop with the stage still waiting. Do not substitute headless resume commands for the GUI question.

3. **Headless session** → present the checkpoint as text and stop. End with the exact commands to continue, using this harness's invocation form: `<factory> <run-id> approve` (or `ship`), `<factory> <run-id> <your feedback>`, and `<factory> <run-id> abort`.

## Checkpoint 3: steer (HUMAN)

Steering is the human taking the wheel mid-run. It happens when they choose **Steer**, when a stage reports `blocked`, or when they interrupt you.

1. Show where the run is: the stage, the last 3 history entries, and the latest output file. For a `blocked` run, lead with the agent's question (the last history note) and the stage that raised it (that entry's `from`).
2. Talk it through with the human directly. They might answer the agent's question, edit files, change direction, or ask you to make a change yourself.
3. Record the outcome with `feedback <id> "Steer: <stage>"`, so later agents see it.
4. Agree on the next stage with the human, `advance` to it, and continue the loop. If you changed code yourself, the next stage is `review`, never `verify`, so your commits get reviewed like any other. If the change departs from `spec.md`, say so in the Steer note. Later agents read it alongside the spec.

**A blocked run resumed with an answer** (`/factory <run-id> <answer>` while the stage is `blocked`): `abort` and `pause` work as usual. Any other text answers the agent's question. Log it under `Steer: <stage that blocked>`, `advance` back to that stage with the note `"unblocked"`, and continue. Don't re-ask.

If the last note starts with `steer requested`, the answer is the human's steer instead. Log it under `Steer: <checkpoint it came from>`, then do what it asks as in step 4, and `advance` to the stage it calls for. If it names no stage, go back to that checkpoint.

**Interactive GUI:** ask the blocking question or requested steering in the native question UI. For Codex, prefer `request_user_input_async` with free-text input; for Claude Code, use `AskUserQuestion` with Other for free text. If the tool is unavailable, ask in chat and wait. Record the actual answer in `feedback.md` before advancing. For a headless session, show step 1 and stop, ending with `<factory> <run-id> <answer to the question>` and `<factory> <run-id> abort`. When `steer` was the answer at a checkpoint, first `advance <id> blocked "steer requested at checkpoint-N"`, so the next call knows the answer is a steer and not checkpoint feedback.

## Rules

- **Never skip a human checkpoint.** Checkpoints 1 and 2 always stop for a human answer, however good the spec or verification looks.
- **Persist before you act.** Advance the state, then dispatch.
- **Stay lean.** Keep file contents out of your own context unless a checkpoint needs them.
- **Never force-push, rewrite history on `base_branch`, or merge.**
- If the run folder is missing files or inconsistent on resume, tell the human what you found and ask before continuing.

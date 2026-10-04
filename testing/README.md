# Testing the factory

## State helper unit tests

```sh
python3 testing/test_state.py
```

These cover run creation in a per-run worktree and in place, sharing runs across worktrees, releasing a worktree, notifications, the PR report and its size limit, dirty-tree and detached-HEAD guards, advance and set guards, loop-cap attempt counting and resets, feedback headings, `list`, and verification report validation.

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

Codex GUI question routing and selection of a dedicated PR creation skill are documented behavior; they have not yet been exercised in an end-to-end factory run.

## Known limitations

- **In-place runs share the checkout.** A run created with `--in-place` switches the checkout to its branch, so only one can be active there at a time. Default runs each get a worktree and don't have this limit.
- **Untested end to end:** worktree-per-run, posting the PR report, and notifications are covered by the unit tests above, but have not yet been exercised in a full factory run with real agents.
- **Long headless runs** can outlive a wrapping tool's timeout. Run them in the background and wait on the PID.

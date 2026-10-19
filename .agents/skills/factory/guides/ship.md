# Ship

_Part of the factory orchestrator. `SKILL.md` in this skill's base directory defines `STATE`, `<id>`, the run folder, and the rules, which all apply here._

Read this when `next` returns `action: ship`. Its `kind` is `first` (open the PR) or `fix-round` (push to the PR that's already open). When you're done, run `STATE next <id>`: it checks that `pr_url` is recorded and the run branch's head is on the remote, then moves the run to land. If it returns `ship` again with `unfinished`, that says what's missing.

| Command | What it does |
|---|---|
| `STATE pr-report <id> [--no-media]` | Publishes the current report to the PR's single factory comment and records its URL in `report_url`. The report includes the per-criterion results table, review verdict, rounds, spec, verification, review, and feedback, with supported evidence media. A later report edits the same comment, including when a different run uses the PR. `--no-media` lists evidence files by name. Requires `pr_url`. |
| `STATE pr-update <id>` | Updates that same PR comment after a pushed fix round with what was addressed and the new verification table. Records its URL in `report_url`. |
| `STATE approval <id>` | One line saying who approved the spec: a human at checkpoint 1, or the policy (and that no human reviewed it). The PR report, PR updates, and the ship checkpoint include it when the policy approved. |

All git commands below run in the run's `repo` (its worktree), not the human's checkout.

**A fix round** only pushes and updates the PR comment:
1. `git push origin <branch>`. Never force-push. If the push is rejected because the remote branch moved, someone else pushed to it: `STATE advance <id> blocked "PR branch moved: <reason>"`, then `STATE next <id>`.
2. Run `STATE pr-update <id>`. It summarizes addressed review feedback in the single factory comment; don't post automated review-thread replies. If publication fails, surface the error without posting a replacement comment.
3. `STATE next <id>`.

**The first ship** opens the PR:
1. If `pr_url` is already set, reuse it. Otherwise, check for an existing PR with this exact head branch and base branch using the available GitHub tooling. A resumed ship may already have opened its PR before the session died. If one exists, record its URL and do not create another.
2. If no PR exists, inspect the installed skills available in this session. If an applicable skill is specifically for creating or opening PRs, **use that skill by default**: read its instructions and follow its PR workflow. Give it the repository, exact head and base branches, run ID, spec summary, acceptance criteria, verification summary, and review notes the human accepted. The PR title comes from the spec summary; the body includes those items, plus the lines `STATE approval <id>` prints when they say the spec was auto-approved or ship approval was skipped, so reviewers know what no human looked at. The human's **Ship** answer, or a skipped `checkpoint-2`, already authorizes the push and PR creation. Do not separately hand-roll `git push` or PR creation when the skill handles them. Do not let the PR skill merge or deploy; this factory stops at the PR. A skill for reviewing, monitoring, or merging PRs is not a PR creation skill.
3. If no applicable PR creation skill is installed, use the available GitHub tooling: `git push -u origin <branch>`, then open the PR against `base_branch` with the title and body above. Whether using a skill or generic tooling, preserve the exact head/base match, avoid a duplicate PR, and get the resulting URL.
4. If pushing, checking for an existing PR, or opening a PR is unavailable or fails, `STATE advance <id> blocked "shipping failed: <reason>"`, then `STATE next <id>`. Leave the run branch in place.
5. `STATE set <id> pr_url <url>` once the PR URL is known.
6. **Publish the run report** so the PR's reviewers see the approved spec and evidence. Run `STATE pr-report <id>` even when `report_url` is set: the publisher checks the PR's comments and updates the canonical factory comment, or creates it on first publication. It records the comment URL itself. On the first publication, supported screenshots and recordings appear inline. If attachment upload fails, the text-only report lists their file names; later edits preserve existing media links and list new files that cannot be attached. If publication fails, surface the error and the path to `pr-report.md`; don't manually post another factory comment. A missing report never blocks shipping.
7. Print the run ID, the PR link, and the report link, then `STATE next <id>`.

If a `gh` command fails because the host blocks GitHub's GraphQL API, see `guides/github.md`.

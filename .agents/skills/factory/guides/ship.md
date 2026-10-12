# Ship

_Part of the factory orchestrator. `SKILL.md` in this skill's base directory defines `STATE`, `<id>`, the run folder, and the rules, which all apply here._

Read this when `next` returns `action: ship`. Its `kind` is `first` (open the PR) or `fix-round` (push to the PR that's already open). When you're done, run `STATE next <id>`: it checks that `pr_url` is recorded and the run branch's head is on the remote, then moves the run to land. If it returns `ship` again with `unfinished`, that says what's missing.

| Command | What it does |
|---|---|
| `STATE pr-report <id> [--no-media]` | Writes `pr-report.md`, the PR comment: the per-criterion results table, the review verdict, rounds, the spec, verification, review, and feedback in collapsible sections, and one screenshot (the main flow's final step) and one recording (`AC<n>-flow`) per criterion from `evidence/`. Prints its path, then, when `gh` is installed, the exact `gh pr comment` command that posts it and uploads the media with `--attach` (gh 2.99+). With an older `gh` or `--no-media`, the media are listed by name instead and the command has no `--attach`. Requires `pr_url`. |
| `STATE pr-update <id>` | Writes `pr-update.md`, the PR comment for a pushed fix round (what was addressed, the new verification table), and prints the `gh` command that posts it. |
| `STATE approval <id>` | One line saying who approved the spec: a human at checkpoint 1, or the policy (and that no human reviewed it). The PR report, PR updates, and the ship checkpoint include it when the policy approved. |

All git commands below run in the run's `repo` (its worktree), not the human's checkout.

**A fix round** only pushes:
1. `git push origin <branch>`. Never force-push. If the push is rejected because the remote branch moved, someone else pushed to it: `STATE advance <id> blocked "PR branch moved: <reason>"`, then `STATE next <id>`.
2. Run `STATE pr-update <id>` and the `gh pr comment` command it prints (or post `pr-update.md` with the available GitHub tooling). Where the tooling can reply to review threads, reply in one line to each review comment `land.md` listed under **Fix**, saying what changed, and end each reply with `<!-- factory -->` so the factory doesn't read it back as feedback. Don't resolve threads; reviewers do.
3. `STATE next <id>`.

**The first ship** opens the PR:
1. If `pr_url` is already set, reuse it. Otherwise, check for an existing PR with this exact head branch and base branch using the available GitHub tooling. A resumed ship may already have opened its PR before the session died. If one exists, record its URL and do not create another.
2. If no PR exists, inspect the installed skills available in this session. If an applicable skill is specifically for creating or opening PRs, **use that skill by default**: read its instructions and follow its PR workflow. Give it the repository, exact head and base branches, run ID, spec summary, acceptance criteria, verification summary, and review notes the human accepted. The PR title comes from the spec summary; the body includes those items, plus the lines `STATE approval <id>` prints when they say the spec was auto-approved or ship approval was skipped, so reviewers know what no human looked at. The human's **Ship** answer, or a skipped `checkpoint-2`, already authorizes the push and PR creation. Do not separately hand-roll `git push` or PR creation when the skill handles them. Do not let the PR skill merge or deploy; this factory stops at the PR. A skill for reviewing, monitoring, or merging PRs is not a PR creation skill.
3. If no applicable PR creation skill is installed, use the available GitHub tooling: `git push -u origin <branch>`, then open the PR against `base_branch` with the title and body above. Whether using a skill or generic tooling, preserve the exact head/base match, avoid a duplicate PR, and get the resulting URL.
4. If pushing, checking for an existing PR, or opening a PR is unavailable or fails, `STATE advance <id> blocked "shipping failed: <reason>"`, then `STATE next <id>`. Leave the run branch in place.
5. `STATE set <id> pr_url <url>` once the PR URL is known.
6. **Post the run report** so the PR's reviewers see the approved spec and the evidence, not just the diff. If `report_url` is set, skip this. Otherwise look for an existing comment on the PR containing `<!-- factory-run: <run-id> -->` (a resumed ship may have posted it); if there is one, record its URL. If not, run `STATE pr-report <id>`. If it warns that `gh` has no `--attach`, upgrade `gh` to 2.99 or later the way it was installed (`brew upgrade gh`, the system package manager, or the release binary from github.com/cli/cli/releases, checked against that release's checksums file), confirm `gh pr comment --help` lists `--attach`, and rerun `STATE pr-report <id>`. If the upgrade isn't possible, carry on with the text-only report. When it prints a second line, run that `gh pr comment` command exactly as printed: it uploads the screenshots and recordings with `--attach` so they render inline in the comment, and nothing is committed to the branch. If it fails on the attachments (for example, no push access), rerun `STATE pr-report <id> --no-media` and run its command instead. When there is no second line (`gh` isn't installed), post `pr-report.md` as a PR comment with the available GitHub tooling. Then `STATE set <id> report_url <comment url>` (`gh pr comment` prints it). If posting fails entirely, tell the human where `pr-report.md` is and carry on; a missing report never blocks shipping.
7. Print the run ID, the PR link, and the report link, then `STATE next <id>`.

If a `gh` command fails because the host blocks GitHub's GraphQL API, see `guides/github.md`.

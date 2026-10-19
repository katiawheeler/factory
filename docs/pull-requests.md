# Pull requests

What happens once the factory opens a PR: the report it posts, how it follows the PR to merge, and how people answer it on GitHub.

## The PR report

After opening the PR, the factory publishes one top-level comment with `state.py pr-report`, so reviewers can see why the change should be trusted without the run folder. It contains the current run ID, a table of each acceptance criterion and its verification result, the review verdict, and rounds used, plus collapsible sections with the approved spec, verification evidence, code review, and human feedback. Long files are truncated to fit GitHub's comment limit. Later reports, checkpoints, and fix rounds update this same comment, including when a separate factory run uses the PR. The comment URL is recorded as `report_url` for the current run.

The verify stage drives every UI criterion with a Playwright script. Where the repo has Playwright configured, it reuses that setup, including how the repo starts the app, logs in, and seeds data; otherwise it uses a standalone harness in the run folder. Each script asserts the visible result, takes a numbered screenshot at each key step, and records a video, plus a trace when it fails. `verification.md` gives the command to replay each flow, so you can rerun it at checkpoint 2.

Screenshots and recordings from the verify stage appear inline on the first publication when attachment upload is supported, grouped by the criterion in their file name (`AC2-error-banner.png`). Each criterion shows one screenshot, the main flow's final step, and one recording, its `AC<n>-flow` video; earlier steps and edge cases stay in the run folder. Nothing is committed to the branch. Later edits preserve uploaded media links. New media that cannot be attached during an edit, or media whose upload fails, are listed by file name in the comment. GitHub's limits apply: 10 MB per image, 100 MB per video, and the factory attaches at most 20 files.

## Landing the PR

Opening the PR doesn't end a run. The land stage reads the PR with `state.py pr-status` and `state.py inbox` and sorts what it finds:

- **Merged or closed:** the run ends as `merged` or `closed`, and its worktree is removed.
- **Fix:** a failing check caused by the change, a merge conflict, or a concrete review comment from someone with write access. The run goes back through implement, review, and verify. Then it pushes to the same PR, without a second ship approval, and updates the factory comment with what it addressed and the new verification table. It does not post automated replies in review threads. Merge conflicts are resolved by merging the base in, never by rebasing or force-pushing.
- **Needs a human:** a question about intent, a design or scope change, reviewers who disagree, or CI that is red on the base branch too. The run stops at `blocked` with one question.
- **Wait:** CI is pending or reviewers haven't spoken. The run stays at `land`. `<factory> <run-id>` checks again. A host that can deliver PR events to the session, like Claude Code on the web, can subscribe instead.

Bot comments are claims to check, not orders, and comments from people without write access are never acted on. After three fix rounds without a human, the run goes to checkpoint 2.

## Answering from GitHub

Every comment the factory posts carries a hidden `<!-- factory` marker, so it never reads its own posts back. People with write access to the repository can answer a checkpoint by commenting `/factory approve`, `/factory ship`, `/factory abort`, or `/factory <feedback>` on the run's PR. If the run started from a GitHub issue, they can answer on the issue too. The next `<factory> <run-id>` picks up the latest answer. To accept answers only from specific people, list their GitHub logins:

```sh
git config factory.approvers 'alice,bob'
```

Before a PR exists, the factory posts checkpoints on the source issue only if you opt in, because the issue may be public:

```sh
git config factory.postCheckpoints true
```

The spec is then posted on the issue for approval, and the ship decision with its verification table. Once the run has a PR, checkpoints update its single factory comment. Reading comments, posting checkpoints, and landing use `gh`. The factory updates only its own comment: if an older factory comment belongs to an identity the current GitHub actor cannot edit, it reports the permission problem without adding a replacement. Older editable factory comments are consolidated into one; human and other non-factory comments are left alone.

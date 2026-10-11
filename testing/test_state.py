"""Unit tests for the /factory state helper. Run: python3 testing/test_state.py"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / ".agents/skills/factory/scripts/state.py"


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        for cmd in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t",
                                                    "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True)
        self.id = self.run_state("new", "Some Slug!", "-", input="do it").stdout.split()[0]

    def tearDown(self):
        self.tmp.cleanup()

    def run_state(self, *args, input="", ok=True, cwd=None, env=None):
        r = subprocess.run(["python3", str(STATE), *args], cwd=cwd or self.repo, input=input,
                           capture_output=True, text=True, env={**os.environ, **(env or {})})
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout)
        return r

    def state(self):
        return json.loads((self.repo / ".factory/runs" / self.id / "state.json").read_text())

    def advance(self, *stages):
        return [self.run_state("advance", self.id, s).stdout.strip() for s in stages]

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_new_run(self):
        s = self.state()
        self.assertRegex(self.id, r"^\d{8}-some-slug$")
        self.assertEqual((s["stage"], s["base_branch"], s["branch"]), ("triage", "main", f"factory/{self.id}"))
        self.assertIn(".factory/", (self.repo / ".git/info/exclude").read_text())
        second = self.run_state("new", "Some Slug!", "-", input="x").stdout.split()[0]
        self.assertEqual(second, self.id + "-2")

    def test_new_run_gets_its_own_worktree(self):
        s = self.state()
        wt = self.repo / ".factory/worktrees" / self.id
        self.assertEqual((s["repo"], s["worktree"]), (str(wt), str(wt)))
        self.assertEqual(self.git("branch", "--show-current", cwd=wt), f"factory/{self.id}")
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_new_worktree_leaves_dirty_checkout_alone(self):
        (self.repo / "mine.txt").write_text("work in progress")
        r = self.run_state("new", "b", "-", input="x")
        self.assertEqual(r.stdout.split()[2], str(self.repo / ".factory/worktrees" / r.stdout.split()[0]))
        self.assertEqual((self.repo / "mine.txt").read_text(), "work in progress")
        self.assertFalse((Path(r.stdout.split()[2]) / "mine.txt").exists())

    def test_new_in_place(self):
        (self.repo / "f").write_text("x")
        self.assertIn("dirty", self.run_state("new", "a", "-", "--in-place", ok=False).stderr)
        (self.repo / "f").unlink()
        run_id = self.run_state("new", "a", "-", "--in-place", input="x").stdout.split()[0]
        s = json.loads((self.repo / ".factory/runs" / run_id / "state.json").read_text())
        self.assertEqual((s["repo"], s["worktree"]), (str(self.repo), None))
        self.assertNotIn(f"factory/{run_id}", self.git("branch"))

    def test_new_refuses_run_branch(self):
        self.git("switch", "-q", "-c", "factory/x")
        self.assertIn("run branch", self.run_state("new", "a", "-", ok=False).stderr)
        self.assertEqual([p.name for p in (self.repo / ".factory/runs").iterdir()], [self.id])

    def test_runs_are_shared_across_worktrees(self):
        wt = self.repo / "wt"
        self.git("worktree", "add", "-q", "-b", "side", str(wt))
        run_id = self.run_state("new", "w", "-", input="x", cwd=wt).stdout.split()[0]
        self.assertTrue((self.repo / ".factory/runs" / run_id / "state.json").exists())
        self.assertEqual(self.run_state("get", run_id, "base_branch").stdout.strip(), "side")
        listed = self.run_state("list", cwd=self.repo / ".factory/worktrees" / self.id).stdout
        self.assertIn(run_id, listed)
        self.assertIn(self.id, listed)

    def test_release_removes_worktree_and_keeps_branch(self):
        wt = self.repo / ".factory/worktrees" / self.id
        self.assertIn("done or aborted", self.run_state("release", self.id, ok=False).stderr)
        self.advance("aborted")
        (wt / "stray.txt").write_text("x")
        self.assertIn("could not remove", self.run_state("release", self.id, ok=False).stderr)
        (wt / "stray.txt").unlink()
        self.run_state("release", self.id)
        self.assertFalse(wt.exists())
        self.assertIn(f"factory/{self.id}", self.git("branch"))
        self.assertIn("already removed", self.run_state("release", self.id).stdout)

    def test_advance_guards(self):
        self.assertIn("without advancing", self.run_state("advance", self.id, "triage", ok=False).stderr)
        self.run_state("advance", self.id, "nope", ok=False)
        self.run_state("advance", self.id, "done", ok=False)
        self.advance("aborted")
        self.run_state("advance", self.id, "spec", ok=False)

    def test_done_requires_pr_url(self):
        self.advance("spec", "checkpoint-1", "implement", "review", "verify", "checkpoint-2", "ship")
        self.assertIn("recorded PR URL", self.run_state("advance", self.id, "done", ok=False).stderr)
        self.run_state("set", self.id, "pr_url", "https://example.test/pr/1")
        self.advance("done")

    def ship(self, pr="https://github.com/o/r/pull/7"):
        self.advance("spec", "checkpoint-1", "implement", "review", "verify", "checkpoint-2", "ship")
        self.run_state("set", self.id, "pr_url", pr)
        self.advance("land")

    def test_ship_needs_ship_approval_unless_fixing_an_open_pr(self):
        self.advance("spec", "checkpoint-1", "implement", "review", "verify")
        self.assertIn("after checkpoint-2", self.run_state("advance", self.id, "ship", ok=False).stderr)
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        self.advance("ship")

    def test_land_requires_ship_and_pr_url(self):
        self.advance("spec", "checkpoint-1", "implement", "review", "verify", "checkpoint-2", "ship")
        self.assertIn("recorded PR URL", self.run_state("advance", self.id, "land", ok=False).stderr)
        self.assertIn("recorded PR URL", self.run_state("advance", self.id, "merged", ok=False).stderr)
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        self.advance("land")
        self.assertIn("PR open for review", self.run_state("list").stdout)

    def test_pr_fix_rounds_loop_back_through_review_and_verify_to_ship(self):
        self.ship()
        out = self.advance("implement", "review", "verify", "ship", "land")
        self.assertEqual(out[0], "implement round 2 (PR fix 1 of 3)")
        self.assertEqual((self.state()["rounds"]["land"], self.state()["loop"]["land"]), (1, 1))
        self.advance("implement", "review", "verify", "ship", "land")
        out = self.advance("implement")
        self.assertIn("PR fix 3 of 3", out[0])
        self.assertIn("LAST ATTEMPT", out[0])
        self.advance("review", "verify", "checkpoint-2")
        self.assertEqual(self.state()["loop"]["land"], 0)
        self.assertEqual(self.state()["rounds"]["land"], 3)

    def test_merged_and_closed_end_the_run_and_release_the_worktree(self):
        self.ship()
        self.assertIn("merged, closed, done or aborted", self.run_state("release", self.id, ok=False).stderr)
        self.advance("merged")
        self.run_state("advance", self.id, "implement", ok=False)
        self.run_state("release", self.id)
        self.assertFalse((self.repo / ".factory/worktrees" / self.id).exists())
        other = self.run_state("new", "b", "-", input="x").stdout.split()[0]
        self.id = other
        self.ship()
        self.advance("implement", "closed")
        self.assertEqual(self.state()["stage"], "closed")

    def test_notify_on_land_and_merge(self):
        log = self.repo.parent / f"{self.repo.name}-notify-land.log"
        env = {"FACTORY_NOTIFY": f'cat >> "{log}"; echo >> "{log}"'}
        self.advance("spec", "checkpoint-1", "implement", "review", "verify", "checkpoint-2", "ship")
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        for stage in ("land", "implement", "review", "verify", "ship", "land", "merged"):
            self.run_state("advance", self.id, stage, env=env)
        events = [json.loads(line) for line in log.read_text().splitlines()]
        log.unlink()
        self.assertEqual([e["stage"] for e in events], ["land", "land", "merged"])
        self.assertEqual([e["message"].split(": ", 1)[1] for e in events],
                         ["PR opened https://github.com/o/r/pull/7", "pushed PR fixes to https://github.com/o/r/pull/7",
                          "PR merged https://github.com/o/r/pull/7"])

    def github(self, data):
        """A gh on PATH that answers REST `api` calls from `data`, keyed by endpoint path."""
        bin_dir = self.repo.parent / f"{self.repo.name}-gh"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "data.json").write_text(json.dumps(data))
        gh = bin_dir / "gh"
        gh.write_text(f"""#!/usr/bin/env python3
import json, sys
data = json.load(open({str(bin_dir / "data.json")!r}))
args = sys.argv[1:]
path = next(a for a in args[1:] if a.startswith("repos/")).split("?")[0]
jq = args[args.index("--jq") + 1] if "--jq" in args else None
if path.endswith("/permission"):
    login = path.split("/")[-2]
    if login in data.get("perms", {{}}):
        print(data["perms"][login])
        sys.exit(0)
    print("gh: Forbidden (HTTP 403)" if data.get("perms_blocked") else "gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
if path in data.get("forbidden", []):
    print("gh: Resource not accessible by integration (HTTP 403)", file=sys.stderr)
    sys.exit(1)
value = data.get(path)
if jq is None:
    print(json.dumps(value))
else:
    key = jq.strip(".[]")
    for item in (value or {{}}).get(key, []) if key else value or []:
        print(json.dumps(item))
""")
        gh.chmod(0o755)
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def test_pr_status_summarizes_checks(self):
        self.ship()
        reviewer = lambda login, state: {"user": {"login": login}, "state": state}
        data = {"repos/o/r/pulls/7": {"state": "open", "merged": False, "draft": False, "mergeable": None,
                                      "mergeable_state": "dirty", "head": {"sha": "abc"}},
                "repos/o/r/commits/abc/check-runs": {"check_runs": [
                    {"name": "lint", "status": "completed", "conclusion": "success"},
                    {"name": "test", "status": "completed", "conclusion": "failure", "details_url": "u"},
                    {"name": "e2e", "status": "in_progress", "conclusion": None}]},
                "repos/o/r/commits/abc/status": {"statuses": [{"context": "deploy", "state": "pending"}]},
                "repos/o/r/pulls/7/reviews": [reviewer("a", "CHANGES_REQUESTED"), reviewer("b", "APPROVED"),
                                              reviewer("a", "COMMENTED"), reviewer("a", "APPROVED")]}
        st = json.loads(self.run_state("pr-status", self.id, env=self.github(data)).stdout)
        self.assertEqual((st["state"], st["ci"], st["failing"], st["pending"]),
                         ("OPEN", "fail", [{"name": "test", "url": "u"}], ["e2e", "deploy"]))
        self.assertEqual((st["mergeable"], st["review_decision"], st["head"]), ("CONFLICTING", "APPROVED", "abc"))
        self.assertNotIn("note", st)
        data["repos/o/r/pulls/7"].update(state="closed", merged=True, mergeable=True, mergeable_state="clean")
        data["repos/o/r/commits/abc/check-runs"] = {"check_runs": []}
        data["forbidden"] = ["repos/o/r/commits/abc/status"]
        st = json.loads(self.run_state("pr-status", self.id, env=self.github(data)).stdout)
        self.assertEqual((st["state"], st["ci"], st["mergeable"]), ("MERGED", "none", "MERGEABLE"))
        self.assertIn("commit statuses could not be read", st["note"])

    def comment(self, login, body, at, kind="User", **extra):
        return {"user": {"login": login, "type": kind}, "body": body, "created_at": at,
                "html_url": f"https://github.com/o/r/c/{login}-{at}", **extra}

    def test_inbox_reads_answers_from_people_with_write_access(self):
        self.advance("spec", "checkpoint-1")
        self.run_state("set", self.id, "thread_url", "https://github.com/o/r/issues/3")
        late = "2999-01-01T00:00:00Z"
        env = self.github({"perms": {"owner": "admin", "reader": "read"}, "repos/o/r/issues/3/comments": [
            self.comment("owner", "/factory approve", "2000-01-01T00:00:00Z"),  # before the checkpoint
            self.comment("owner", "<!-- factory-checkpoint: x -->\n/factory approve", late),
            self.comment("reader", "/factory ship it", late),
            self.comment("stranger", "/factory approve", late),
            self.comment("ci[bot]", "/factory approve", late, kind="Bot"),
            self.comment("owner", "/factoryx approve", late),
            self.comment("owner", "/factory   make it blue\nand bigger", late),
            self.comment("owner", "looks good", late),
        ]})
        box = json.loads(self.run_state("inbox", self.id, env=env).stdout)
        self.assertEqual([a["answer"] for a in box["answers"]], ["make it blue\nand bigger"])
        self.assertEqual(sorted((i["author"], i["reason"]) for i in box["ignored"]),
                         [("ci[bot]", "bot"), ("reader", "no write access"), ("stranger", "no write access")])
        self.assertEqual(box["feedback"], [])  # issue chatter is not PR feedback
        allow = {**env, "FACTORY_APPROVERS": "Stranger, other"}
        box = json.loads(self.run_state("inbox", self.id, env=allow).stdout)
        self.assertEqual([a["author"] for a in box["answers"]], ["stranger"])

    def test_inbox_trusts_only_the_owner_when_permissions_cannot_be_read(self):
        self.advance("spec", "checkpoint-1")
        self.run_state("set", self.id, "thread_url", "https://github.com/o/r/issues/3")
        late = "2999-01-01T00:00:00Z"
        env = self.github({"perms_blocked": True, "repos/o/r/issues/3/comments": [
            self.comment("owner", "/factory approve", late, author_association="OWNER"),
            self.comment("member", "/factory abort", late, author_association="MEMBER")]})
        box = json.loads(self.run_state("inbox", self.id, env=env).stdout)
        self.assertEqual([a["author"] for a in box["answers"]], ["owner"])
        self.assertEqual([i["author"] for i in box["ignored"]], ["member"])

    def test_inbox_on_a_pr_reads_reviews_and_inline_comments_since_the_last_fix_round(self):
        self.ship()
        env = self.github({"perms": {"rev": "write"},
                           "repos/o/r/issues/7/comments": [self.comment("rev", "please rename", "2999-01-01T00:00:01Z")],
                           "repos/o/r/pulls/7/reviews": [
                               {"user": {"login": "rev", "type": "User"}, "state": "CHANGES_REQUESTED", "body": "",
                                "submitted_at": "2999-01-01T00:00:02Z", "html_url": "r1"},
                               {"user": {"login": "rev", "type": "User"}, "state": "APPROVED", "body": "",
                                "submitted_at": "2999-01-01T00:00:03Z", "html_url": "r2"}],
                           "repos/o/r/pulls/7/comments": [
                               self.comment("lint[bot]", "unused import", "2999-01-01T00:00:04Z", kind="Bot",
                                            path="a.py", line=3),
                               self.comment("drive-by", "/factory ship", "2999-01-01T00:00:05Z", path="a.py", line=1)]})
        box = json.loads(self.run_state("inbox", self.id, env=env).stdout)
        self.assertEqual([(f["kind"], f["author"], f["trusted"]) for f in box["feedback"]],
                         [("comment", "rev", True), ("review", "rev", True), ("inline", "lint[bot]", False)])
        self.assertEqual(box["feedback"][2]["path"], "a.py")
        self.assertEqual(box["ignored"][0]["author"], "drive-by")
        self.advance("implement")
        self.assertEqual(json.loads(self.run_state("inbox", self.id, env=env).stdout)["since"], self.state()["history"][-1]["at"])

    def test_checkpoint_post_puts_the_spec_on_the_issue_when_enabled(self):
        folder = self.repo / ".factory/runs" / self.id
        self.assertIn("not a checkpoint", self.run_state("checkpoint-post", self.id, ok=False).stderr)
        self.advance("spec", "checkpoint-1")
        self.assertIn("no thread_url", self.run_state("checkpoint-post", self.id, ok=False).stderr)
        self.run_state("set", self.id, "thread_url", "https://github.com/o/r/issues/3")
        self.assertIn("postCheckpoints", self.run_state("checkpoint-post", self.id, ok=False).stderr)
        (folder / "spec.md").write_text("# Spec: blue\n\n## Acceptance criteria\n- [ ] AC1: it is blue\n")
        self.git("config", "factory.postCheckpoints", "true")
        out = self.run_state("checkpoint-post", self.id).stdout.splitlines()
        self.assertEqual(out[1], f"gh issue comment https://github.com/o/r/issues/3 --body-file {folder / 'checkpoint-post.md'}")
        post = (folder / "checkpoint-post.md").read_text()
        self.assertTrue(post.startswith(f"<!-- factory-checkpoint: {self.id} checkpoint-1 -->"))
        self.assertIn("# Spec: blue\n\n## Acceptance criteria\n- [ ] AC1: it is blue\n", post)
        self.assertIn("`/factory approve`", post)

    def test_checkpoint_post_on_the_pr_needs_no_opt_in(self):
        self.ship()
        self.advance("implement", "review", "verify", "checkpoint-2", "blocked")
        folder = self.repo / ".factory/runs" / self.id
        self.run_state("advance", self.id, "checkpoint-2", "PR feedback did not converge")
        out = self.run_state("checkpoint-post", self.id).stdout.splitlines()
        self.assertTrue(out[1].startswith("gh pr comment https://github.com/o/r/pull/7 "))
        post = (folder / "checkpoint-post.md").read_text()
        self.assertIn("**Note:** PR feedback did not converge", post)
        self.assertIn("push this round to the PR", post)

    def test_pr_update_summarizes_a_fix_round(self):
        self.ship()
        self.assertIn("pr-report", self.run_state("pr-update", self.id, ok=False).stderr)
        folder = self.repo / ".factory/runs" / self.id
        (folder / "implementation.md").write_text("# Implementation\n\n## Addressed\n- renamed foo (review r1)\n\n"
                                                  "## Checks run\n- tests: pass\n")
        self.advance("implement", "review", "verify", "ship")
        out = self.run_state("pr-update", self.id).stdout.splitlines()
        self.assertEqual(out[1], f"gh pr comment https://github.com/o/r/pull/7 --body-file {folder / 'pr-update.md'}")
        post = (folder / "pr-update.md").read_text()
        self.assertTrue(post.startswith(f"<!-- factory-update: {self.id} 1 -->"))
        self.assertIn("### Addressed\n\n- renamed foo (review r1)\n\n**Verification:**", post)
        self.assertIn("· PR fixes 1", post)
        self.assertNotIn("Checks run", post)

    def test_usage_and_stats(self):
        self.run_state("usage", self.id, "triage", "1200", "0.05")
        self.run_state("usage", self.id, "spec", "800")
        self.run_state("usage", self.id, "spec", "x", ok=False)
        self.advance("spec", "checkpoint-1")
        self.run_state("advance", self.id, "spec", "revise")
        self.advance("checkpoint-1", "implement", "review", "verify", "checkpoint-2", "implement", "review",
                     "verify", "checkpoint-2", "ship")
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        self.advance("land", "implement", "review", "verify", "ship", "land", "merged")
        st = json.loads(self.run_state("stats", self.id, "--json").stdout)
        self.assertEqual((st["spec_revisions"], st["send_backs"], st["tokens"], st["usd"]), (1, 1, 2000, 0.05))
        self.assertEqual(st["rounds"]["land"], 1)
        self.assertIsNotNone(st["to_merge"])
        self.assertIn("land", st["seconds"]["by_stage"])
        self.assertIn("tokens 2000 · $0.05", self.run_state("stats", self.id).stdout)
        self.run_state("new", "other", "-", input="x")
        all_runs = json.loads(self.run_state("stats", "--json").stdout)["summary"]
        self.assertEqual((all_runs["runs"], all_runs["outcomes"], all_runs["merge_rate"]), (2, {"merged": 1, "open": 1}, 1.0))
        self.assertEqual((all_runs["spec_approved_first_time"], all_runs["ship_approved_first_time"]), (0.0, 0.0))
        self.assertIn("merge rate: 100% of 1 finished PRs", self.run_state("stats").stdout)

    def test_set_guards(self):
        for field in ("stage", "rounds", "loop", "history", "id", "repo", "branch", "base_branch", "worktree"):
            self.run_state("set", self.id, field, "x", ok=False)
        self.run_state("set", self.id, "pr_url", "http://x")
        self.run_state("set", self.id, "report_url", "http://x#c")
        self.assertEqual((self.state()["pr_url"], self.state()["report_url"]), ("http://x", "http://x#c"))

    def test_review_attempts_count_consecutive_change_requests(self):
        out = self.advance("spec", "checkpoint-1", "implement", "review", "implement", "review",
                           "implement", "review")
        self.assertIn("attempt 3 of 3", out[-1])
        self.assertIn("LAST ATTEMPT", out[-1])
        self.assertNotIn("LAST ATTEMPT", out[-3])

    def test_review_approval_resets_review_attempts_but_not_verify(self):
        self.advance("spec", "checkpoint-1")
        for _ in range(2):
            self.advance("implement", "review", "implement", "review", "verify")
        out = self.advance("implement", "review", "verify")
        self.assertIn("review round 5 (attempt 1 of 3)", out[1])
        self.assertIn("verify round 3 (attempt 3 of 3)", out[2])
        self.assertIn("LAST ATTEMPT", out[2])

    def test_human_stages_reset_attempts(self):
        for human in ("checkpoint-2", "blocked", "checkpoint-1"):
            self.advance("implement", "review", "verify", human)
            self.assertEqual(self.state()["loop"], {"land": 0, "review": 0, "verify": 0}, human)
        self.assertEqual(self.state()["rounds"]["review"], 3)

    def test_feedback_appends_under_heading(self):
        self.run_state("feedback", self.id, "Checkpoint 1: round 1", input="make it blue\n")
        self.run_state("feedback", self.id, "Steer: implement", input="use foo.py")
        text = (self.repo / ".factory/runs" / self.id / "feedback.md").read_text()
        self.assertEqual(text, "## Checkpoint 1: round 1\nmake it blue\n\n## Steer: implement\nuse foo.py\n\n")
        self.run_state("feedback", self.id, "x", input="  ", ok=False)

    def test_list_marks_waiting_runs(self):
        self.advance("spec", "checkpoint-1")
        self.assertIn("waiting on human for 0m", self.run_state("list").stdout)

    def test_notify_on_human_stages_and_done(self):
        log = self.repo.parent / f"{self.repo.name}-notify.log"
        env = {"FACTORY_NOTIFY": f'cat >> "{log}"; echo >> "{log}"; echo "$FACTORY_STAGE" >> "{log}"'}
        self.run_state("set", self.id, "input_summary", "Fix login")
        for stage in ("spec", "checkpoint-1", "implement", "review", "verify", "checkpoint-2", "ship"):
            self.run_state("advance", self.id, stage, "triage questions" if stage == "checkpoint-1" else "", env=env)
        self.run_state("set", self.id, "pr_url", "https://example.test/pr/1")
        self.run_state("advance", self.id, "done", env=env)
        lines = log.read_text().splitlines()
        log.unlink()
        self.assertEqual(lines[1::2], ["checkpoint-1", "checkpoint-2", "done"])
        first, last = json.loads(lines[0]), json.loads(lines[4])
        self.assertEqual((first["id"], first["from"], first["note"]), (self.id, "spec", "triage questions"))
        self.assertEqual(first["message"], f"factory {self.id}: waiting on you at checkpoint-1 (triage questions) - Fix login")
        self.assertIn("PR opened https://example.test/pr/1", last["message"])

    def test_notify_from_git_config_and_failure_is_a_warning(self):
        self.git("config", "factory.notify", "echo boom >&2; exit 3")
        r = self.run_state("advance", self.id, "blocked", "need a key")
        self.assertIn("notify command exited 3: boom", r.stderr)
        self.assertEqual(self.state()["stage"], "blocked")

    def fake_gh(self, supports_attach):
        """A gh on PATH whose `pr comment --help` does or doesn't list --attach (added in gh 2.99)."""
        bin_dir = self.repo.parent / f"{self.repo.name}-bin-{supports_attach}"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\necho '  -F, --body-file file'\n" + ("echo '      --attach path'\n" if supports_attach else ""))
        gh.chmod(0o755)
        self.addCleanup(lambda: (gh.unlink(), bin_dir.rmdir()))
        return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def report_run(self):
        folder = self.repo / ".factory/runs" / self.id
        (folder / "spec.md").write_text("# Spec: x\n\n## Acceptance criteria\n"
                                        "- [ ] AC1: Login works | fast. **Verify by:** run it\n- [ ] AC2: Errors show\n")
        (folder / "verification.md").write_text("Verdict: fail\nSuite: pass\n### AC1: a\nResult: verified\n"
                                                "### AC2: b\nResult: unverifiable\n")
        (folder / "review.md").write_text("# Review\n\nVerdict: approve\n")
        (folder / "evidence").mkdir()
        for name in ("AC1-login-page.png", "AC2_error.mp4", "overview.png", "run.log"):
            (folder / "evidence" / name).write_bytes(b"x")
        self.run_state("set", self.id, "input_summary", "Fix login")
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        return folder

    def test_pr_report_requires_pr_url(self):
        self.assertIn("pr_url", self.run_state("pr-report", self.id, ok=False).stderr)

    def test_pr_report_attaches_media_with_gh(self):
        folder = self.report_run()
        out = self.run_state("pr-report", self.id, env=self.fake_gh(True)).stdout.splitlines()
        self.assertEqual(out[0], str(folder / "pr-report.md"))
        self.assertEqual(out[1], f"cd {folder} && gh pr comment https://github.com/o/r/pull/7 --body-file pr-report.md"
                                 " --attach evidence/AC1-login-page.png --attach evidence/AC2_error.mp4"
                                 " --attach evidence/overview.png")
        report = (folder / "pr-report.md").read_text()
        self.assertTrue(report.startswith(f"<!-- factory-run: {self.id} -->"))
        self.assertIn("**Verification:** fail · test suite pass", report)
        self.assertIn("| **AC1** Login works \\| fast. | ✅ verified |", report)
        self.assertIn("| **AC2** Errors show | ⚠️ unverifiable |", report)
        self.assertIn("**Review:** approve", report)
        self.assertIn("<details><summary>Approved spec</summary>\n\n# Spec: x", report)
        self.assertNotIn("Human feedback", report)
        self.assertIn("**AC1** Login works | fast.\n\n![AC1: login page](evidence/AC1-login-page.png)\n\n"
                      "**AC2** Errors show\n\nevidence/AC2_error.mp4\n\n**Other**\n\n![overview](evidence/overview.png)\n",
                      report)
        self.assertNotIn("run.log", report)

    def test_pr_report_orders_flow_steps_and_skips_node_modules(self):
        folder = self.report_run()
        for name in ("AC1-02-error-banner.png", "AC1-01-form-filled.png", "AC1-flow.webm", "AC1-trace.zip",
                     "node_modules/pkg/icon.png"):
            (folder / "evidence" / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / "evidence" / name).write_bytes(b"x")
        out = self.run_state("pr-report", self.id, env=self.fake_gh(True)).stdout
        report = (folder / "pr-report.md").read_text()
        self.assertIn("**AC1** Login works | fast.\n\n![AC1: step 2: error banner](evidence/AC1-02-error-banner.png)"
                      "\n\nevidence/AC1-flow.webm\n\n**AC2**", report)
        self.assertIn("_2 more screenshots and recordings (earlier steps and edge cases)", report)
        for name in ("AC1-01-form-filled", "AC1-login-page"):
            self.assertNotIn(name, out)
        self.assertNotIn("node_modules", out + report)
        self.assertNotIn("trace.zip", out)

    def test_pr_report_attaches_one_screenshot_and_recording_per_criterion(self):
        folder = self.report_run()
        for name in ("AC2_error.mp4", "overview.png", "AC1-login-page.png"):
            (folder / "evidence" / name).unlink()
        for name in ("AC1-01-start.png", "AC1-10-done.png", "AC1-02-mid.png", "AC1-03-edge-x.png",
                     "AC1-edge-flow.webm", "AC1-flow.webm", "AC2-01-edge-only.png", "AC2-edge-flow.webm"):
            (folder / "evidence" / name).write_bytes(b"x")
        out = self.run_state("pr-report", self.id, env=self.fake_gh(True)).stdout.splitlines()
        self.assertEqual(out[1].split(" --attach ")[1:], ["evidence/AC1-10-done.png", "evidence/AC1-flow.webm",
                                                          "evidence/AC2-01-edge-only.png", "evidence/AC2-edge-flow.webm"])
        self.assertIn("_4 more screenshots and recordings", (folder / "pr-report.md").read_text())

    def test_pr_report_without_attach_support_lists_media(self):
        folder = self.report_run()
        old_gh = self.fake_gh(False)
        for env, args in ((old_gh, ()), (self.fake_gh(True), ("--no-media",))):
            out = self.run_state("pr-report", self.id, *args, env=env).stdout.splitlines()
            self.assertEqual(out[1], f"cd {folder} && gh pr comment https://github.com/o/r/pull/7 --body-file pr-report.md")
            report = (folder / "pr-report.md").read_text()
            self.assertIn("Not uploaded; they are in the run folder: `evidence/AC1-login-page.png`", report)
            self.assertNotIn("![", report)
        self.assertIn("upgrade gh", self.run_state("pr-report", self.id, env=old_gh).stderr)
        self.assertNotIn("upgrade gh", self.run_state("pr-report", self.id, "--no-media", env=old_gh).stderr)

    def test_pr_report_skips_oversized_media(self):
        folder = self.report_run()
        with (folder / "evidence/AC3-huge.png").open("wb") as f:
            f.truncate(11 * 1024 * 1024)
        out = self.run_state("pr-report", self.id, env=self.fake_gh(True)).stdout
        self.assertNotIn("AC3-huge.png", out)
        self.assertIn("_Not uploaded (over GitHub's size limit", (folder / "pr-report.md").read_text())

    def test_pr_report_fits_comment_limit_and_tolerates_bad_verification(self):
        folder = self.repo / ".factory/runs" / self.id
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        (folder / "spec.md").write_text("## Acceptance criteria\n- [ ] AC1: x\n" + "s" * 50000)
        (folder / "verification.md").write_text("garbage\n" + "v" * 50000)
        self.run_state("pr-report", self.id)
        report = (folder / "pr-report.md").read_text()
        self.assertLess(len(report), 65536)
        self.assertIn("report did not validate", report)
        self.assertIn("Truncated to fit", report)
        self.assertIn("`verification.md` in the run folder", report)

    def test_new_refuses_detached_head(self):
        subprocess.run(["git", "switch", "-q", "--detach", "HEAD"], cwd=self.repo, check=True)
        self.assertIn("detached HEAD", self.run_state("new", "x", "-", input="x", ok=False).stderr)

    def test_verification_requires_each_criterion_and_passing_suite(self):
        folder = self.repo / ".factory/runs" / self.id
        (folder / "spec.md").write_text("## Acceptance criteria\n- [ ] AC1: first\n- [ ] AC2: second\n")
        verification = folder / "verification.md"
        verification.write_text("Verdict: pass\nSuite: pass\n### AC1: first\nResult: verified\n"
                                "### AC1: duplicate\nResult: verified\n")
        self.assertIn("AC1", self.run_state("check-verification", self.id, ok=False).stderr)
        verification.write_text("Verdict: pass\nSuite: fail\n### AC1: first\nResult: verified\n"
                                "### AC2: second\nResult: verified\n")
        self.assertIn("pass requires", self.run_state("check-verification", self.id, ok=False).stderr)
        verification.write_text("Verdict: fail\nSuite: pass\n### AC1: first\nResult: verified\n"
                                "### AC2: second\nResult: unverifiable\n")
        result = json.loads(self.run_state("check-verification", self.id).stdout)
        self.assertEqual(result["results"]["AC2"], "unverifiable")
        verification.write_text("Verdict: pass\nSuite: pass\n### AC1: first\nResult: verified extra\n"
                                "### AC2: second\nResult: verified\n")
        self.assertIn("AC1", self.run_state("check-verification", self.id, ok=False).stderr)
        verification.write_text("Verdict: pass\nSuite: pass\n### AC1: first\nResult: verified\n"
                                "### AC2: second\nResult: verified\n")
        result = json.loads(self.run_state("check-verification", self.id).stdout)
        self.assertEqual(result["results"], {"AC1": "verified", "AC2": "verified"})

    def test_verification_accepts_adaptive_spec(self):
        folder = self.repo / ".factory/runs" / self.id
        (folder / "spec.md").write_text(
            "# Spec: adaptive\n\nRound: 1\n\n## Summary\nFix a bug.\n\n"
            "## Acceptance criteria\n- [ ] AC1: first. **Verify by:** run it\n- [ ] AC2: second. **Verify by:** read it\n\n"
            "## Root cause\nThe parser drops a line.\n\n"
            "```mermaid\nflowchart LR\n  A[\"parse (CHANGED)\"] --> B[\"render\"]\n```\n\n"
            "## Approach\n### Parser\nKeep the line.\n\n## Out of scope\nNone.\n\n## Test plan\nAdd a test.\n\n"
            "## Risks\nNone.\n\n## Open questions\nNone.\n\n## Changes from previous round\nNone.\n")
        (folder / "verification.md").write_text("Verdict: pass\nSuite: pass\n### AC1: first\nResult: verified\n"
                                                "### AC2: second\nResult: verified\n")
        result = json.loads(self.run_state("check-verification", self.id).stdout)
        self.assertEqual(result["results"], {"AC1": "verified", "AC2": "verified"})


if __name__ == "__main__":
    unittest.main()

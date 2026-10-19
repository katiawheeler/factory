"""Unit tests for the /factory state helper. Run: python3 testing/test_state.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / ".agents/skills/factory/scripts/state.py"


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Resolved, because state.py prints real paths and macOS's temp dir is behind a symlink (/var -> /private/var).
        self.repo = Path(self.tmp.name).resolve()
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

    def test_new_refuses_a_base_behind_its_upstream(self):
        remote = self.repo.parent / f"{self.repo.name}-up.git"
        other = self.repo.parent / f"{self.repo.name}-other"
        self.addCleanup(shutil.rmtree, remote, ignore_errors=True)
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "-u", "origin", "main")
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "upstream",
                 cwd=other)
        self.git("push", "-q", "origin", "main", cwd=other)
        err = self.run_state("new", "b", "-", input="x", ok=False).stderr
        self.assertIn("main is 1 commit(s) behind origin/main; run `git pull --ff-only` first", err)
        self.git("pull", "-q", "--ff-only")
        self.run_state("new", "b", "-", input="x")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "local")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "up2", cwd=other)
        self.git("push", "-q", "origin", "main", cwd=other)
        self.assertIn("have diverged (1 ahead, 1 behind)", self.run_state("new", "c", "-", input="x").stderr)

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

    def test_advance_follows_the_loop_unless_the_repo_skips_stages(self):
        err = self.run_state("advance", self.id, "implement", ok=False).stderr
        self.assertIn("cannot move to 'implement'", err)
        self.assertIn("factory.skip", err)
        self.advance("spec", "checkpoint-1", "implement")
        for stage in ("verify", "checkpoint-2", "ship", "land"):
            self.run_state("advance", self.id, stage, ok=False)
        self.advance("blocked", "verify")  # a human steer may send the run anywhere

    def test_skip_takes_only_skippable_stages(self):
        for value in ("implement", "ship", "land", "reviews"):
            self.git("config", "factory.skip", value)
            self.assertIn("can't skip", self.run_state("advance", self.id, "spec", ok=False).stderr)
        self.git("config", "factory.skip", "all")
        self.assertIn("cannot move", self.run_state("advance", self.id, "review", ok=False).stderr)
        self.assertEqual(json.loads(self.run_state("skips").stdout), list(
            ("triage", "spec", "checkpoint-1", "review", "verify", "checkpoint-2")))
        self.assertEqual(json.loads(self.run_state("skips", env={"FACTORY_SKIP": "spec"}).stdout),
                         ["spec", "checkpoint-1"])

    def test_skip_for_one_run(self):
        self.assertIn("--skip can't skip implement",
                      self.run_state("new", "x", "-", "--skip", "implement", input="x", ok=False).stderr)
        self.id = self.run_state("new", "auto", "-", "--skip", "spec,checkpoint-2", input="x").stdout.split()[0]
        s = self.state()
        self.assertEqual(s["skip"], ["spec", "checkpoint-1", "checkpoint-2"])
        self.assertEqual(s["history"][0]["note"], "run created, skipping spec, checkpoint-1, checkpoint-2")
        self.assertEqual(json.loads(self.run_state("skips", self.id).stdout), ["spec", "checkpoint-1", "checkpoint-2"])
        self.assertEqual(json.loads(self.run_state("skips").stdout), [])
        (self.repo / ".factory/runs" / self.id / "triage.md").write_text("Verdict: proceed\n")
        self.advance("implement")
        self.assertEqual(self.state()["history"][-1]["note"], "skipped spec, checkpoint-1 (--skip)")
        self.run_state("advance", self.id, "verify", ok=False)  # review isn't skipped for this run
        # The repo's setting adds to the run's own.
        self.git("config", "factory.skip", "review")
        self.advance("verify")
        folder = self.repo / ".factory/runs" / self.id
        (folder / "verification.md").write_text("Verdict: pass\nSuite: pass\n")
        self.advance("ship")
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        self.run_state("pr-report", self.id, env=self.fake_gh(False))
        report = (folder / "pr-report.md").read_text()
        self.assertIn("**Skipped by this run's `--skip` and the repo's `factory.skip`:** spec, checkpoint-1, review, "
                      "checkpoint-2.", report)
        # Other runs don't inherit this run's --skip.
        self.git("config", "--unset", "factory.skip")
        self.id = self.run_state("new", "other", "-", input="x").stdout.split()[0]
        self.run_state("advance", self.id, "implement", ok=False)

    def test_skip_review_and_verify(self):
        self.git("config", "factory.skip", "review, verify")
        self.advance("spec", "checkpoint-1", "implement", "checkpoint-2")
        self.assertEqual(self.state()["history"][-1]["skipped"], ["review", "verify"])
        self.advance("ship")
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        self.run_state("pr-report", self.id, env=self.fake_gh(False))
        report = (self.repo / ".factory/runs" / self.id / "pr-report.md").read_text()
        self.assertIn("**Skipped by the repo's `factory.skip`:** review, verify.", report)
        self.assertIn("**Verification:** skipped", report)
        self.assertIn("(factory.skip)", self.state()["history"][4]["note"])
        self.assertNotIn("No human approved shipping", report)

    def test_skip_everything_goes_from_input_to_pr(self):
        self.git("config", "factory.skip", "all")
        self.advance("implement")
        self.assertEqual(self.state()["history"][-1]["skipped"], ["triage", "spec", "checkpoint-1"])
        self.advance("ship")
        self.assertEqual(self.state()["history"][-1]["skipped"], ["review", "verify", "checkpoint-2"])
        self.assertEqual(self.run_state("approval", self.id).stdout.strip().splitlines(), [
            "Spec: skipped; no spec was written and no human approved the work before implementation",
            "Ship: no human approved shipping; checkpoint 2 was skipped"])
        self.assertIn("recorded PR URL", self.run_state("advance", self.id, "land", ok=False).stderr)
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        self.advance("land", "implement", "ship", "land", "merged")
        stats = json.loads(self.run_state("stats", "--json").stdout)["summary"]
        self.assertEqual((stats["runs_with_skips"], stats["shipped_without_ship_approval"]), (1, 1))
        self.assertEqual(stats["auto_approved_specs"], 0)

    def test_skipping_ship_approval_needs_the_checks_that_ran_to_pass(self):
        self.git("config", "factory.skip", "checkpoint-2")
        folder = self.repo / ".factory/runs" / self.id
        self.advance("spec", "checkpoint-1", "implement", "review", "verify")
        (folder / "spec.md").write_text("## Acceptance criteria\n- [ ] AC1: first\n")
        verification = folder / "verification.md"
        verification.write_text("Verdict: fail\nSuite: fail\n### AC1: first\nResult: failed\n")
        self.assertIn("verification did not pass", self.run_state("advance", self.id, "ship", ok=False).stderr)
        verification.write_text("Verdict: pass\nSuite: pass\n### AC1: first\nResult: verified\n")
        self.advance("ship")
        self.assertEqual(self.state()["history"][-1]["skipped"], ["checkpoint-2"])
        # With verify skipped too, review's verdict is the last check, and it must approve.
        self.git("config", "factory.skip", "verify,checkpoint-2")
        self.id = self.run_state("new", "b", "-", input="x").stdout.split()[0]
        folder = self.repo / ".factory/runs" / self.id
        self.advance("spec", "checkpoint-1", "implement", "review")
        (folder / "review.md").write_text("Verdict: changes-requested\n")
        self.assertIn("review did not approve", self.run_state("advance", self.id, "ship", ok=False).stderr)
        (folder / "review.md").write_text("Verdict: approve\n")
        self.advance("ship")

    def test_skipped_triage_is_recorded_and_a_spec_less_run_verifies_the_suite(self):
        self.git("config", "factory.skip", "triage,spec")
        folder = self.repo / ".factory/runs" / self.id
        self.advance("spec")
        self.assertEqual(self.state()["history"][-1]["skipped"], ["triage"])
        self.advance("implement")  # spec skipped, so its approval is too
        self.assertEqual(self.state()["history"][-1]["skipped"], ["spec", "checkpoint-1"])
        self.advance("review", "verify")
        (folder / "verification.md").write_text("Verdict: pass\nSuite: pass\n")
        self.assertEqual(json.loads(self.run_state("check-verification", self.id).stdout)["results"], {})
        (folder / "verification.md").write_text("Verdict: pass\nSuite: fail\n")
        self.run_state("check-verification", self.id, ok=False)

    def test_concurrent_writes_to_one_run_are_not_lost(self):
        procs = [subprocess.Popen(["python3", str(STATE), "usage", self.id, "implement", str(n)], cwd=self.repo,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for n in range(1, 21)]
        procs += [subprocess.Popen(["python3", str(STATE), "set", self.id, "input_summary", "x"], cwd=self.repo,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)]
        for proc in procs:
            _, err = proc.communicate()
            self.assertEqual(proc.returncode, 0, err)
        s = self.state()
        self.assertEqual(sorted(u["tokens"] for u in s["usage"]), list(range(1, 21)))
        self.assertEqual(s["input_summary"], "x")

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
        self.assertIn("cannot move to 'merged'", self.run_state("advance", self.id, "merged", ok=False).stderr)
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        self.advance("land")
        self.assertIn("PR open for review", self.run_state("list").stdout)
        self.advance("blocked", "land")
        self.advance("implement", "blocked")
        self.assertEqual(self.state()["stage"], "blocked")

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
        self.advance("closed")
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
            self.comment("owner", "/factory Approve\n\n---\n_Generated by a tool_", late),
        ]})
        box = json.loads(self.run_state("inbox", self.id, env=env).stdout)
        self.assertEqual([a["answer"] for a in box["answers"]], ["make it blue\nand bigger", "approve"])
        self.assertEqual(sorted((i["author"], i["reason"]) for i in box["ignored"]),
                         [("ci[bot]", "bot"), ("reader", "no write access"), ("stranger", "no write access")])
        self.assertEqual(box["feedback"], [])  # issue chatter is not PR feedback
        allow = {**env, "FACTORY_APPROVERS": "Stranger, other"}
        box = json.loads(self.run_state("inbox", self.id, env=allow).stdout)
        self.assertEqual([a["author"] for a in box["answers"]], ["stranger"])
        self.assertEqual({i["reason"] for i in box["ignored"] if i["author"] == "owner"}, {"not in factory.approvers"})

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
        self.run_state("seen", self.id)
        box = json.loads(self.run_state("inbox", self.id, env=env).stdout)
        self.assertEqual(box["since"], self.state()["inbox_seen"])  # starts after what land already read
        self.assertNotIn("inbox", self.run_state("show", self.id).stderr)
        self.advance("blocked")
        r = self.run_state("show", self.id)
        self.assertIn(f"run `next {self.id}`", r.stderr)
        json.loads(r.stdout)

    def test_checkpoint_post_puts_the_spec_on_the_issue_when_enabled(self):
        folder = self.repo / ".factory/runs" / self.id
        self.assertIn("not a checkpoint", self.run_state("checkpoint-post", self.id, ok=False).stderr)
        out = self.advance("spec", "checkpoint-1")
        self.assertNotIn("post this checkpoint", out[1])
        self.assertIn("no thread_url", self.run_state("checkpoint-post", self.id, ok=False).stderr)
        self.run_state("set", self.id, "thread_url", "https://github.com/o/r/issues/3")
        self.assertIn("postCheckpoints", self.run_state("checkpoint-post", self.id, ok=False).stderr)
        (folder / "spec.md").write_text("# Spec: blue\n\n## Acceptance criteria\n- [ ] AC1: it is blue\n")
        self.git("config", "factory.postCheckpoints", "true")
        self.run_state("advance", self.id, "spec", "revise")
        self.assertIn(f"checkpoint-post {self.id}", self.run_state("advance", self.id, "checkpoint-1").stdout)
        out = self.run_state("checkpoint-post", self.id).stdout.splitlines()
        self.assertEqual(out[1], f"gh issue comment https://github.com/o/r/issues/3 --body-file {folder / 'checkpoint-post.md'}")
        self.assertEqual(out[2], f"fallback: gh api repos/o/r/issues/3/comments -F body=@{folder / 'checkpoint-post.md'} --jq .html_url")
        post = (folder / "checkpoint-post.md").read_text()
        self.assertTrue(post.startswith(f"<!-- factory-checkpoint: {self.id} checkpoint-1 -->"))
        self.assertIn("# Spec: blue\n\n## Acceptance criteria\n- [ ] AC1: it is blue\n", post)
        self.assertIn("`/factory approve`", post)

    def test_checkpoint_post_on_the_pr_needs_no_opt_in(self):
        self.ship()
        self.advance("implement", "review", "verify", "checkpoint-2", "blocked")
        folder = self.repo / ".factory/runs" / self.id
        self.run_state("advance", self.id, "checkpoint-2", "PR feedback did not converge")
        env = self.fake_gh(False)
        out = self.run_state("checkpoint-post", self.id, env=env).stdout.splitlines()
        self.assertEqual(out[1], self.state()["report_url"])
        self.assertEqual(len(self.fake_comments(env)), 1)
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
        env = self.fake_gh(False)
        out = self.run_state("pr-update", self.id, env=env).stdout.splitlines()
        self.assertEqual(out[1], self.state()["report_url"])
        post = (folder / "pr-update.md").read_text()
        self.assertTrue(post.startswith(f"<!-- factory-pr: {self.id} -->"))
        self.assertIn(f"<!-- factory-update: {self.id} 1 -->", post)
        self.assertIn("### Addressed\n\n- renamed foo (review r1)\n\n**Verification:**", post)
        self.assertIn("· PR fixes 1", post)
        self.assertNotIn("Checks run", post)

    def test_usage_and_stats(self):
        self.run_state("usage", self.id, "triage", "1200", "0.05")
        self.run_state("usage", self.id, "spec", "700")
        self.run_state("usage", self.id, "retro", "100")
        self.run_state("usage", self.id, "spec", "x", ok=False)
        self.run_state("usage", self.id, "nope", "1", ok=False)
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

    def policy_run(self, size="S", kind="bug", questions="None.", verdict="proceed"):
        folder = self.repo / ".factory/runs" / self.id
        (folder / "triage.md").write_text(f"# Triage\n\nVerdict: {verdict}\n\n## Classification\nType: {kind}\nSize: {size}\n")
        (folder / "spec.md").write_text("# Spec: x\n\n## Acceptance criteria\n- [ ] AC1: works\n\n"
                                        f"## Approach\nDo it.\n\n## Open questions\n{questions}\n")
        self.advance("spec")

    def test_spec_needs_human_approval_unless_policy_allows(self):
        self.policy_run()
        err = self.run_state("advance", self.id, "implement", ok=False).stderr
        self.assertIn("auto-approval is off", err)
        self.git("config", "factory.autoApproveSpec", "S")
        out = json.loads(self.run_state("policy", self.id).stdout)
        self.assertEqual((out["auto_approve_spec"], out["size"], out["type"]), (True, "S", "bug"))
        self.advance("implement")
        self.assertEqual(self.state()["history"][-1]["note"], "spec auto-approved by policy (size S, type bug)")
        stats = json.loads(self.run_state("stats", "--json").stdout)["summary"]
        self.assertEqual(stats["auto_approved_specs"], 1)
        self.assertEqual(self.run_state("approval", self.id).stdout.strip(),
                         "Spec: auto-approved by policy (size S, type bug); no human reviewed it before implementation")
        self.advance("review", "verify", "checkpoint-2", "ship")
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        self.run_state("pr-report", self.id, env=self.fake_gh(False))
        report = (self.repo / ".factory/runs" / self.id / "pr-report.md").read_text()
        self.assertIn("**Spec:** auto-approved by policy (size S, type bug); no human reviewed it before implementation.",
                      report)
        # A human later sends it back to spec and approves the revision: the report stops saying auto.
        self.advance("land", "blocked", "spec", "checkpoint-1", "implement")
        self.assertEqual(self.run_state("approval", self.id).stdout.strip(), "Spec: approved by a human at checkpoint 1")
        self.run_state("pr-report", self.id, env=self.fake_gh(False))
        self.assertNotIn("**Spec:**", (self.repo / ".factory/runs" / self.id / "pr-report.md").read_text())

    def test_approval_before_and_after_a_human_approves(self):
        self.assertEqual(self.run_state("approval", self.id).stdout.strip(), "Spec: not approved yet")
        self.advance("spec", "checkpoint-1", "implement")
        self.assertEqual(self.run_state("approval", self.id).stdout.strip(), "Spec: approved by a human at checkpoint 1")

    def test_policy_reasons(self):
        self.policy_run(size="M", kind="feature", questions="- Q: which? **Default if unanswered:** a",
                        verdict="needs-human")
        reasons = json.loads(self.run_state("policy", self.id, env={"FACTORY_AUTO_APPROVE_SPEC": "S"}).stdout)["reasons"]
        self.assertEqual(reasons, ["triage verdict is needs-human, not proceed", "size M is not in S",
                                   "type feature is not in bug,chore", "the spec has open questions"])
        env = {"FACTORY_AUTO_APPROVE_SPEC": "S,M", "FACTORY_AUTO_APPROVE_TYPES": "feature"}
        self.advance("checkpoint-1")
        self.run_state("advance", self.id, "spec", "revise")
        reasons = json.loads(self.run_state("policy", self.id, env=env).stdout)["reasons"]
        self.assertIn("the spec has been revised", reasons)
        self.assertNotIn("size M is not in S,M", reasons)

    def test_new_links_a_follow_up_to_its_parent(self):
        self.run_state("new", "fix", "-", "--parent", "nope", input="x", ok=False)
        child = self.run_state("new", "fix", "-", "--parent", self.id, "--in-place", input="x").stdout.split()[0]
        s = json.loads((self.repo / ".factory/runs" / child / "state.json").read_text())
        self.assertEqual((s["parent_run"], s["worktree"]), (self.id, None))
        self.assertIn("unknown option", self.run_state("new", "a", "-", "--bogus", ok=False).stderr)

    def finished_run(self, stage="merged"):
        self.ship()
        self.advance(stage)
        return self.repo / ".factory/runs" / self.id

    def test_learn_merges_retro_into_learnings(self):
        self.assertIn("learn from a merged", self.run_state("learn", self.id, ok=False).stderr)
        folder = self.finished_run()
        self.assertIn("no retro.md", self.run_state("learn", self.id, ok=False).stderr)
        (folder / "retro.md").write_text("# Retro\n\n## Learnings\n- [Commands] Run tests with `make test`.\n"
                                         "- [pitfalls] The API client caches tokens per process.\n"
                                         "- [Weird] Something else\n\n## Proposed repo changes\nNone.\n")
        out = self.run_state("learn", self.id).stdout
        self.assertIn("added 3, retired 0, skipped 0 duplicates; 3 learnings", out)
        text = (self.repo / ".factory/learnings.md").read_text()
        self.assertIn(f"## Commands\n- Run tests with `make test`. _({self.id})_\n", text)
        self.assertIn("## Pitfalls\n- The API client caches tokens per process.", text)
        self.assertLess(text.index("## Pitfalls"), text.index("## Other"))
        self.assertIn("already learned", self.run_state("learn", self.id, ok=False).stderr)

        first = self.id
        self.id = self.run_state("new", "b", "-", input="x").stdout.split()[0]
        folder = self.finished_run("closed")
        (folder / "retro.md").write_text("## Learnings\n- [Commands] run tests with `make test`\n"
                                         "- [Retire] The API client caches tokens per process\n"
                                         "- [Review] Reviewers want a changelog entry. _(copied-id)_\n")
        self.assertIn("added 1, retired 1, skipped 1 duplicates; 3 learnings", self.run_state("learn", self.id).stdout)
        text = (self.repo / ".factory/learnings.md").read_text()
        self.assertNotIn("caches tokens", text)
        self.assertNotIn("## Pitfalls", text)
        self.assertIn(f"`make test`. _({first})_", text)
        self.assertIn(f"- Reviewers want a changelog entry. _({self.id})_\n", text)

    def test_signals_find_reverts_and_custom_signals(self):
        remote = self.repo.parent / f"{self.repo.name}-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        self.addCleanup(shutil.rmtree, remote, ignore_errors=True)
        self.git("remote", "add", "origin", str(remote))
        self.assertIn("signals are for merged runs", self.run_state("signals", self.id, ok=False).stderr)
        self.finished_run()
        commit = lambda msg: self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", msg)
        commit("unrelated")
        commit('Revert "Fix login"\n\nReverts o/r#7')
        self.git("push", "-q", "origin", "main")
        env = {"FACTORY_SIGNALS": 'python3 -c "import json,sys; e=json.load(sys.stdin); print(\'errors up after \' + e[\'id\'])"',
               "PATH": self.bin_without_gh()}  # no gh: the merge commit and its checks are unknown
        [result] = json.loads(self.run_state("signals", env=env).stdout)
        self.assertEqual(len(result["reverted_by"]), 1)
        self.assertEqual(result["custom"], [f"errors up after {self.id}"])
        self.assertTrue(result["regression"])
        self.assertIn("merge commit unknown", result["notes"])
        self.assertTrue(self.state()["signals"]["regression"])
        self.assertEqual(json.loads(self.run_state("signals", "--days", "0", env=env).stdout), [])
        stats = json.loads(self.run_state("stats", "--json").stdout)["summary"]
        self.assertEqual((stats["regressions"], stats["merged_runs_checked_for_signals"]), (1, 1))

    def bin_without_gh(self):
        """A PATH with git and python3 but no gh, wherever the system installs gh."""
        bin_dir = self.repo.parent / f"{self.repo.name}-nogh"
        bin_dir.mkdir()
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        for name, target in (("git", shutil.which("git")), ("python3", sys.executable)):
            (bin_dir / name).symlink_to(target)
        return str(bin_dir)

    def test_signals_read_checks_on_the_merge_commit(self):
        self.finished_run()
        env = self.github({"repos/o/r/pulls/7": {"merge_commit_sha": "m1"},
                           "repos/o/r/commits/m1/check-runs": {"check_runs": [
                               {"name": "deploy", "status": "completed", "conclusion": "failure", "details_url": "d"}]}})
        [result] = json.loads(self.run_state("signals", self.id, env=env).stdout)
        self.assertEqual((result["merge_sha"], result["base_ci"], result["reverted_by"]), ("m1", "fail", []))
        self.assertTrue(result["regression"])
        self.assertEqual(self.state()["merge_sha"], "m1")

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
        self.advance("spec", "checkpoint-1")
        for human in ("checkpoint-2", "blocked"):
            self.advance("implement", "review", "verify", human)
            self.assertEqual(self.state()["loop"], {"land": 0, "review": 0, "verify": 0}, human)
        self.assertEqual(self.state()["rounds"]["review"], 2)

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
        """A stateful fake GitHub CLI for PR comment publication."""
        bin_dir = self.repo.parent / f"{self.repo.name}-bin-{supports_attach}"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "comments.json").write_text("[]")
        (bin_dir / "calls.json").write_text("[]")
        gh = bin_dir / "gh"
        gh.write_text(f'''#!/usr/bin/env python3
import json, pathlib, sys
root = pathlib.Path({str(bin_dir)!r})
comments_path = root / "comments.json"
calls_path = root / "calls.json"
comments = json.loads(comments_path.read_text())
calls = json.loads(calls_path.read_text())
args = sys.argv[1:]
calls.append(args)
calls_path.write_text(json.dumps(calls))
def save(): comments_path.write_text(json.dumps(comments))
def body():
    flag = "-F" if "-F" in args else "--body-file"
    value = args[args.index(flag) + 1]
    return pathlib.Path(value.removeprefix("body=@")).read_text()
def created(content, api=False):
    number = max((item["id"] for item in comments), default=0) + 1
    item = {{"id": number, "body": content, "user": {{"login": "factory"}},
            "html_url": f"https://github.com/o/r/issues/7#issuecomment-{{number}}",
            "created_at": "2026-01-01T00:00:00Z"}}
    comments.append(item)
    save()
    print(json.dumps(item) if api else item["html_url"])
if args[:3] == ["pr", "comment", "--help"]:
    print("  -F, --body-file file")
    {"print('      --attach path')" if supports_attach else "pass"}
elif args[:2] == ["pr", "comment"]:
    if (root / "fail-upload").exists(): sys.exit(1)
    created(body())
elif args[0] == "api":
    method = args[args.index("--method") + 1] if "--method" in args else "GET"
    endpoint = next((arg for arg in args if arg.startswith("repos/")), "user")
    if endpoint == "user": print("factory")
    elif method == "GET":
        if (root / "fail-list").exists(): sys.exit(1)
        for item in comments: print(json.dumps(item))
    elif method == "POST": created(body(), api=True)
    elif method == "PATCH":
        if (root / "fail-patch").exists(): sys.exit(1)
        number = int(endpoint.rsplit("/", 1)[1])
        item = next(item for item in comments if item["id"] == number)
        item["body"] = body()
        save()
        print(json.dumps(item))
    elif method == "DELETE":
        number = int(endpoint.rsplit("/", 1)[1])
        comments[:] = [item for item in comments if item["id"] != number]
        save()
else: sys.exit(1)
''')
        gh.chmod(0o755)
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(bin_dir)}

    @staticmethod
    def fake_comments(env):
        return json.loads((Path(env["FAKE_GH_DIR"]) / "comments.json").read_text())

    @staticmethod
    def fake_calls(env):
        return json.loads((Path(env["FAKE_GH_DIR"]) / "calls.json").read_text())

    @staticmethod
    def seed_fake_comments(env, comments):
        (Path(env["FAKE_GH_DIR"]) / "comments.json").write_text(json.dumps(comments))

    def factory_comment(self, number, body, author="factory"):
        return {"id": number, "body": body, "user": {"login": author},
                "html_url": f"https://github.com/o/r/issues/7#issuecomment-{number}",
                "created_at": f"2026-01-{number:02d}T00:00:00Z"}

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

    def test_pr_report_creates_once_then_edits_same_comment(self):
        self.report_run()
        env = self.fake_gh(False)
        self.run_state("pr-report", self.id, env=env)
        first = self.fake_comments(env)[0]
        self.assertEqual(self.state()["report_url"], first["html_url"])
        self.assertIn(f"factory-run: {self.id}", first["body"])
        self.assertIn("**Verification:**", first["body"])
        self.assertIn("**Review:**", first["body"])
        self.assertIn("Approved spec", first["body"])
        self.run_state("pr-report", self.id, env=env)
        self.assertEqual([item["id"] for item in self.fake_comments(env)], [first["id"]])
        calls = self.fake_calls(env)
        self.assertEqual(sum("POST" in call or (call[:2] == ["pr", "comment"] and "--help" not in call)
                             for call in calls), 1)
        self.assertEqual(sum("PATCH" in call for call in calls), 1)

    def test_pr_report_reuses_another_runs_comment_and_cleans_owned_duplicates(self):
        self.report_run()
        env = self.fake_gh(False)
        self.seed_fake_comments(env, [
            self.factory_comment(1, "<!-- factory-run: previous -->\nOld"),
            self.factory_comment(2, "<!-- factory-update: previous 1 -->\nOlder"),
            self.factory_comment(3, "Human note", "human"),
            self.factory_comment(4, "Bot output", "ci[bot]"),
        ])
        self.run_state("pr-report", self.id, env=env)
        comments = self.fake_comments(env)
        self.assertEqual([item["id"] for item in comments], [1, 3, 4])
        self.assertIn(f"factory-run: {self.id}", comments[0]["body"])
        self.assertEqual(self.state()["report_url"], comments[0]["html_url"])
        self.assertEqual(sum("PATCH" in call for call in self.fake_calls(env)), 1)
        self.assertEqual(sum("DELETE" in call for call in self.fake_calls(env)), 1)
        self.assertEqual(sum("POST" in call for call in self.fake_calls(env)), 0)

    def test_pr_report_does_not_replace_foreign_or_failed_comment(self):
        self.report_run()
        for failure in ("foreign", "fail-list", "fail-patch"):
            env = self.fake_gh(False)
            author = "other" if failure == "foreign" else "factory"
            original = self.factory_comment(1, "<!-- factory-run: previous -->\nOld", author)
            self.seed_fake_comments(env, [original])
            if failure != "foreign":
                (Path(env["FAKE_GH_DIR"]) / failure).touch()
            self.run_state("pr-report", self.id, env=env, ok=False)
            self.assertEqual(self.fake_comments(env), [original])
            self.assertFalse(any("POST" in call for call in self.fake_calls(env)))

    def test_pr_report_ignores_quoted_factory_marker_in_human_comment(self):
        self.report_run()
        env = self.fake_gh(False)
        human = self.factory_comment(1, "Review note:\n> <!-- factory-run: quoted -->\nPlease check this.", "human")
        self.seed_fake_comments(env, [human])
        self.run_state("pr-report", self.id, env=env)
        comments = self.fake_comments(env)
        self.assertEqual(comments[0], human)
        self.assertEqual(len(comments), 2)
        self.assertIn(f"<!-- factory-pr: {self.id} -->", comments[1]["body"])

    def test_pr_checkpoint_and_fix_round_keep_one_comment(self):
        self.report_run()
        env = self.fake_gh(False)
        self.run_state("pr-report", self.id, env=env)
        original = self.fake_comments(env)[0]
        self.advance("spec", "checkpoint-1")
        self.run_state("checkpoint-post", self.id, env=env)
        self.assertEqual([item["id"] for item in self.fake_comments(env)], [original["id"]])
        self.assertIn("checkpoint-1", self.fake_comments(env)[0]["body"])
        self.advance("implement", "review", "verify", "checkpoint-2", "ship", "land", "implement",
                     "review", "verify", "ship")
        self.run_state("pr-update", self.id, env=env)
        self.assertEqual([item["id"] for item in self.fake_comments(env)], [original["id"]])
        self.assertIn("factory-update", self.fake_comments(env)[0]["body"])
        self.assertEqual(self.state()["report_url"], original["html_url"])
        self.assertFalse(any("POST" in call for call in self.fake_calls(env)[5:]))

    def test_pr_report_upload_failure_keeps_one_text_only_comment(self):
        self.report_run()
        env = self.fake_gh(True)
        (Path(env["FAKE_GH_DIR"]) / "fail-upload").touch()
        self.run_state("pr-report", self.id, env=env)
        self.assertEqual(len(self.fake_comments(env)), 1)
        self.assertIn("Not uploaded", self.fake_comments(env)[0]["body"])
        self.assertNotIn("![", self.fake_comments(env)[0]["body"])

    def test_pr_report_edit_retains_uploaded_media_links(self):
        self.report_run()
        env = self.fake_gh(True)
        link = "![flow](https://github.com/user-attachments/assets/flow.png)"
        self.seed_fake_comments(env, [self.factory_comment(1, f"<!-- factory-run: prior -->\n"
                                                               f"### Screenshots and recordings\n\n{link}")])
        self.run_state("pr-report", self.id, env=env)
        self.assertEqual(len(self.fake_comments(env)), 1)
        body = self.fake_comments(env)[0]["body"]
        self.assertIn(link, body)
        self.assertIn("AC1-login-page.png", body)
        self.assertNotIn("![AC1: login page](evidence/", body)

    def test_pr_report_retains_only_bounded_uploaded_media(self):
        self.report_run()
        env = self.fake_gh(False)
        uploaded = "![flow](https://github.com/user-attachments/assets/flow.png)"
        unrelated = "[old spec](https://example.com/spec)"
        long_links = [f"![image {n}](https://github.com/user-attachments/assets/{'x' * 900}{n})"
                      for n in range(100)]
        old_body = ("<!-- factory-run: prior -->\n### Screenshots and recordings\n\n"
                    + "\n".join([uploaded, unrelated, *long_links]))
        self.seed_fake_comments(env, [self.factory_comment(1, old_body)])
        self.run_state("pr-report", self.id, env=env)
        body = self.fake_comments(env)[0]["body"]
        self.assertLessEqual(len(body), 60000)
        self.assertTrue(body.startswith(f"<!-- factory-pr: {self.id} -->"))
        self.assertIn("**Verification:** fail · test suite pass", body)
        self.assertIn(uploaded, body)
        self.assertNotIn(unrelated, body)

    def test_pr_report_attaches_media_with_gh(self):
        folder = self.report_run()
        env = self.fake_gh(True)
        out = self.run_state("pr-report", self.id, env=env).stdout.splitlines()
        self.assertEqual(out[0], str(folder / "pr-report.md"))
        self.assertEqual(out[1], self.state()["report_url"])
        self.assertEqual(len(self.fake_comments(env)), 1)
        self.assertIn("--attach", next(call for call in self.fake_calls(env) if call[:2] == ["pr", "comment"]
                                       and "--help" not in call))
        report = (folder / "pr-report.md").read_text()
        self.assertTrue(report.startswith(f"<!-- factory-pr: {self.id} -->"))
        self.assertIn(f"<!-- factory-run: {self.id} -->", report)
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
        env = self.fake_gh(True)
        self.run_state("pr-report", self.id, env=env)
        attach_call = next(call for call in self.fake_calls(env) if call[:2] == ["pr", "comment"]
                           and "--attach" in call)
        self.assertEqual([attach_call[index + 1] for index, arg in enumerate(attach_call) if arg == "--attach"],
                         ["evidence/AC1-10-done.png", "evidence/AC1-flow.webm", "evidence/AC2-01-edge-only.png",
                          "evidence/AC2-edge-flow.webm"])
        self.assertIn("_4 more screenshots and recordings", (folder / "pr-report.md").read_text())

    def test_pr_report_without_attach_support_lists_media(self):
        folder = self.report_run()
        old_gh = self.fake_gh(False)
        for env, args in ((old_gh, ()), (self.fake_gh(True), ("--no-media",))):
            out = self.run_state("pr-report", self.id, *args, env=env).stdout.splitlines()
            self.assertEqual(out[1], self.state()["report_url"])
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
        self.run_state("pr-report", self.id, env=self.fake_gh(False))
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


    # -- next and answer: the loop in state.py ---------------------------------------------------------------

    TRIAGE = ("# Triage\n\nVerdict: proceed\nSource: none\n\n## Resolved input\nFix the word count.\n\n"
              "## Classification\nType: bug\nSize: S\n")

    @staticmethod
    def spec(n):
        return (f"# Spec: fix\n\nRound: {n}\n\n## Acceptance criteria\n- [ ] AC1: counts words. **Verify by:** run it\n"
                f"- [ ] AC2: empty is 0. **Verify by:** run it\n\n## Open questions\nNone.\n")

    @staticmethod
    def verification(n, verdict="pass", suite="pass", ac2="verified"):
        return (f"# Verification\n\nRound: {n}\nVerdict: {verdict}\nSuite: {suite}\n\n### AC1: counts words\n"
                f"Result: verified\nReplay: npx playwright test AC1\n\n### AC2: empty is 0\nResult: {ac2}\n")

    def step(self, *extra, env=None):
        return json.loads(self.run_state("next", self.id, *extra, env=env).stdout)

    def answer(self, *args, input="", ok=True, env=None):
        r = self.run_state("answer", self.id, *args, input=input, ok=ok, env=env)
        return json.loads(r.stdout) if ok else r.stderr

    def write(self, name, text):
        (self.repo / ".factory/runs" / self.id / name).write_text(text)

    def to_spec_approval(self):
        self.step()
        self.write("triage.md", self.TRIAGE)
        self.step()
        self.write("spec.md", self.spec(1))
        return self.step()

    def to_verify(self):
        self.to_spec_approval()
        self.answer("approve")
        self.write("implementation.md", "Round: 1\nStatus: done\n")
        self.step()
        self.write("review.md", "Round: 1\nVerdict: approve\n")
        return self.step()

    def test_next_dispatches_each_stage_and_redispatches_one_that_wrote_nothing(self):
        folder = self.repo / ".factory/runs" / self.id
        out = self.step()
        self.assertEqual((out["action"], out["stage"], out["round"], out["agent"]),
                         ("dispatch", "triage", 1, "factory-triage"))
        self.assertTrue(out["prompt"].splitlines()[0].endswith("stages/triage.md and follow it."))
        self.assertIn(f"Run folder: {folder}\n", out["prompt"])
        self.assertIn(f"Repository: {self.repo / '.factory/worktrees' / self.id}.", out["prompt"])
        again = self.step("--tokens", "1200", "--usd", "0.5")
        self.assertEqual(again["round"], 1)
        self.assertIn("didn't finish", again["tell"][0])
        self.assertEqual([(u["stage"], u["round"], u["tokens"]) for u in self.state()["usage"]], [("triage", 1, 1200)])
        self.write("triage.md", self.TRIAGE)
        out = self.step()
        self.assertEqual((out["stage"], out["round"], out["did"]), ("spec", 1, ["triage → spec"]))
        self.assertEqual(self.state()["input_summary"], "do it")
        self.write("spec.md", self.spec(1))
        out = self.step()
        self.assertEqual((out["action"], out["kind"], out["choices"][0]), ("ask", "spec-approval", "approve"))
        self.assertEqual(out["spec"], str(folder / "spec.md"))
        self.assertEqual(self.step()["kind"], "spec-approval")  # asking again moves nothing
        self.assertEqual([e["to"] for e in self.state()["history"]], ["triage", "spec", "checkpoint-1"])

    def test_a_stage_that_never_writes_its_output_goes_to_a_human(self):
        for _ in range(3):
            self.assertEqual(self.step()["action"], "dispatch")
        out = self.step()
        self.assertEqual((out["action"], out["kind"], out["raised_by"]), ("ask", "blocked", "triage"))
        self.assertEqual(out["question"], "triage wrote no usable triage.md in 3 dispatches")
        self.assertEqual(self.answer("try again")["stage"], "triage")
        self.write("triage.md", "Verdict: maybe\n")
        out = self.step()
        self.assertIn("Fix from the orchestrator: your last triage.md can't be used: it needs exactly one "
                      "`Verdict: proceed | needs-human | reject` line", out["prompt"])

    def test_answer_applies_spec_approval_revisions_and_pause(self):
        self.to_spec_approval()
        self.assertIn("waiting on spec approval", self.answer("ship", ok=False))
        self.assertEqual(self.answer("pause")["action"], "paused")
        out = self.answer("-", input="make it blue\n")
        self.assertEqual((out["stage"], out["round"], out["did"]), ("spec", 2, ["checkpoint-1 → spec: revise"]))
        self.write("spec.md", self.spec(1))
        self.assertEqual(self.step()["action"], "dispatch")  # a round 1 spec isn't round 2's output
        self.write("spec.md", self.spec(2))
        self.assertEqual(self.step()["kind"], "spec-approval")
        out = self.answer("approve")
        self.assertEqual((out["stage"], out["round"]), ("implement", 1))
        self.assertEqual((self.repo / ".factory/runs" / self.id / "feedback.md").read_text(),
                         "## Checkpoint 1: round 1\nmake it blue\n\n## Checkpoint 1: round 2\nApproved.\n\n")
        self.assertEqual(json.loads(self.run_state("stats", self.id, "--json").stdout)["spec_revisions"], 1)

    def test_triage_questions_go_to_spec_with_the_answers(self):
        self.step()
        self.write("triage.md", "Verdict: needs-human\n\n## Open questions\n- Tabs or spaces? Default: both\n")
        out = self.step()
        self.assertEqual((out["kind"], out["open_questions"]), ("triage-questions", "- Tabs or spaces? Default: both"))
        out = self.answer("approve")
        self.assertEqual((out["stage"], out["did"]), ("spec", ["checkpoint-1 → spec: triage answered"]))
        self.assertIn("## Checkpoint 1: triage\nUse triage's suggested defaults.",
                      (self.repo / ".factory/runs" / self.id / "feedback.md").read_text())

    def test_next_caps_review_rounds_and_advance_enforces_the_cap(self):
        self.to_spec_approval()
        self.answer("approve")
        for n in (1, 2, 3):
            self.write("implementation.md", f"Round: {n}\nStatus: done\n")
            self.assertEqual(self.step()["stage"], "review")
            self.write("review.md", f"Round: {n}\nVerdict: changes-requested\n")
            out = self.step()
        self.assertEqual((out["action"], out["kind"], out["note"]), ("ask", "ship-approval", "review did not converge"))
        self.id = self.run_state("new", "b", "-", input="x").stdout.split()[0]
        self.advance("spec", "checkpoint-1", "implement", "review", "implement", "review", "implement", "review")
        err = self.run_state("advance", self.id, "implement", ok=False).stderr
        self.assertIn("review has used all 3 attempts; go to checkpoint-2 with the note 'review did not converge'", err)
        self.advance("checkpoint-2")

    def test_next_caps_verify_rounds(self):
        self.to_verify()
        for n in (1, 2, 3):
            self.write("verification.md", self.verification(n, "fail", ac2="failed"))
            out = self.step()
            if n < 3:
                self.assertEqual((out["stage"], out["did"][0]), ("implement", "verify → implement: verify failed"))
                self.write("implementation.md", f"Round: {n + 1}\nStatus: done\n")
                self.step()
                self.write("review.md", f"Round: {n + 1}\nVerdict: approve\n")
                self.step()
        self.assertEqual((out["kind"], out["note"]), ("ship-approval", "verify did not pass"))

    def test_invalid_verification_is_sent_back_once_then_blocks(self):
        out = self.to_verify()
        self.assertEqual((out["stage"], out["round"]), ("verify", 1))
        self.write("verification.md", "Round: 1\nVerdict: pass\nSuite: pass\n")
        out = self.step()
        self.assertEqual((out["stage"], out["round"]), ("verify", 1))
        self.assertIn("Fix from the orchestrator: your last verification.md can't be used: correct verification.md: "
                      "criterion mismatch", out["prompt"])
        self.write("verification.md", "Round: 1\nVerdict: pass\nSuite: pass\n\n")
        out = self.step()
        self.assertEqual((out["kind"], out["raised_by"]), ("blocked", "verify"))
        self.assertTrue(out["question"].startswith("verification report invalid: correct verification.md"))
        out = self.answer("no display here; AC2 can't be checked")
        self.assertEqual((out["stage"], out["round"], out["did"]), ("verify", 2, ["blocked → verify: unblocked"]))
        self.write("verification.md", self.verification(2, "fail", ac2="unverifiable"))
        out = self.step()
        self.assertEqual((out["kind"], out["note"]), ("ship-approval", "unverifiable: AC2"))
        self.assertEqual(out["verification"]["results"], {"AC1": "verified", "AC2": "unverifiable"})
        self.assertEqual((out["review"], out["replay"]), ("approve", ["Replay: npx playwright test AC1"]))
        self.assertNotIn("skipped", out)  # nothing skipped, so nothing to say
        self.assertIn("pass --to with one of them", self.answer("-", input="add a test", ok=False))
        out = self.answer("--to", "implement", "-", input="add a test")
        self.assertEqual((out["stage"], out["did"]), ("implement", ["checkpoint-2 → implement: sent back"]))

    def test_blocked_implement_and_leaked_run_files(self):
        self.to_spec_approval()
        self.answer("approve")
        self.write("implementation.md", "Round: 1\nStatus: blocked\n\n## Blocked on\nWhich database?\n")
        out = self.step()
        self.assertEqual((out["kind"], out["question"], out["raised_by"]), ("blocked", "Which database?", "implement"))
        out = self.answer("Postgres")
        self.assertEqual((out["stage"], out["round"]), ("implement", 2))
        self.assertIn("## Steer: implement\nPostgres", (self.repo / ".factory/runs" / self.id / "feedback.md").read_text())
        wt = self.repo / ".factory/worktrees" / self.id
        (wt / ".factory").mkdir()
        (wt / ".factory/leak.md").write_text("x")
        self.git("add", "-f", ".factory/leak.md", cwd=wt)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "leak", cwd=wt)
        self.write("implementation.md", "Round: 2\nStatus: done\n")
        out = self.step()
        self.assertEqual((out["stage"], out["round"]), ("implement", 2))
        self.assertIn("run files leaked into the branch (.factory/leak.md)", out["prompt"])

    def test_ship_waits_for_the_push_and_land_follows_the_pr_to_a_retro(self):
        remote = self.repo.parent / f"{self.repo.name}-up.git"
        self.addCleanup(shutil.rmtree, remote, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "-u", "origin", "main")
        wt = self.repo / ".factory/worktrees" / self.id
        commit = lambda msg: self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                                      "-m", msg, cwd=wt)
        self.advance("spec", "checkpoint-1", "implement", "review", "verify", "checkpoint-2")
        out = self.answer("ship")
        self.assertEqual((out["action"], out["kind"], out["did"]), ("ship", "first", ["checkpoint-2 → ship: ship approved"]))
        self.assertIn("guides/ship.md", out["instruction"])
        self.assertEqual(self.step()["unfinished"], "no pr_url is recorded yet")
        commit("work")
        self.git("push", "-q", "-u", "origin", f"factory/{self.id}", cwd=wt)
        commit("more work")
        self.run_state("set", self.id, "pr_url", "https://github.com/o/r/pull/7")
        self.assertEqual(self.step()["unfinished"], "the run branch has commits that aren't on the remote yet")
        self.git("push", "-q", cwd=wt)
        out = self.step()
        self.assertEqual((out["stage"], out["round"], out["did"]), ("land", 1, ["ship → land: PR opened"]))
        self.write("land.md", "Round: 1\nVerdict: wait\n\n## Waiting on\nCI: test\n")
        out = self.step()
        self.assertEqual((out["action"], out["waiting_on"]), ("wait", "CI: test"))
        self.assertIn("inbox_seen", self.state())
        self.assertNotIn("tell", self.step())  # checking the PR again is a fresh dispatch
        self.write("land.md", "Round: 1\nVerdict: fix\n\n## Fix\n- [ ] CI test: fix it\n")
        out = self.step()
        self.assertEqual((out["stage"], out["round"], out["did"]), ("implement", 2, ["land → implement: PR feedback"]))
        self.write("implementation.md", "Round: 2\nStatus: done\n")
        self.step()
        self.write("review.md", "Round: 2\nVerdict: approve\n")
        self.step()
        (self.repo / ".factory/runs" / self.id / "spec.md").write_text(self.spec(1))
        self.write("verification.md", self.verification(2))
        out = self.step()
        self.assertEqual((out["action"], out["kind"], out["did"]), ("ship", "fix-round", ["verify → ship: PR fixes"]))
        self.assertEqual(self.step()["did"], ["ship → land: pushed PR fixes"])
        self.write("land.md", "Round: 2\nVerdict: merged\n")
        out = self.step()
        self.assertEqual((out["action"], out["agent"], out["did"]), ("dispatch", "factory-retro", ["land → merged"]))
        self.write("retro.md", "# Retro\n\n## Learnings\n- [Commands] Run `make test`.\n\n"
                               "## Proposed repo changes\n- Document `make test` in CONTRIBUTING.md\n")
        out = self.step()
        self.assertEqual((out["action"], out["outcome"]), ("finish", "merged"))
        self.assertEqual(out["proposals"], ["Document `make test` in CONTRIBUTING.md"])
        self.assertTrue(out["released"].startswith("removed worktree"))
        self.assertTrue(out["did"][0].startswith("learned: added 1"))
        self.assertFalse(wt.exists())
        self.assertIn(f"Run `make test`. _({self.id})_", (self.repo / ".factory/learnings.md").read_text())
        self.assertEqual(self.step()["action"], "ended")

    def test_land_caps_fix_rounds_and_its_questions_need_a_stage(self):
        env = self.github({})
        self.ship()
        for n in (1, 2, 3, 4):
            self.step(env=env)
            self.write("land.md", f"Round: {n}\nVerdict: fix\n")
            out = self.step(env=env)
            if n < 4:
                self.assertEqual(out["stage"], "implement")
                self.advance("review", "verify", "ship", "land")
        self.assertEqual((out["kind"], out["note"]), ("ship-approval", "PR feedback did not converge"))
        self.assertIn("post_on_github", out)
        self.id = self.run_state("new", "b", "-", input="x").stdout.split()[0]
        self.ship()
        self.step(env=env)
        self.write("land.md", "Round: 1\nVerdict: needs-human\n\n## Needs a human\nRename it or keep it?\n")
        out = self.step(env=env)
        self.assertEqual((out["kind"], out["question"], out["raised_by"]), ("blocked", "Rename it or keep it?", "land"))
        self.assertIn("pass --to implement", self.answer("keep it", ok=False, env=env))
        out = self.answer("--to", "land", "keep it", env=env)
        self.assertEqual((out["stage"], out["action"], out["did"]), ("land", "dispatch", ["blocked → land: unblocked"]))

    def test_advance_refuses_a_pr_fix_round_past_the_cap(self):
        self.ship()
        for _ in range(3):
            self.advance("implement", "review", "verify", "ship", "land")
        self.assertIn("all 3 fix rounds", self.run_state("advance", self.id, "implement", ok=False).stderr)
        out = self.answer("-", input="one more thing")  # a human asking for it directly isn't capped
        self.assertEqual((out["stage"], out["did"]), ("implement", ["land → implement: PR feedback"]))
        self.assertIn("## Land: round 4\none more thing", (self.repo / ".factory/runs" / self.id / "feedback.md").read_text())

    def test_next_skips_what_the_repo_skips(self):
        self.git("config", "factory.skip", "all")
        out = self.step()
        self.assertEqual((out["stage"], out["did"]),
                         ("implement", ["triage → implement: skipped triage, spec, checkpoint-1 (factory.skip)"]))
        self.assertEqual(self.state()["input_summary"], "do it")
        self.write("implementation.md", "Round: 1\nStatus: done\n")
        out = self.step()
        self.assertEqual((out["action"], out["did"]),
                         ("ship", ["implement → ship: skipped review, verify, checkpoint-2 (factory.skip)"]))

    def test_next_tells_the_human_a_spec_was_auto_approved(self):
        self.git("config", "factory.autoApproveSpec", "S")
        self.step()
        self.write("triage.md", self.TRIAGE)
        self.step()
        self.write("spec.md", self.spec(1))
        out = self.step()
        self.assertEqual(out["stage"], "implement")
        self.assertIn("auto-approved by the repo's policy (size S, type bug)", out["tell"][0])

    def test_next_applies_a_trusted_answer_from_github(self):
        self.to_spec_approval()
        self.run_state("set", self.id, "thread_url", "https://github.com/o/r/issues/3")
        comments = [self.comment("owner", "/factory approve", "2999-01-01T00:00:00Z"),
                    self.comment("stranger", "/factory abort", "2999-01-01T00:00:01Z")]
        data = {"perms": {"owner": "admin"}, "repos/o/r/issues/3/comments": comments}
        out = self.step(env=self.github(data))
        self.assertEqual((out["stage"], out["did"]), ("implement", ["checkpoint-1 → implement: spec approved"]))
        self.assertIn("Ignored `/factory` comment from @stranger (no write access)", out["tell"][0])
        self.assertIn("## Checkpoint 1: round 1 (from @owner on GitHub: https://github.com/o/r/c/owner-2999-01-01T00:00:00Z)",
                      (self.repo / ".factory/runs" / self.id / "feedback.md").read_text())
        self.advance("review", "verify", "checkpoint-2")
        comments.append(self.comment("owner", "/factory make the button red", "2999-01-02T00:00:00Z"))
        out = self.step(env=self.github(data))
        self.assertEqual((out["action"], out["github_answer"]["answer"]), ("ask", "make the button red"))
        self.assertIn("answer --to", out["instruction"])
        self.assertEqual(self.state()["stage"], "checkpoint-2")

    def test_steer_at_ship_approval_goes_where_the_human_says(self):
        self.advance("spec", "checkpoint-1", "implement", "review", "verify", "checkpoint-2")
        out = self.answer("steer")
        self.assertEqual((out["kind"], out["note"]), ("steer", "steer requested at checkpoint-2"))
        out = self.answer("--to", "review", "I fixed the typo myself")
        self.assertEqual((out["stage"], out["did"]), ("review", ["blocked → review: steered"]))
        self.assertIn("## Steer: checkpoint-2\nI fixed the typo myself",
                      (self.repo / ".factory/runs" / self.id / "feedback.md").read_text())

    def test_an_in_place_run_switches_to_its_branch_for_implement_and_back_at_the_end(self):
        self.id = self.run_state("new", "a", "-", "--in-place", input="x").stdout.split()[0]
        self.to_spec_approval()
        out = self.answer("approve")
        self.assertIn(f"switched the checkout to factory/{self.id}", out["did"])
        self.assertEqual(self.git("branch", "--show-current"), f"factory/{self.id}")
        out = self.answer("abort")
        self.assertEqual((out["action"], out["agent"]), ("dispatch", "factory-retro"))  # work began, so a retro
        self.write("retro.md", "# Retro\n\n## Learnings\n\n## Proposed repo changes\nNone.\n")
        out = self.step()
        self.assertEqual((out["released"], out.get("proposals")), ("switched back to main", None))
        self.assertEqual(self.git("branch", "--show-current"), "main")

    def test_abort_before_any_work_finishes_without_a_retro(self):
        self.step()
        out = self.answer("abort")
        self.assertEqual((out["action"], out["outcome"], out["did"]),
                         ("finish", "aborted", ["triage → aborted: aborted by a human"]))
        self.assertFalse((self.repo / ".factory/worktrees" / self.id).exists())
        self.assertIn("no answers", self.answer("approve", ok=False))

if __name__ == "__main__":
    unittest.main()

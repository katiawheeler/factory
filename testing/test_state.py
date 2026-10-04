"""Unit tests for the /factory state helper. Run: python3 testing/test_state.py"""
import json
import os
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
            self.assertEqual(self.state()["loop"], {"review": 0, "verify": 0}, human)
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
        self.assertIn("![AC1: step 1: form filled](evidence/AC1-01-form-filled.png)\n\n"
                      "![AC1: step 2: error banner](evidence/AC1-02-error-banner.png)\n\n"
                      "evidence/AC1-flow.webm\n\n![AC1: login page]", report)
        self.assertNotIn("node_modules", out + report)
        self.assertNotIn("trace.zip", out)

    def test_pr_report_without_attach_support_lists_media(self):
        folder = self.report_run()
        for env, args in ((self.fake_gh(False), ()), (self.fake_gh(True), ("--no-media",))):
            out = self.run_state("pr-report", self.id, *args, env=env).stdout.splitlines()
            self.assertEqual(out[1], f"cd {folder} && gh pr comment https://github.com/o/r/pull/7 --body-file pr-report.md")
            report = (folder / "pr-report.md").read_text()
            self.assertIn("Not uploaded; they are in the run folder: `evidence/AC1-login-page.png`", report)
            self.assertNotIn("![", report)

    def test_pr_report_skips_oversized_media(self):
        folder = self.report_run()
        with (folder / "evidence/AC1-huge.png").open("wb") as f:
            f.truncate(11 * 1024 * 1024)
        out = self.run_state("pr-report", self.id, env=self.fake_gh(True)).stdout
        self.assertNotIn("AC1-huge.png", out)
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

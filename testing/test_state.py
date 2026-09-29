"""Unit tests for the /factory state helper. Run: python3 testing/test_state.py"""
import json
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

    def run_state(self, *args, input="", ok=True):
        r = subprocess.run(["python3", str(STATE), *args], cwd=self.repo, input=input,
                           capture_output=True, text=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout)
        return r

    def state(self):
        return json.loads((self.repo / ".factory/runs" / self.id / "state.json").read_text())

    def advance(self, *stages):
        return [self.run_state("advance", self.id, s).stdout.strip() for s in stages]

    def test_new_run(self):
        s = self.state()
        self.assertRegex(self.id, r"^\d{8}-some-slug$")
        self.assertEqual((s["stage"], s["base_branch"], s["branch"]), ("triage", "main", f"factory/{self.id}"))
        self.assertIn(".factory/", (self.repo / ".git/info/exclude").read_text())
        second = self.run_state("new", "Some Slug!", "-", input="x").stdout.split()[0]
        self.assertEqual(second, self.id + "-2")

    def test_new_refuses_dirty_tree_and_run_branch(self):
        (self.repo / "f").write_text("x")
        self.run_state("new", "a", "-", ok=False)
        (self.repo / "f").unlink()
        subprocess.run(["git", "switch", "-q", "-c", "factory/x"], cwd=self.repo, check=True)
        self.assertIn("run branch", self.run_state("new", "a", "-", ok=False).stderr)
        self.assertEqual([p.name for p in (self.repo / ".factory/runs").iterdir()], [self.id])

    def test_new_in_worktree(self):
        wt = self.repo / "wt"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "side", str(wt)], cwd=self.repo, check=True)
        r = subprocess.run(["python3", str(STATE), "new", "w", "-"], cwd=wt, input="x",
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((wt / ".factory/runs" / r.stdout.split()[0] / "state.json").exists())
        self.assertIn(".factory/", (self.repo / ".git/info/exclude").read_text())

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
        for field in ("stage", "rounds", "loop", "history", "id", "repo", "branch", "base_branch"):
            self.run_state("set", self.id, field, "x", ok=False)
        self.run_state("set", self.id, "pr_url", "http://x")
        self.assertEqual(self.state()["pr_url"], "http://x")

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
        self.assertIn("waiting on human", self.run_state("list").stdout)

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


if __name__ == "__main__":
    unittest.main()

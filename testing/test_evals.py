"""Tests for the eval runner, using the fake agent. Run: python3 testing/test_evals.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EVALS = Path(__file__).resolve().parent / "evals"
FAKE = f"{sys.executable} {EVALS / 'fake_agent.py'} {{prompt}}"


class EvalRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_evals(self, *cases, env=None, extra=()):
        args = [sys.executable, str(EVALS / "run.py"), "--agent", FAKE, "--out", str(self.out / "results"),
                "--baseline", str(self.out / "baseline.json"), *extra]
        for case in cases:
            args += ["--case", case]
        r = subprocess.run(args, capture_output=True, text=True, env={**os.environ, **(env or {})})
        [folder] = sorted((self.out / "results").iterdir())[-1:]
        return r, json.loads((folder / "results.json").read_text())

    def test_fixed_bug_passes_every_check_after_a_resume(self):
        r, doc = self.run_evals("word-count-whitespace", "vague-request")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        bug, vague = doc["results"]
        self.assertTrue(bug["passed"])
        self.assertEqual(set(bug["checks"]), {"triage", "stage", "verification", "hidden_test", "files"})
        self.assertEqual(bug["checks"]["files"]["got"], ["textstat/core.py"])
        self.assertEqual(bug["metrics"]["rounds"]["implement"], 1)
        self.assertEqual((vague["checks"]["triage"]["got"], vague["checks"]["stage"]["got"]),
                         ("needs-human", "checkpoint-1"))
        self.assertIn("2/2 passed", r.stdout)

    def test_stray_files_and_wrong_triage_fail(self):
        r, doc = self.run_evals("json-output", "out-of-scope", env={"FAKE_AGENT_MODE": "stray"})
        self.assertEqual(r.returncode, 1)
        json_case, scope = doc["results"]
        self.assertEqual(json_case["checks"]["files"]["stray"], ["notes.txt"])
        self.assertTrue(json_case["checks"]["hidden_test"]["ok"])
        self.assertFalse(json_case["passed"])
        self.assertFalse(scope["checks"]["triage"]["ok"])

    def test_baseline_comparison_flags_regressions(self):
        self.run_evals("word-count-whitespace", extra=["--save-baseline"])
        self.assertTrue((self.out / "baseline.json").exists())
        r, _ = self.run_evals("word-count-whitespace", env={"FAKE_AGENT_MODE": "stray"})
        self.assertIn("vs baseline", r.stdout)
        self.assertIn("passed -1", r.stdout)
        self.assertIn("- word-count-whitespace: REGRESSED", r.stdout)

    def test_unknown_case(self):
        r = subprocess.run([sys.executable, str(EVALS / "run.py"), "--case", "nope"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown case: nope", r.stderr)


if __name__ == "__main__":
    unittest.main()

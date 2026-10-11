#!/usr/bin/env python3
"""A stand-in for a coding agent, for testing run.py without spending model time.

Called as `fake_agent.py <factory prompt>` from the case repo. It plays the factory with
canned stage outputs: vague input gets triage questions, anything else gets a one-criterion
spec, and approval makes the fix the input asks for. The first approval "dies" after entering
implement, so run.py has to resume the run. FAKE_AGENT_MODE=stray also commits an unrelated file.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

STATE = Path(__file__).resolve().parents[2] / ".agents/skills/factory/scripts/state.py"


def state(*args, input=None):
    return subprocess.run([sys.executable, str(STATE), *args], input=input, capture_output=True, text=True,
                          check=True).stdout


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def implement(run, folder, s):
    work = Path(s["repo"])
    text = (folder / "input.md").read_text()
    core, cli = work / "textstat/core.py", work / "textstat/cli.py"
    if "words" in text:
        core.write_text(core.read_text().replace('len(text.split(" "))', "len(text.split())"))
    if "--json" in text:
        src = cli.read_text().replace('stats.add_argument("file")',
                                      'stats.add_argument("file")\n    stats.add_argument("--json", action="store_true")')
        src = src.replace('        print(f"lines', '        if args.json:\n            import json\n'
                                                   '            print(json.dumps(result))\n            return 0\n        print(f"lines')
        cli.write_text(src)
    if os.environ.get("FAKE_AGENT_MODE") == "stray":
        (work / "notes.txt").write_text("scratch\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", f"factory({run}): fix", cwd=work)
    (folder / "implementation.md").write_text("# Implementation\n\nStatus: done\n")
    state("advance", run, "review")
    (folder / "review.md").write_text("# Review\n\nVerdict: approve\n")
    state("advance", run, "verify")
    (folder / "verification.md").write_text("# Verification\n\nVerdict: pass\nSuite: pass\n\n"
                                            "### AC1: fixed\nResult: verified\n")
    state("advance", run, "checkpoint-2")


def main(prompt):
    words = prompt.split(maxsplit=2)
    runs = Path(".factory/runs")
    if len(words) >= 2 and (runs / words[1]).is_dir():
        run, answer = words[1], (words[2] if len(words) > 2 else "")
        folder = runs / run
        s = json.loads(state("show", run))
        if s["stage"] == "checkpoint-1" and answer == "approve":
            state("feedback", run, "Checkpoint 1: round 1", input="approve")
            state("advance", run, "implement")
            died = runs.parent / "fake-agent-died"
            if not died.exists():
                died.write_text("x")
                return  # the session dies mid-stage; run.py has to resume it
            implement(run, folder, s)
        elif s["stage"] == "implement":
            implement(run, folder, s)
        return
    text = prompt.split(maxsplit=1)[1]
    run = state("new", "eval", "-", input=text).split()[0]
    folder = runs / run
    if "better" in text:
        (folder / "triage.md").write_text("# Triage\n\nVerdict: needs-human\n")
        state("advance", run, "checkpoint-1", "triage questions")
        return
    (folder / "triage.md").write_text("# Triage\n\nVerdict: proceed\n\n## Classification\nType: bug\nSize: S\n")
    state("advance", run, "spec")
    (folder / "spec.md").write_text("# Spec: fix\n\n## Acceptance criteria\n- [ ] AC1: fixed. **Verify by:** run it\n")
    state("advance", run, "checkpoint-1")


if __name__ == "__main__":
    main(sys.argv[1])

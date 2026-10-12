#!/usr/bin/env python3
"""A stand-in for a coding agent, for testing run.py without spending model time.

Called as `fake_agent.py <factory prompt>` from the case repo. It orchestrates the way SKILL.md says,
by running `state.py next` (or `answer`) and doing what it prints, and plays each dispatched stage with
a canned output: vague input gets triage questions, anything else gets a one-criterion spec, and
implement makes the fix the input asks for. The first implement dispatch "dies" without writing
anything, so run.py has to resume the run. FAKE_AGENT_MODE=stray also commits an unrelated file.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

STATE = Path(__file__).resolve().parents[2] / ".agents/skills/factory/scripts/state.py"
RUNS = Path(".factory/runs")


def state(*args, input=None):
    return subprocess.run([sys.executable, str(STATE), *args], input=input, capture_output=True, text=True,
                          check=True).stdout


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def triage(folder, s, n):
    if "better" in (folder / "input.md").read_text():
        return "# Triage\n\nVerdict: needs-human\n\n## Open questions\n- Better how?\n"
    return "# Triage\n\nVerdict: proceed\n\n## Resolved input\nFix textstat.\n\n## Classification\nType: bug\nSize: S\n"


def spec(folder, s, n):
    return f"# Spec: fix\n\nRound: {n}\n\n## Acceptance criteria\n- [ ] AC1: fixed. **Verify by:** run it\n"


def implement(folder, s, n):
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
    git("commit", "-q", "-m", f"factory({s['id']}): fix", cwd=work)
    return f"# Implementation\n\nRound: {n}\nStatus: done\n"


def review(folder, s, n):
    return f"# Review\n\nRound: {n}\nVerdict: approve\n"


def verify(folder, s, n):
    return f"# Verification\n\nRound: {n}\nVerdict: pass\nSuite: pass\n\n### AC1: fixed\nResult: verified\n"


PLAY = {"triage": ("triage.md", triage), "spec": ("spec.md", spec), "implement": ("implementation.md", implement),
        "review": ("review.md", review), "verify": ("verification.md", verify)}


def follow(run, out):
    """Do what `next` says until it stops for a human (or the session "dies")."""
    folder = RUNS / run
    while True:
        step = json.loads(out)
        if step["action"] != "dispatch":
            return
        stage = step["agent"].removeprefix("factory-")
        died = RUNS.parent / "fake-agent-died"
        if stage == "implement" and not died.exists():
            died.write_text("x")
            return  # the session dies mid-stage; run.py has to resume it
        name, play = PLAY[stage]
        s = json.loads(state("show", run))
        (folder / name).write_text(play(folder, s, step["round"]))
        out = state("next", run)


def main(prompt):
    words = prompt.split(maxsplit=2)
    if len(words) >= 2 and (RUNS / words[1]).is_dir():
        run, answer = words[1], (words[2] if len(words) > 2 else "")
        follow(run, state("answer", run, answer) if answer else state("next", run))
        return
    run = state("new", "eval", "-", input=prompt.split(maxsplit=1)[1]).split()[0]
    follow(run, state("next", run))


if __name__ == "__main__":
    main(sys.argv[1])

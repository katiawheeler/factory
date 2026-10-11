#!/usr/bin/env python3
"""Run the factory against fixed tickets on a sample repo, grade the result, and compare runs.

Each case starts from a fresh copy of fixture/ with the factory skill installed, drives the factory
headlessly until it stops for ship approval (or another expected stop), then grades it:
the triage verdict, where the run stopped, the verification verdict, hidden tests the agents
never saw, which files changed, and the run's rounds, time and tokens from `state.py stats`.
It never ships.

Usage:
  run.py [--case ID ...] [--agent CMD] [--baseline FILE] [--save-baseline] [--keep] [--out DIR]

--agent is a command template; {prompt} is replaced with the shell-quoted factory prompt and
{skill_dir} with the installed skill's directory. Default ($FACTORY_EVAL_AGENT overrides it):
  claude -p {prompt} --allowedTools "Bash Read Write Edit Glob Grep Agent"
"""
import argparse
import datetime
import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent.parent / ".agents/skills/factory"
STATE = SKILL / "scripts/state.py"
DEFAULT_AGENT = 'claude -p {prompt} --allowedTools "Bash Read Write Edit Glob Grep Agent"'
# Stages a headless session can stop at. Anything else means the session died mid-stage; resume it.
STOPS = {"checkpoint-1", "checkpoint-2", "blocked", "land", "merged", "closed", "done", "aborted"}
MAX_STEPS = 6


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **kw)


def make_repo(root):
    """A fresh git repo from the fixture, with the factory skill installed where agents find it."""
    repo = root / "repo"
    shutil.copytree(HERE / "fixture", repo, ignore=shutil.ignore_patterns("__pycache__"))
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=eval", "-c", "user.email=eval@example.com", "commit", "-q", "-m", "textstat"]):
        sh(["git", *args], repo).check_returncode()
    sh(["git", "config", "user.name", "factory eval"], repo)
    sh(["git", "config", "user.email", "eval@example.com"], repo)
    for skills in (".claude/skills", ".agents/skills"):
        shutil.copytree(SKILL, repo / skills / "factory")
    with open(repo / ".git/info/exclude", "a") as f:
        f.write("\n.claude/\n.agents/\n")
    return repo


def state(repo, *args):
    r = sh([sys.executable, str(STATE), *args], repo)
    return r.stdout if r.returncode == 0 else None


def runs(repo):
    d = repo / ".factory/runs"
    return sorted(p.parent.name for p in d.glob("*/state.json")) if d.exists() else []


def load(repo, run_id):
    return json.loads((repo / ".factory/runs" / run_id / "state.json").read_text())


def answer_for(case, s):
    """What the simulated human says at a stop, or None to end the case there."""
    if s["stage"] == "checkpoint-1" and not s["history"][-1]["note"].startswith("triage"):
        return case.get("answers", {}).get("checkpoint-1", "approve")
    return None


def drive(case, repo, agent, log):
    """Run the factory until it stops where a human would have to decide, answering as the case says."""
    prompt = f"/factory {case['input']}"
    run_id = None
    for step in range(MAX_STEPS):
        cmd = agent.format(prompt=shlex.quote(prompt), skill_dir=shlex.quote(str(repo / ".claude/skills/factory")))
        started = datetime.datetime.now()
        r = subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True)
        log.write(f"$ {cmd}\n[exit {r.returncode}, {datetime.datetime.now() - started}]\n{r.stdout}\n{r.stderr}\n\n")
        if run_id is None:
            found = runs(repo)
            if not found:
                return None, f"no run was created (agent exit {r.returncode})"
            run_id = found[0]
        s = load(repo, run_id)
        if s["stage"] not in STOPS:
            prompt = f"/factory {run_id}"
            continue
        answer = answer_for(case, s)
        if answer is None:
            return run_id, None
        prompt = f"/factory {run_id} {answer}"
    return run_id, f"no stop after {MAX_STEPS} agent sessions"


def changed_files(repo, s):
    r = sh(["git", "diff", "--name-only", f"{s['base_branch']}...{s['branch']}"], repo)
    return r.stdout.split() if r.returncode == 0 else []


def hidden_tests(case, s):
    """Copy the hidden test into the run's checkout, run it, and remove it again."""
    src = HERE / case["expect"]["hidden_test"]
    work = Path(s["repo"])
    if not work.exists():
        return False, f"run checkout {work} is gone"
    dest = work / f"factory_eval_{src.stem}.py"
    shutil.copy(src, dest)
    try:
        r = sh([sys.executable, "-m", "unittest", dest.stem], work, timeout=120)
    finally:
        dest.unlink()
        shutil.rmtree(work / "__pycache__", ignore_errors=True)
    return r.returncode == 0, (r.stderr.strip().splitlines() or ["no output"])[-1]


def grade(case, repo, run_id, error):
    """Every expectation in the case, checked against the run folder and the run branch."""
    expect = case["expect"]
    checks = {}
    if run_id is None:
        return {"id": case["id"], "run": None, "passed": False, "error": error, "checks": {}}
    s = load(repo, run_id)
    folder = repo / ".factory/runs" / run_id
    triage = (folder / "triage.md").read_text() if (folder / "triage.md").exists() else ""
    verdict = next((line.split(":", 1)[1].strip() for line in triage.splitlines() if line.startswith("Verdict:")), None)
    checks["triage"] = {"ok": verdict in expect["triage"], "got": verdict, "want": expect["triage"]}
    checks["stage"] = {"ok": s["stage"] == expect["stage"], "got": s["stage"], "want": expect["stage"]}
    if "verification" in expect:
        out = state(repo, "check-verification", run_id)
        got = json.loads(out)["verdict"] if out else "invalid"
        checks["verification"] = {"ok": got == expect["verification"], "got": got, "want": expect["verification"]}
    if "hidden_test" in expect:
        ok, detail = hidden_tests(case, s)
        checks["hidden_test"] = {"ok": ok, "got": detail}
    if "files" in expect:
        files = changed_files(repo, s)
        stray = [f for f in files if not any(fnmatch.fnmatch(f, pat) for pat in expect["files"])]
        checks["files"] = {"ok": bool(files) and not stray, "got": files, "stray": stray}
    stats = json.loads(state(repo, "stats", run_id, "--json") or "{}")
    return {
        "id": case["id"], "run": run_id, "error": error,
        "passed": error is None and all(c["ok"] for c in checks.values()),
        "checks": checks,
        "metrics": {
            "rounds": stats.get("rounds"),
            "agent_seconds": round((stats.get("seconds") or {}).get("agents", 0)),
            "spec_revisions": stats.get("spec_revisions"),
            "cap_hits": stats.get("cap_hits"),
            "tokens": stats.get("tokens"),
        },
    }


def totals(results):
    rounds = lambda r: sum(((r.get("metrics") or {}).get("rounds") or {}).values())
    return {
        "passed": sum(r["passed"] for r in results),
        "cases": len(results),
        "rounds": sum(rounds(r) for r in results),
        "agent_seconds": sum((r.get("metrics") or {}).get("agent_seconds") or 0 for r in results),
        "tokens": sum((r.get("metrics") or {}).get("tokens") or 0 for r in results),
    }


def report(results, baseline=None):
    lines = ["| Case | Result | Failed checks | Rounds | Agent time |", "|---|---|---|---|---|"]
    for r in results:
        failed = ", ".join(k for k, c in r["checks"].items() if not c["ok"]) or (r["error"] or "")
        m = r.get("metrics") or {}
        rounds = sum((m.get("rounds") or {}).values())
        lines.append(f"| {r['id']} | {'✅' if r['passed'] else '❌'} | {failed} | {rounds} | {m.get('agent_seconds', 0)}s |")
    t = totals(results)
    lines += ["", f"**{t['passed']}/{t['cases']} passed** · rounds {t['rounds']} · agent time {t['agent_seconds']}s"
                  + (f" · tokens {t['tokens']}" if t["tokens"] else "")]
    if baseline:
        b = totals(baseline["results"])
        delta = lambda k: f"{t[k] - b[k]:+d}"
        lines.append(f"vs baseline ({baseline.get('at', '?')}): passed {delta('passed')} · rounds {delta('rounds')}"
                     f" · agent time {delta('agent_seconds')}s" + (f" · tokens {delta('tokens')}" if b["tokens"] else ""))
        was = {r["id"]: r["passed"] for r in baseline["results"]}
        for r in results:
            if r["id"] in was and was[r["id"]] != r["passed"]:
                lines.append(f"- {r['id']}: {'now passes' if r['passed'] else 'REGRESSED'}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--case", action="append", help="run only this case (repeatable)")
    parser.add_argument("--agent", default=os.environ.get("FACTORY_EVAL_AGENT", DEFAULT_AGENT))
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--save-baseline", action="store_true", help="write these results to --baseline")
    parser.add_argument("--keep", action="store_true", help="keep each case's repo for inspection")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    args = parser.parse_args(argv)

    cases = json.loads((HERE / "cases.json").read_text())
    if args.case:
        unknown = set(args.case) - {c["id"] for c in cases}
        if unknown:
            parser.error(f"unknown case: {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c["id"] in args.case]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out, n = args.out / stamp, 1
    while out.exists():  # two runs in the same second
        n += 1
        out = args.out / f"{stamp}-{n}"
    out.mkdir(parents=True)
    results = []
    for case in cases:
        root = Path(tempfile.mkdtemp(prefix=f"factory-eval-{case['id']}-"))
        repo = make_repo(root)
        with open(out / f"{case['id']}.log", "w") as log:
            run_id, error = drive(case, repo, args.agent, log)
        result = grade(case, repo, run_id, error)
        if args.keep:
            result["repo"] = str(repo)
        else:
            shutil.rmtree(root, ignore_errors=True)
        results.append(result)
        print(f"{case['id']}: {'pass' if result['passed'] else 'FAIL'}", flush=True)
    doc = {"at": stamp, "agent": args.agent, "results": results}
    (out / "results.json").write_text(json.dumps(doc, indent=2) + "\n")
    baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() and not args.save_baseline else None
    summary = report(results, baseline)
    (out / "report.md").write_text(summary + "\n")
    print(summary)
    print(f"\nresults: {out}")
    if args.save_baseline:
        args.baseline.write_text(json.dumps(doc, indent=2) + "\n")
        print(f"baseline saved: {args.baseline}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

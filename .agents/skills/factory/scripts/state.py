#!/usr/bin/env python3
"""Run-state helper for the /factory skill.

All run state lives in <repo>/.factory/runs/<run-id>/. This script is the only
thing that writes state.json, so stage names and terminal completion are
checked and every transition is logged the same way.

Usage:
  state.py new <slug> <input-file|->     create a run; prints run id and folder
  state.py list                          list runs with stage and summary
  state.py show <run-id>                 print state.json
  state.py get <run-id> <field>          print one field (dotted, e.g. rounds.review)
  state.py advance <run-id> <stage> [note]
                                         move to <stage>, log it, bump the round
                                         counter for work stages; prints the round
  state.py set <run-id> <field> <value>  set a top-level field (e.g. pr_url, input_summary)
  state.py feedback <run-id> <heading>   append stdin to feedback.md under "## <heading>"
  state.py check-verification <run-id>    validate criterion and suite results
"""
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

STAGES = [
    "triage", "spec", "checkpoint-1", "implement", "review", "verify",
    "checkpoint-2", "ship", "done", "aborted", "blocked",
]
WORK_STAGES = {"spec", "implement", "review", "verify"}
ROUND_CAP = {"review": 3, "verify": 3}
# Entering one of these hands the run to a human, which resets the loop counters.
HUMAN_STAGES = {"checkpoint-1", "checkpoint-2", "blocked"}


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def repo_root():
    r = git("rev-parse", "--show-toplevel")
    if r.returncode != 0:
        die("not inside a git repository")
    return Path(r.stdout.strip())


def runs_dir():
    return repo_root() / ".factory" / "runs"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(run_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id):
        die(f"invalid run id '{run_id}'")
    path = runs_dir() / run_id / "state.json"
    if not path.exists():
        die(f"no run '{run_id}' (looked for {path})")
    return path, json.loads(path.read_text())


def save(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def cmd_new(slug, source):
    root = repo_root()
    status = git("status", "--porcelain")
    if status.returncode != 0:
        die(status.stderr.strip() or "could not inspect working tree")
    if status.stdout.strip():
        die("working tree is dirty; commit or stash first")
    base = git("branch", "--show-current").stdout.strip()
    if not base:
        die("detached HEAD; switch to a base branch before creating a run")
    if base.startswith("factory/"):
        die(f"you're on run branch '{base}'; switch to the branch new work should start from")
    # --git-path, because in a worktree .git is a file, not a directory
    exclude = Path(git("rev-parse", "--path-format=absolute", "--git-path", "info/exclude").stdout.strip())
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if ".factory/" not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as f:
            f.write("\n.factory/\n")

    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "run"
    base_id = f"{datetime.date.today():%Y%m%d}-{slug}"
    run_id, n = base_id, 1
    while (runs_dir() / run_id).exists():
        n += 1
        run_id = f"{base_id}-{n}"

    text = sys.stdin.read() if source == "-" else Path(source).read_text()
    folder = runs_dir() / run_id
    folder.mkdir(parents=True)
    (folder / "input.md").write_text(text if text.endswith("\n") else text + "\n")

    state = {
        "id": run_id,
        "input_summary": "",
        "repo": str(root),
        "stage": "triage",
        "base_branch": base,
        "branch": f"factory/{run_id}",
        "rounds": {s: 0 for s in sorted(WORK_STAGES)},
        "loop": {s: 0 for s in sorted(ROUND_CAP)},
        "pr_url": None,
        "history": [{"at": now(), "from": None, "to": "triage", "note": "run created"}],
    }
    save(folder / "state.json", state)
    print(run_id)
    print(folder)


def cmd_list():
    d = runs_dir()
    runs = sorted(d.glob("*/state.json")) if d.exists() else []
    if not runs:
        print("no runs")
        return
    for p in runs:
        s = json.loads(p.read_text())
        wait = "  ⏸ waiting on human" if s["stage"].startswith("checkpoint") or s["stage"] == "blocked" else ""
        print(f"{s['id']}  [{s['stage']}]  {s.get('input_summary', '')}{wait}")


def cmd_get(run_id, field):
    _, s = load(run_id)
    val = s
    for part in field.split("."):
        if not isinstance(val, dict) or part not in val:
            die(f"no field '{field}'")
        val = val[part]
    print(json.dumps(val) if isinstance(val, (dict, list)) else val)


def cmd_advance(run_id, stage, note=""):
    if stage not in STAGES:
        die(f"unknown stage '{stage}'; valid: {', '.join(STAGES)}")
    path, s = load(run_id)
    if s["stage"] in ("done", "aborted"):
        die(f"run is {s['stage']}; it cannot advance")
    if stage == s["stage"]:
        die(f"run is already at '{stage}'; dispatch it without advancing")
    if stage == "done" and (s["stage"] != "ship" or not s.get("pr_url")):
        die("done requires the ship stage and a recorded PR URL")
    loop = s.setdefault("loop", {k: 0 for k in sorted(ROUND_CAP)})
    if stage in WORK_STAGES:
        s["rounds"][stage] += 1
    if stage in ROUND_CAP:
        loop[stage] += 1
    if stage in HUMAN_STAGES:
        for k in loop:
            loop[k] = 0
    if s["stage"] == "review" and stage == "verify":
        loop["review"] = 0  # review approved; its loop only counts consecutive change requests
    s["history"].append({"at": now(), "from": s["stage"], "to": stage, "note": note})
    s["stage"] = stage
    save(path, s)
    if stage in ROUND_CAP:
        n, cap = loop[stage], ROUND_CAP[stage]
        last = "  LAST ATTEMPT: if this one fails, escalate to checkpoint-2" if n >= cap else ""
        print(f"{stage} round {s['rounds'][stage]} (attempt {n} of {cap}){last}")
    elif stage in WORK_STAGES:
        print(f"{stage} round {s['rounds'][stage]}")
    else:
        print(stage)


def cmd_set(run_id, field, value):
    path, s = load(run_id)
    if field not in ("input_summary", "pr_url"):
        die(f"cannot set '{field}'; allowed: input_summary, pr_url")
    s[field] = None if value == "null" else value
    save(path, s)
    print(f"{field} = {s[field]}")


def cmd_feedback(run_id, heading):
    path, _ = load(run_id)
    text = sys.stdin.read().strip()
    if not text:
        die("no feedback text on stdin")
    with (path.parent / "feedback.md").open("a") as f:
        f.write(f"## {heading}\n{text}\n\n")
    print(f"appended to {path.parent / 'feedback.md'}")


def cmd_check_verification(run_id):
    path, _ = load(run_id)
    folder = path.parent
    try:
        spec = (folder / "spec.md").read_text()
        verification = (folder / "verification.md").read_text()
    except OSError as exc:
        die(f"verification input missing or unreadable: {exc.filename}")
    expected = re.findall(r"^- \[[ xX]\] (AC\d+):", spec, re.MULTILINE)
    if not expected or len(expected) != len(set(expected)):
        die("spec must contain unique acceptance criteria named AC1, AC2, ...")
    verdicts = re.findall(r"^Verdict: (\S+)$", verification, re.MULTILINE)
    suites = re.findall(r"^Suite: (\S+)$", verification, re.MULTILINE)
    if len(verdicts) != 1 or verdicts[0] not in ("pass", "fail"):
        die("verification must contain one Verdict: pass | fail")
    if len(suites) != 1 or suites[0] not in ("pass", "fail", "unverifiable"):
        die("verification must contain one Suite: pass | fail | unverifiable")
    sections = re.split(r"^### (AC\d+):[^\n]*\n", verification, flags=re.MULTILINE)
    found = {}
    for i in range(1, len(sections), 2):
        criterion = sections[i]
        results = re.findall(r"^Result:.*$", sections[i + 1], re.MULTILINE)
        if criterion in found or len(results) != 1 or results[0] not in ("Result: verified", "Result: failed", "Result: unverifiable"):
            die(f"{criterion} must have exactly one valid Result line")
        found[criterion] = results[0][8:]
    if set(found) != set(expected) or len(found) != len(expected):
        die(f"criterion mismatch: expected {expected}, found {list(found)}")
    if verdicts[0] == "pass" and (suites[0] != "pass" or any(result != "verified" for result in found.values())):
        die("pass requires a passing suite and every criterion verified")
    if verdicts[0] == "fail" and suites[0] == "pass" and all(result == "verified" for result in found.values()):
        die("fail must identify a failed or unverifiable check")
    print(json.dumps({"verdict": verdicts[0], "suite": suites[0], "results": found}))


COMMANDS = {
    "new": (cmd_new, 2), "list": (cmd_list, 0), "show": (None, 1), "get": (cmd_get, 2),
    "advance": (cmd_advance, 2), "set": (cmd_set, 3), "feedback": (cmd_feedback, 2),
    "check-verification": (cmd_check_verification, 1),
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    name, args = argv[0], argv[1:]
    if name == "show":
        if len(args) != 1:
            die("usage: show <run-id>")
        print(load(args[0])[0].read_text(), end="")
        return
    fn, nargs = COMMANDS[name]
    if name == "advance" and len(args) in (2, 3):
        return fn(*args)
    if len(args) != nargs:
        die(f"'{name}' takes {nargs} argument(s); see --help")
    fn(*args)


if __name__ == "__main__":
    main(sys.argv[1:])

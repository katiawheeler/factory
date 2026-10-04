#!/usr/bin/env python3
"""Run-state helper for the /factory skill.

All run state lives in <repo>/.factory/runs/<run-id>/. This script is the only
thing that writes state.json, so stage names and terminal completion are
checked and every transition is logged the same way.

Usage:
  state.py new <slug> <input-file|-> [--in-place]
                                         create a run in its own worktree (or in the
                                         current checkout); prints run id, folder, repo
  state.py list                          list runs with stage and summary
  state.py show <run-id>                 print state.json
  state.py get <run-id> <field>          print one field (dotted, e.g. rounds.review)
  state.py advance <run-id> <stage> [note]
                                         move to <stage>, log it, bump the round
                                         counter for work stages; prints the round
  state.py set <run-id> <field> <value>  set a top-level field (e.g. pr_url, report_url)
  state.py feedback <run-id> <heading>   append stdin to feedback.md under "## <heading>"
  state.py check-verification <run-id>    validate criterion and suite results
  state.py pr-report <run-id> [--no-media]
                                         write pr-report.md, the PR comment with spec,
                                         evidence and review; print the gh command that
                                         posts it with screenshots attached
  state.py release <run-id>              remove a finished run's worktree (keeps the branch)

Entering checkpoint-1, checkpoint-2, blocked or done runs the notify command, if one
is set in $FACTORY_NOTIFY or `git config factory.notify`, with the event JSON on stdin.
"""
import datetime
import json
import os
import re
import shlex
import shutil
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
NOTIFY_STAGES = HUMAN_STAGES | {"done"}
SETTABLE = ("input_summary", "pr_url", "report_url")
# GitHub rejects comments over 65536 characters.
REPORT_LIMIT = 60000
# Evidence that `gh ... --attach` (gh 2.99+) uploads, with GitHub's per-file size limits.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
VIDEO_EXTS = {".mp4", ".mov", ".webm"}
MAX_BYTES = {"image": 10 * 1024 * 1024, "video": 100 * 1024 * 1024}
MAX_ATTACHMENTS = 20


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


def main_root():
    """The main checkout, so every worktree shares one .factory/ folder."""
    r = git("worktree", "list", "--porcelain")
    first = r.stdout.split("\n\n")[0].splitlines() if r.returncode == 0 else []
    if first and first[0].startswith("worktree ") and "bare" not in first:
        return Path(first[0][len("worktree "):])
    return repo_root()


def runs_dir():
    return main_root() / ".factory" / "runs"


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


def cmd_new(slug, source, in_place=False):
    root = repo_root()
    status = git("status", "--porcelain")
    if status.returncode != 0:
        die(status.stderr.strip() or "could not inspect working tree")
    if in_place and status.stdout.strip():
        die("working tree is dirty; commit or stash first, or drop --in-place to use a worktree")
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
    branch = f"factory/{run_id}"
    worktree = None
    if not in_place:
        # The run gets its own checkout, so the human's checkout never changes branch.
        worktree = main_root() / ".factory" / "worktrees" / run_id
        r = git("worktree", "add", "-q", "-b", branch, str(worktree), base)
        if r.returncode != 0:
            die(f"could not create worktree: {r.stderr.strip()}; retry with --in-place to use this checkout")
    folder = runs_dir() / run_id
    folder.mkdir(parents=True)
    (folder / "input.md").write_text(text if text.endswith("\n") else text + "\n")

    state = {
        "id": run_id,
        "input_summary": "",
        "repo": str(worktree or root),
        "worktree": str(worktree) if worktree else None,
        "stage": "triage",
        "base_branch": base,
        "branch": branch,
        "rounds": {s: 0 for s in sorted(WORK_STAGES)},
        "loop": {s: 0 for s in sorted(ROUND_CAP)},
        "pr_url": None,
        "report_url": None,
        "history": [{"at": now(), "from": None, "to": "triage", "note": "run created"}],
    }
    save(folder / "state.json", state)
    print(run_id)
    print(folder)
    print(state["repo"])


def cmd_list():
    d = runs_dir()
    runs = sorted(d.glob("*/state.json")) if d.exists() else []
    if not runs:
        print("no runs")
        return
    for p in runs:
        s = json.loads(p.read_text())
        wait = ""
        if s["stage"] in HUMAN_STAGES:
            since = datetime.datetime.strptime(s["history"][-1]["at"], "%Y-%m-%dT%H:%M:%SZ")
            since = since.replace(tzinfo=datetime.timezone.utc)
            wait = f"  ⏸ waiting on human for {ago(datetime.datetime.now(datetime.timezone.utc) - since)}"
        print(f"{s['id']}  [{s['stage']}]  {s.get('input_summary', '')}{wait}")


def ago(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


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
    if stage in NOTIFY_STAGES:
        notify(path.parent, s)


def notify(folder, s):
    """Tell the human a run needs them. A failed notification never fails the advance."""
    cmd = os.environ.get("FACTORY_NOTIFY") or git("config", "--get", "factory.notify").stdout.strip()
    if not cmd:
        return
    last = s["history"][-1]
    if s["stage"] == "done":
        message = f"factory {s['id']}: PR opened {s.get('pr_url')}"
    else:
        message = f"factory {s['id']}: waiting on you at {s['stage']}"
        if last["note"]:
            message += f" ({last['note']})"
    if s.get("input_summary"):
        message += f" - {s['input_summary']}"
    event = {
        "id": s["id"], "stage": s["stage"], "from": last["from"], "note": last["note"],
        "message": message, "input_summary": s.get("input_summary", ""), "pr_url": s.get("pr_url"),
        "run_folder": str(folder), "repo": s["repo"], "at": last["at"],
    }
    env = {**os.environ, "FACTORY_RUN_ID": s["id"], "FACTORY_STAGE": s["stage"], "FACTORY_MESSAGE": message}
    try:
        r = subprocess.run(cmd, shell=True, input=json.dumps(event), env=env, cwd=main_root(),
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("warning: notify command timed out after 30s", file=sys.stderr)
        return
    if r.returncode != 0:
        print(f"warning: notify command exited {r.returncode}: {r.stderr.strip()[:200]}", file=sys.stderr)


def cmd_set(run_id, field, value):
    path, s = load(run_id)
    if field not in SETTABLE:
        die(f"cannot set '{field}'; allowed: {', '.join(SETTABLE)}")
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


class Invalid(Exception):
    pass


def verification_results(folder):
    """Validate verification.md against spec.md; return (verdict, suite, {ACn: result})."""
    try:
        spec = (folder / "spec.md").read_text()
        verification = (folder / "verification.md").read_text()
    except OSError as exc:
        raise Invalid(f"verification input missing or unreadable: {exc.filename}")
    expected = re.findall(r"^- \[[ xX]\] (AC\d+):", spec, re.MULTILINE)
    if not expected or len(expected) != len(set(expected)):
        raise Invalid("spec must contain unique acceptance criteria named AC1, AC2, ...")
    verdicts = re.findall(r"^Verdict: (\S+)$", verification, re.MULTILINE)
    suites = re.findall(r"^Suite: (\S+)$", verification, re.MULTILINE)
    if len(verdicts) != 1 or verdicts[0] not in ("pass", "fail"):
        raise Invalid("verification must contain one Verdict: pass | fail")
    if len(suites) != 1 or suites[0] not in ("pass", "fail", "unverifiable"):
        raise Invalid("verification must contain one Suite: pass | fail | unverifiable")
    sections = re.split(r"^### (AC\d+):[^\n]*\n", verification, flags=re.MULTILINE)
    found = {}
    for i in range(1, len(sections), 2):
        criterion = sections[i]
        results = re.findall(r"^Result:.*$", sections[i + 1], re.MULTILINE)
        if criterion in found or len(results) != 1 or results[0] not in ("Result: verified", "Result: failed", "Result: unverifiable"):
            raise Invalid(f"{criterion} must have exactly one valid Result line")
        found[criterion] = results[0][8:]
    if set(found) != set(expected) or len(found) != len(expected):
        raise Invalid(f"criterion mismatch: expected {expected}, found {list(found)}")
    if verdicts[0] == "pass" and (suites[0] != "pass" or any(result != "verified" for result in found.values())):
        raise Invalid("pass requires a passing suite and every criterion verified")
    if verdicts[0] == "fail" and suites[0] == "pass" and all(result == "verified" for result in found.values()):
        raise Invalid("fail must identify a failed or unverifiable check")
    return verdicts[0], suites[0], found


def cmd_check_verification(run_id):
    path, _ = load(run_id)
    try:
        verdict, suite, found = verification_results(path.parent)
    except Invalid as exc:
        die(str(exc))
    print(json.dumps({"verdict": verdict, "suite": suite, "results": found}))


def read(folder, name):
    p = folder / name
    return p.read_text().strip() if p.exists() else ""


def gh_can_attach():
    if not shutil.which("gh"):
        return False
    r = subprocess.run(["gh", "pr", "comment", "--help"], capture_output=True, text=True)
    return "--attach" in r.stdout


def media_section(folder, criteria, attach):
    """Screenshots and recordings grouped by criterion; returns (markdown lines, paths to attach)."""
    files = sorted(p for p in (folder / "evidence").rglob("*")
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS
                   and "node_modules" not in p.relative_to(folder).parts) \
        if (folder / "evidence").exists() else []
    if not files:
        return [], []
    if not attach:
        names = ", ".join(f"`{p.relative_to(folder).as_posix()}`" for p in files)
        return ["", "### Screenshots and recordings", "",
                f"Not uploaded; they are in the run folder: {names}"], []
    uploads, skipped = [], []
    for p in files:
        kind = "image" if p.suffix.lower() in IMAGE_EXTS else "video"
        if p.stat().st_size > MAX_BYTES[kind] or len(uploads) >= MAX_ATTACHMENTS:
            skipped.append(p)
        else:
            uploads.append(p)
    groups = {}
    for p in uploads:
        m = re.match(r"(AC\d+)[-_ .]", p.name, re.IGNORECASE)
        groups.setdefault(m.group(1).upper() if m else None, []).append(p)
    out = ["", "### Screenshots and recordings"]
    for ac in sorted(groups, key=lambda k: (k is None, int(k[2:]) if k else 0)):
        out += ["", f"**{ac}** {criteria.get(ac, '')}".rstrip() if ac else "**Other**"]
        for p in groups[ac]:
            rel = p.relative_to(folder).as_posix()
            alt = re.sub(r"^AC\d+[-_ .]*", "", p.stem, flags=re.IGNORECASE)
            alt = re.sub(r"^0*(\d+)[-_ .]+", r"step \1: ", alt).replace("-", " ").replace("_", " ")
            # gh rewrites each local reference in place; a video renders as a player only alone in its paragraph.
            out += ["", f"![{ac + ': ' if ac else ''}{alt}]({rel})" if p.suffix.lower() in IMAGE_EXTS else rel]
    if skipped:
        names = ", ".join(f"`{p.relative_to(folder).as_posix()}`" for p in skipped)
        out += ["", f"_Not uploaded (over GitHub's size limit or the {MAX_ATTACHMENTS}-file cap); "
                    f"in the run folder: {names}_"]
    return out, [p.relative_to(folder).as_posix() for p in uploads]


def cmd_pr_report(run_id, no_media=False):
    """The PR comment: what a reviewer needs to trust the run without the run folder."""
    path, s = load(run_id)
    folder = path.parent
    if not s.get("pr_url"):
        die("set pr_url before writing the PR report")
    spec, review, feedback = read(folder, "spec.md"), read(folder, "review.md"), read(folder, "feedback.md")
    verification = read(folder, "verification.md")
    criteria = dict(re.findall(r"^- \[[ xX]\] (AC\d+): *(.*?)(?: *\*\*Verify by:\*\*.*)?$", spec, re.MULTILINE))
    icons = {"verified": "✅", "failed": "❌", "unverifiable": "⚠️"}
    out = [f"<!-- factory-run: {s['id']} -->", f"## Factory run `{s['id']}`", ""]
    if s.get("input_summary"):
        out += [s["input_summary"], ""]
    try:
        verdict, suite, found = verification_results(folder)
        out += [f"**Verification:** {verdict} · test suite {suite}", "",
                "| Criterion | Result |", "|---|---|"]
        for ac, result in found.items():
            text = criteria.get(ac, "").replace("|", "\\|")
            out.append(f"| **{ac}** {text} | {icons[result]} {result} |")
        out.append("")
    except Invalid as exc:
        out += [f"**Verification:** report did not validate ({exc})", ""]
    m = re.search(r"^Verdict: (\S+)$", review, re.MULTILINE)
    if m:
        out += [f"**Review:** {m.group(1)}", ""]
    r = s["rounds"]
    out += [f"**Rounds:** spec {r['spec']} · implement {r['implement']} · review {r['review']} · verify {r['verify']}", ""]
    gh = shutil.which("gh")
    tail, uploads = media_section(folder, criteria, attach=not no_media and gh_can_attach())
    budget = REPORT_LIMIT - len("\n".join(out + tail))
    for title, name, body in (("Approved spec", "spec.md", spec), ("Verification evidence", "verification.md", verification),
                              ("Code review", "review.md", review), ("Human feedback", "feedback.md", feedback)):
        if not body:
            continue
        wrapper = f"<details><summary>{title}</summary>\n\n\n\n</details>\n"
        room = budget - len(wrapper)
        if room < 200:
            body = f"_Omitted to fit GitHub's comment limit; see `{name}` in the run folder._"
        elif len(body) > room:
            note = f"\n\n_Truncated to fit GitHub's comment limit; the full file is `{name}` in the run folder._"
            body = body[:room - len(note)] + note
        section = f"<details><summary>{title}</summary>\n\n{body}\n\n</details>\n"
        budget -= len(section)
        out.append(section)
    report = folder / "pr-report.md"
    report.write_text("\n".join(out + tail).rstrip() + "\n")
    print(report)
    if gh:
        # Run from the run folder: --attach paths and the references in the body are relative to it.
        attach = "".join(f" --attach {shlex.quote(u)}" for u in uploads)
        print(f"cd {shlex.quote(str(folder))} && gh pr comment {shlex.quote(s['pr_url'])} --body-file pr-report.md{attach}")


def cmd_release(run_id):
    """Remove the run's worktree once the branch is pushed or the run is abandoned."""
    path, s = load(run_id)
    wt = s.get("worktree")
    if not wt:
        print("run uses the main checkout; nothing to release")
        return
    if s["stage"] not in ("done", "aborted"):
        die(f"run is at '{s['stage']}'; release only a done or aborted run")
    if not Path(wt).exists():
        git("worktree", "prune")
        print(f"worktree {wt} already removed")
        return
    r = subprocess.run(["git", "worktree", "remove", wt], cwd=main_root(), capture_output=True, text=True)
    if r.returncode != 0:
        die(f"could not remove worktree {wt}: {r.stderr.strip()}; commit, stash or delete its changes first")
    print(f"removed worktree {wt}; branch {s['branch']} is kept")


COMMANDS = {
    "new": (cmd_new, 2), "list": (cmd_list, 0), "show": (None, 1), "get": (cmd_get, 2),
    "advance": (cmd_advance, 2), "set": (cmd_set, 3), "feedback": (cmd_feedback, 2),
    "check-verification": (cmd_check_verification, 1), "pr-report": (cmd_pr_report, 1),
    "release": (cmd_release, 1),
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
    if name == "new" and args[2:] == ["--in-place"]:
        return fn(*args[:2], in_place=True)
    if name == "pr-report" and args[1:] == ["--no-media"]:
        return fn(args[0], no_media=True)
    if name == "advance" and len(args) in (2, 3):
        return fn(*args)
    if len(args) != nargs:
        die(f"'{name}' takes {nargs} argument(s); see --help")
    fn(*args)


if __name__ == "__main__":
    main(sys.argv[1:])

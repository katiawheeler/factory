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
  state.py set <run-id> <field> <value>  set a top-level field (e.g. pr_url, report_url, thread_url)
  state.py feedback <run-id> <heading>   append stdin to feedback.md under "## <heading>"
  state.py check-verification <run-id>    validate criterion and suite results
  state.py pr-report <run-id> [--no-media]
                                         write pr-report.md, the PR comment with spec,
                                         evidence and review; print the gh command that
                                         posts it with screenshots attached
  state.py pr-status <run-id>            print the PR's state, CI, mergeability and review decision as JSON
                                         (needs gh; REST only)
  state.py inbox <run-id>                print new comments on the issue and PR as JSON: /factory
                                         answers from people with write access, and feedback
  state.py checkpoint-post <run-id>      write checkpoint-post.md for the current checkpoint and print
                                         the gh command that posts it on the issue or PR
  state.py pr-update <run-id>            write pr-update.md, the PR comment for a pushed fix round
  state.py usage <run-id> <stage> <tokens> [usd]
                                         record what a stage dispatch cost
  state.py stats [<run-id>] [--json]     time, rounds, outcomes and cost, for one run or all runs
  state.py release <run-id>              remove a finished run's worktree (keeps the branch)

Entering checkpoint-1, checkpoint-2, blocked, land, merged or closed runs the notify command,
if one is set in $FACTORY_NOTIFY or `git config factory.notify`, with the event JSON on stdin.
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
    "checkpoint-2", "ship", "land", "merged", "closed", "done", "aborted", "blocked",
]
WORK_STAGES = {"spec", "implement", "review", "verify"}
ROUND_CAP = {"review": 3, "verify": 3}
# PR fix rounds (land -> implement) allowed between human checkpoints.
LAND_CAP = 3
# Entering one of these hands the run to a human, which resets the loop counters.
HUMAN_STAGES = {"checkpoint-1", "checkpoint-2", "blocked"}
# done is the terminal stage of runs shipped before the land stage existed.
TERMINAL = {"merged", "closed", "done", "aborted"}
NOTIFY_STAGES = HUMAN_STAGES | {"land", "merged", "closed", "done"}
SETTABLE = ("input_summary", "pr_url", "report_url", "thread_url")
CAP_NOTES = ("review did not converge", "verify did not pass", "PR feedback did not converge")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
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
    return datetime.datetime.now(datetime.timezone.utc).strftime(TIME_FORMAT)


def parse_at(at):
    return datetime.datetime.strptime(at, TIME_FORMAT).replace(tzinfo=datetime.timezone.utc)


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
        "rounds": {s: 0 for s in sorted(WORK_STAGES | {"land"})},
        "loop": {s: 0 for s in sorted(set(ROUND_CAP) | {"land"})},
        "pr_url": None,
        "report_url": None,
        "thread_url": None,
        "usage": [],
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
        waited = ago(datetime.datetime.now(datetime.timezone.utc) - parse_at(s["history"][-1]["at"]))
        if s["stage"] in HUMAN_STAGES:
            wait = f"  ⏸ waiting on human for {waited}"
        elif s["stage"] == "land":
            wait = f"  ⏳ PR open for review for {waited}: {s.get('pr_url')}"
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
    if s["stage"] in TERMINAL:
        die(f"run is {s['stage']}; it cannot advance")
    if stage == s["stage"]:
        die(f"run is already at '{stage}'; dispatch it without advancing")
    if stage in ("done", "land") and (s["stage"] != "ship" or not s.get("pr_url")):
        die(f"{stage} requires the ship stage and a recorded PR URL")
    if stage in ("merged", "closed") and not s.get("pr_url"):
        die(f"{stage} requires a recorded PR URL")
    # Shipping needs the human's ship answer, except to push fixes to a PR they already approved.
    if stage == "ship" and not (s["stage"] in ("checkpoint-2", "blocked") or (s["stage"] == "verify" and s.get("pr_url"))):
        die("ship comes after checkpoint-2, or after verify when pushing fixes to an open PR")
    loop = s.setdefault("loop", {k: 0 for k in sorted(ROUND_CAP)})
    loop.setdefault("land", 0)
    s["rounds"].setdefault("land", 0)
    if stage in WORK_STAGES:
        s["rounds"][stage] += 1
    if stage in ROUND_CAP:
        loop[stage] += 1
    pr_fix = s["stage"] == "land" and stage == "implement"
    if pr_fix:
        s["rounds"]["land"] += 1
        loop["land"] += 1
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
    elif pr_fix:
        n = loop["land"]
        last = "  LAST ATTEMPT: if the PR still needs fixes after this, escalate to checkpoint-2" if n >= LAND_CAP else ""
        print(f"implement round {s['rounds']['implement']} (PR fix {n} of {LAND_CAP}){last}")
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
    if s["stage"] == "done" or (s["stage"] == "land" and not s["rounds"].get("land")):
        message = f"factory {s['id']}: PR opened {s.get('pr_url')}"
    elif s["stage"] == "land":
        message = f"factory {s['id']}: pushed PR fixes to {s.get('pr_url')}"
    elif s["stage"] == "merged":
        message = f"factory {s['id']}: PR merged {s.get('pr_url')}"
    elif s["stage"] == "closed":
        message = f"factory {s['id']}: PR closed without merging {s.get('pr_url')}"
    else:
        message = f"factory {s['id']}: waiting on you at {s['stage']}"
        if last["note"]:
            message += f" ({last['note']})"
    if s.get("input_summary"):
        message += f" - {s['input_summary']}"
    event = {
        "id": s["id"], "stage": s["stage"], "from": last["from"], "note": last["note"],
        "message": message, "input_summary": s.get("input_summary", ""), "pr_url": s.get("pr_url"),
        "thread_url": s.get("thread_url"),
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


def step_number(p):
    """The step in a flow screenshot's name (AC2-03-banner.png -> 3), or -1 for an unnumbered one."""
    m = re.match(r"AC\d+[-_ .]+0*(\d+)[-_ .]", p.name, re.IGNORECASE)
    return int(m.group(1)) if m else -1


def pick_media(files):
    """One screenshot and one recording per criterion, the rest stay in the run folder.

    The screenshot is the main flow's last numbered step (its end state); the recording is AC<n>-flow.
    Edge-case media are used only when a criterion has nothing else. Files without a criterion are all kept.
    """
    groups, picked = {}, []
    for p in files:
        m = re.match(r"(AC\d+)[-_ .]", p.name, re.IGNORECASE)
        if m:
            groups.setdefault(m.group(1).upper(), []).append(p)
        else:
            picked.append(p)
    for ps in groups.values():
        for exts in (IMAGE_EXTS, VIDEO_EXTS):
            kind = [p for p in ps if p.suffix.lower() in exts]
            main = [p for p in kind if "edge" not in p.stem.lower()] or kind
            if main:
                flow = [p for p in main if re.fullmatch(r"AC\d+[-_ .]flow", p.stem, re.IGNORECASE)]
                picked.append(flow[0] if flow else max(main, key=lambda p: (step_number(p), p.name)))
    return sorted(picked)


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
    picked = pick_media(files)
    uploads, skipped = [], []
    for p in picked:
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
    if len(files) > len(picked):
        out += ["", f"_{len(files) - len(picked)} more screenshots and recordings (earlier steps and edge cases) "
                    "are in the run folder's `evidence/`._"]
    return out, [p.relative_to(folder).as_posix() for p in uploads]


def spec_criteria(spec):
    return dict(re.findall(r"^- \[[ xX]\] (AC\d+): *(.*?)(?: *\*\*Verify by:\*\*.*)?$", spec, re.MULTILINE))


def results_summary(folder, s):
    """The verification table, review verdict and rounds, as markdown lines."""
    criteria = spec_criteria(read(folder, "spec.md"))
    icons = {"verified": "✅", "failed": "❌", "unverifiable": "⚠️"}
    out = []
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
    m = re.search(r"^Verdict: (\S+)$", read(folder, "review.md"), re.MULTILINE)
    if m:
        out += [f"**Review:** {m.group(1)}", ""]
    r = s["rounds"]
    rounds = f"**Rounds:** spec {r['spec']} · implement {r['implement']} · review {r['review']} · verify {r['verify']}"
    if r.get("land"):
        rounds += f" · PR fixes {r['land']}"
    return out + [rounds, ""]


def details_sections(budget, files):
    """Collapsible sections for (title, file name, body), truncated to fit the comment budget."""
    out = []
    for title, name, body in files:
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
    return out


def cmd_pr_report(run_id, no_media=False):
    """The PR comment: what a reviewer needs to trust the run without the run folder."""
    path, s = load(run_id)
    folder = path.parent
    if not s.get("pr_url"):
        die("set pr_url before writing the PR report")
    spec, review, feedback = read(folder, "spec.md"), read(folder, "review.md"), read(folder, "feedback.md")
    verification = read(folder, "verification.md")
    criteria = spec_criteria(spec)
    out = [f"<!-- factory-run: {s['id']} -->", f"## Factory run `{s['id']}`", ""]
    if s.get("input_summary"):
        out += [s["input_summary"], ""]
    out += results_summary(folder, s)
    gh = shutil.which("gh")
    attach = not no_media and gh_can_attach()
    tail, uploads = media_section(folder, criteria, attach=attach)
    if gh and tail and not no_media and not attach:
        print("warning: this gh has no `pr comment --attach` (added in gh 2.99), so media are only listed by name; "
              "upgrade gh and rerun pr-report", file=sys.stderr)
    out += details_sections(REPORT_LIMIT - len("\n".join(out + tail)), (
        ("Approved spec", "spec.md", spec), ("Verification evidence", "verification.md", verification),
        ("Code review", "review.md", review), ("Human feedback", "feedback.md", feedback)))
    report = folder / "pr-report.md"
    report.write_text("\n".join(out + tail).rstrip() + "\n")
    print(report)
    if gh:
        # Run from the run folder: --attach paths and the references in the body are relative to it.
        attach = "".join(f" --attach {shlex.quote(u)}" for u in uploads)
        print(f"cd {shlex.quote(str(folder))} && gh pr comment {shlex.quote(s['pr_url'])} --body-file pr-report.md{attach}")


GITHUB_URL = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)")
# Only these repository permissions can answer a checkpoint from a comment.
WRITE_PERMISSIONS = {"admin", "maintain", "write"}
# Every comment the factory posts carries this, so it never reads its own posts back as input.
MARKER = "<!-- factory"
COMMAND = re.compile(r"\A\s*/factory(?:\s+|\Z)(.*)\Z", re.DOTALL)


def github_thread(url):
    """(owner, repo, kind, number) for an issue or PR URL on github.com."""
    m = GITHUB_URL.match(url or "")
    if not m:
        die(f"not a GitHub issue or pull request URL: {url}")
    return m.group(1), m.group(2), m.group(3), int(m.group(4))


def gh(*args, optional=False):
    """Run gh and return its output. Only REST calls: some hosts block GitHub's GraphQL API."""
    if not shutil.which("gh"):
        die("gh is not installed; read GitHub with the available GitHub tooling instead")
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        if optional:
            return None
        die(f"gh {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def gh_items(endpoint, jq=".[]", optional=False):
    """Every item of a paginated GitHub list endpoint, or None if an optional call fails."""
    out = gh("api", "--paginate", endpoint, "--jq", jq, optional=optional)
    return None if out is None else [json.loads(line) for line in out.splitlines() if line.strip()]


def checks_summary(check_runs, statuses):
    """Reduce check runs and commit statuses to pass | fail | pending | none, with failing and pending names."""
    failing, pending = [], []
    for c in check_runs:
        if c.get("status") != "completed":
            pending.append(c["name"])
        elif c.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required", "startup_failure"):
            failing.append({"name": c["name"], "url": c.get("details_url") or c.get("html_url")})
    for c in statuses:
        if c.get("state") in ("failure", "error"):
            failing.append({"name": c["context"], "url": c.get("target_url")})
        elif c.get("state") == "pending":
            pending.append(c["context"])
    ci = "fail" if failing else "pending" if pending else "pass" if check_runs or statuses else "none"
    return ci, failing, pending


def review_decision(reviews):
    """GitHub's review decision from the reviews: each reviewer's latest approval or change request counts."""
    latest = {}
    for r in reviews:
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(r.get("user") or {}).get("login")] = r["state"]
    states = set(latest.values())
    return "CHANGES_REQUESTED" if "CHANGES_REQUESTED" in states else "APPROVED" if "APPROVED" in states else None


def cmd_pr_status(run_id):
    """What the PR needs from the land stage: merged or closed, red CI, a conflict, or nothing yet."""
    _, s = load(run_id)
    if not s.get("pr_url"):
        die("no pr_url recorded for this run")
    owner, repo, _, number = github_thread(s["pr_url"])
    base = f"repos/{owner}/{repo}"
    pr = json.loads(gh("api", f"{base}/pulls/{number}"))
    head = pr["head"]["sha"]
    check_runs = gh_items(f"{base}/commits/{head}/check-runs", ".check_runs[]")
    statuses = gh_items(f"{base}/commits/{head}/status", ".statuses[]", optional=True)
    ci, failing, pending = checks_summary(check_runs, statuses or [])
    mergeable = {True: "MERGEABLE", False: "CONFLICTING"}.get(pr.get("mergeable"), "UNKNOWN")
    if pr.get("mergeable_state") == "dirty":
        mergeable = "CONFLICTING"
    status = {
        "state": "MERGED" if pr.get("merged") else pr["state"].upper(), "draft": pr.get("draft"),
        "mergeable": mergeable, "merge_state": pr.get("mergeable_state"),
        "review_decision": review_decision(gh_items(f"{base}/pulls/{number}/reviews")),
        "head": head, "ci": ci, "failing": failing, "pending": pending,
    }
    if statuses is None:
        status["note"] = "commit statuses could not be read; only check runs are counted"
    print(json.dumps(status, indent=2))


def inbox_since(s):
    """When the run last took input: entering a human stage, or leaving land to fix the PR."""
    h = s["history"]
    if s["stage"] == "land":
        left = [e["at"] for e in h if e["from"] == "land"]
        return left[-1] if left else [e["at"] for e in h if e["to"] == "ship"][-1]
    return h[-1]["at"]


class Trust:
    """Who may answer a checkpoint: `factory.approvers` if set, otherwise anyone with write access.

    Where the permission lookup is blocked (some hosts proxy GitHub), only the repository's owner counts.
    """

    def __init__(self):
        raw = os.environ.get("FACTORY_APPROVERS") or git("config", "--get", "factory.approvers").stdout.strip()
        self.allow = {x.lower() for x in re.split(r"[,\s]+", raw) if x} or None
        self.cache = {}

    def __call__(self, owner, repo, login, association):
        if self.allow is not None:
            return login.lower() in self.allow
        if (owner, repo, login) not in self.cache:
            r = subprocess.run(["gh", "api", f"repos/{owner}/{repo}/collaborators/{login}/permission",
                                "--jq", ".permission"], capture_output=True, text=True)
            if r.returncode == 0:
                self.cache[owner, repo, login] = r.stdout.strip() in WRITE_PERMISSIONS
            elif "HTTP 404" in r.stderr:
                self.cache[owner, repo, login] = False  # not a collaborator
            else:
                # The permission lookup isn't available here: trust only the repository's owner.
                self.cache[owner, repo, login] = association == "OWNER"
        return self.cache[owner, repo, login]


def thread_items(url, since):
    """Comments, reviews and inline review comments on an issue or PR, created after `since`."""
    owner, repo, kind, number = github_thread(url)
    base = f"repos/{owner}/{repo}"
    items = [("comment", c, c.get("created_at")) for c in gh_items(f"{base}/issues/{number}/comments?since={since}")]
    if kind == "pull":
        items += [("review", r, r.get("submitted_at")) for r in gh_items(f"{base}/pulls/{number}/reviews")]
        items += [("inline", c, c.get("created_at")) for c in gh_items(f"{base}/pulls/{number}/comments?since={since}")]
    out = []
    for kind_, c, at in items:
        body = (c.get("body") or "").strip()
        if not at or at <= since or MARKER in body:
            continue
        if kind_ == "review" and not body and c.get("state") != "CHANGES_REQUESTED":
            continue  # an approval or the empty wrapper around inline comments
        user = c.get("user") or {}
        item = {"kind": kind_, "author": user.get("login", "?"), "association": c.get("author_association"),
                "bot": user.get("type") == "Bot" or user.get("login", "").endswith("[bot]"),
                "at": at, "url": c.get("html_url"), "body": body}
        if kind_ == "review":
            item["state"] = c.get("state")
        if kind_ == "inline":
            item.update(path=c.get("path"), line=c.get("line") or c.get("original_line"))
        out.append((owner, repo, item))
    return out


def cmd_inbox(run_id):
    """New human input on GitHub: /factory answers from trusted people, and PR feedback.

    Comment text is untrusted input. Only people with write access (or listed in
    factory.approvers) can answer a checkpoint; everyone else's /factory comment is ignored.
    """
    _, s = load(run_id)
    urls = list(dict.fromkeys(u for u in (s.get("thread_url"), s.get("pr_url")) if u))
    if not urls:
        die("no thread_url or pr_url recorded for this run")
    since, trust = inbox_since(s), Trust()
    answers, feedback, ignored = [], [], []
    for url in urls:
        for owner, repo, item in thread_items(url, since):
            m = COMMAND.match(item["body"])
            trusted = not item["bot"] and trust(owner, repo, item["author"], item["association"])
            if m:
                entry = {"answer": m.group(1).strip(), "author": item["author"], "at": item["at"], "url": item["url"]}
                if not m.group(1).strip():
                    ignored.append({**entry, "reason": "empty /factory command"})
                elif trusted:
                    answers.append(entry)
                else:
                    ignored.append({**entry, "reason": "bot" if item["bot"] else "no write access"})
            elif url == s.get("pr_url"):
                feedback.append({**item, "trusted": trusted})
    key = lambda e: e["at"]
    print(json.dumps({"since": since, "threads": urls, "answers": sorted(answers, key=key),
                      "feedback": sorted(feedback, key=key), "ignored": sorted(ignored, key=key)}, indent=2))


def posting_enabled():
    value = os.environ.get("FACTORY_POST_CHECKPOINTS") or git("config", "--get", "factory.postCheckpoints").stdout
    return value.strip().lower() in ("1", "true", "yes", "on")


def cmd_checkpoint_post(run_id):
    """The comment that puts the current checkpoint on the issue or PR, answerable with /factory."""
    path, s = load(run_id)
    folder, stage = path.parent, s["stage"]
    if stage not in HUMAN_STAGES:
        die(f"run is at '{stage}', not a checkpoint")
    target = s.get("pr_url") or s.get("thread_url")
    if not target:
        die("no thread_url or pr_url recorded for this run")
    owner, repo, kind, _ = github_thread(target)
    if not s.get("pr_url") and not posting_enabled():
        die("posting checkpoints on the issue is off; enable it with `git config factory.postCheckpoints true`")
    last = s["history"][-1]
    note = last["note"] or ""
    out = [f"{MARKER}-checkpoint: {s['id']} {stage} -->", f"## Factory run `{s['id']}` is waiting on you", ""]
    if s.get("input_summary"):
        out += [s["input_summary"], ""]
    if stage == "checkpoint-1" and note.startswith("triage"):
        title = "Triage rejected this request" if note == "triage rejected" else "Triage has questions"
        out += [f"### {title}", ""]
        files = [("Triage", "triage.md", read(folder, "triage.md"))]
        replies = ["`/factory approve` to use triage's suggested defaults" if note == "triage questions"
                   else "`/factory <override>` to take the work on anyway, with your direction",
                   "`/factory <your answers>` to answer the questions"]
    elif stage == "checkpoint-1":
        out += ["### Spec approval", "", "The complete spec is below. Approve it, or say what to change.", ""]
        files = []
        out += [read(folder, "spec.md"), ""]
        replies = ["`/factory approve` to approve the spec", "`/factory <changes>` to request a revision"]
    elif stage == "checkpoint-2":
        out += ["### Ship approval", ""]
        if note:
            out += [f"**Note:** {note}", ""]
        out += results_summary(folder, s)
        diff = subprocess.run(["git", "diff", "--stat", f"{s['base_branch']}...{s['branch']}"],
                              cwd=s["repo"] if Path(s["repo"]).exists() else main_root(),
                              capture_output=True, text=True)
        if diff.returncode == 0 and diff.stdout.strip():
            out += ["```text", diff.stdout.rstrip(), "```", ""]
        files = [("Verification evidence", "verification.md", read(folder, "verification.md")),
                 ("Code review", "review.md", read(folder, "review.md"))]
        replies = ["`/factory ship` to open the PR" if not s.get("pr_url") else "`/factory ship` to push this round to the PR",
                   "`/factory <feedback>` to send the work back"]
    else:
        out += [f"### Blocked at {last['from']}", "", note or "A stage needs a human decision.", ""]
        files = []
        replies = ["`/factory <answer>` to answer and continue"]
    replies.append("`/factory abort` to stop the run")
    footer = ["", "Reply with one of:", ""] + [f"- {r}" for r in replies] + [
        "", "_Only replies from people with write access to this repository (or listed in `factory.approvers`) are read._"]
    out += details_sections(REPORT_LIMIT - len("\n".join(out + footer)), files)
    body = "\n".join(out + footer)
    if len(body) > REPORT_LIMIT:
        cut = "\n\n_Truncated to fit GitHub's comment limit; see the run folder._\n"
        head = "\n".join(out)[:REPORT_LIMIT - len(cut) - len("\n".join(footer))]
        body = head + cut + "\n".join(footer)
    post = folder / "checkpoint-post.md"
    post.write_text(body.rstrip() + "\n")
    print(post)
    noun = "pr" if kind == "pull" else "issue"
    print(f"gh {noun} comment {shlex.quote(target)} --body-file {shlex.quote(str(post))}")


def cmd_pr_update(run_id):
    """The PR comment for a pushed fix round: what was addressed and the new verification results."""
    path, s = load(run_id)
    folder = path.parent
    if not s.get("pr_url"):
        die("set pr_url before writing a PR update")
    n = s["rounds"].get("land", 0)
    if not n:
        die("no PR fix round yet; the first push is covered by pr-report")
    m = re.search(r"^## Addressed\n(.*?)(?=^## |\Z)", read(folder, "implementation.md"), re.MULTILINE | re.DOTALL)
    addressed = m.group(1).strip() if m and m.group(1).strip() else "_See the commits in this push._"
    out = [f"{MARKER}-update: {s['id']} {n} -->", f"## Factory run `{s['id']}`: PR fix round {n}", "",
           "### Addressed", "", addressed, ""] + results_summary(folder, s)
    body = "\n".join(out)
    if len(body) > REPORT_LIMIT:
        body = body[:REPORT_LIMIT - 80] + "\n\n_Truncated; see `implementation.md` in the run folder._"
    post = folder / "pr-update.md"
    post.write_text(body.rstrip() + "\n")
    print(post)
    print(f"gh pr comment {shlex.quote(s['pr_url'])} --body-file {shlex.quote(str(post))}")


def cmd_usage(run_id, stage, tokens, usd=None):
    path, s = load(run_id)
    if stage not in STAGES:
        die(f"unknown stage '{stage}'")
    try:
        tokens = int(tokens)
        usd = float(usd) if usd is not None else None
    except ValueError:
        die("tokens must be an integer and usd a number")
    if tokens < 0 or (usd is not None and usd < 0):
        die("usage cannot be negative")
    rounds = s["rounds"].get(stage)
    s.setdefault("usage", []).append({"at": now(), "stage": stage, "round": rounds, "tokens": tokens, "usd": usd})
    save(path, s)
    total = sum(u["tokens"] for u in s["usage"])
    print(f"{stage}: {tokens} tokens; run total {total}")


def stage_seconds(s, until):
    """Seconds spent in each stage, from the history. A run still going counts up to `until`."""
    h, spent = s["history"], {}
    for a, b in zip(h, h[1:] + [None]):
        if b:
            end = parse_at(b["at"])
        elif a["to"] in TERMINAL:
            continue
        else:
            end = until
        spent[a["to"]] = spent.get(a["to"], 0) + max(0, (end - parse_at(a["at"])).total_seconds())
    return spent


def run_stats(s, until):
    h = s["history"]
    spent = stage_seconds(s, until)
    created = parse_at(h[0]["at"])
    first = lambda stages: next((parse_at(e["at"]) for e in h if e["to"] in stages), None)
    pr_at, merged_at = first(("land", "done")), first(("merged",))
    usage = s.get("usage", [])
    return {
        "id": s["id"], "stage": s["stage"], "summary": s.get("input_summary", ""), "pr_url": s.get("pr_url"),
        "rounds": s["rounds"],
        "seconds": {
            "agents": sum(v for k, v in spent.items() if k not in HUMAN_STAGES and k != "land"),
            "human": sum(v for k, v in spent.items() if k in HUMAN_STAGES),
            "pr_review": spent.get("land", 0),
            "by_stage": spent,
        },
        "to_pr": (pr_at - created).total_seconds() if pr_at else None,
        "to_merge": (merged_at - created).total_seconds() if merged_at else None,
        "spec_revisions": sum(e["from"] == "checkpoint-1" and e["to"] == "spec" and e["note"] == "revise" for e in h),
        "send_backs": sum(e["from"] == "checkpoint-2" and e["to"] in ("spec", "implement", "verify") for e in h),
        "cap_hits": sum((e["note"] or "").startswith(CAP_NOTES) for e in h),
        "reached_spec_approval": any(e["from"] == "checkpoint-1" and e["to"] == "implement" for e in h),
        "reached_ship_approval": any(e["from"] == "checkpoint-2" and e["to"] == "ship" for e in h),
        "tokens": sum(u["tokens"] for u in usage) if usage else None,
        "usd": sum(u["usd"] for u in usage if u.get("usd") is not None) if any(u.get("usd") is not None for u in usage) else None,
    }


def duration(seconds):
    return "-" if seconds is None else ago(datetime.timedelta(seconds=seconds))


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def cmd_stats(run_id=None, as_json=False):
    """The run ledger: where time went, how often humans sent work back, and what came of each PR."""
    until = datetime.datetime.now(datetime.timezone.utc)
    if run_id:
        st = run_stats(load(run_id)[1], until)
        if as_json:
            print(json.dumps(st, indent=2))
            return
        sec = st["seconds"]
        print(f"{st['id']}  [{st['stage']}]  {st['summary']}")
        print(f"agents {duration(sec['agents'])} · waiting on human {duration(sec['human'])} · "
              f"PR in review {duration(sec['pr_review'])}")
        print(f"to PR {duration(st['to_pr'])} · to merge {duration(st['to_merge'])}")
        print("rounds: " + " · ".join(f"{k} {v}" for k, v in st["rounds"].items()))
        print(f"spec revisions {st['spec_revisions']} · send-backs from ship approval {st['send_backs']} · "
              f"loop caps hit {st['cap_hits']}")
        if st["tokens"] is not None:
            print(f"tokens {st['tokens']}" + (f" · ${st['usd']:.2f}" if st["usd"] is not None else ""))
        for stage, secs in sorted(sec["by_stage"].items(), key=lambda kv: -kv[1]):
            print(f"  {stage:<13} {duration(secs)}")
        return
    d = runs_dir()
    runs = [run_stats(json.loads(p.read_text()), until) for p in sorted(d.glob("*/state.json"))] if d.exists() else []
    outcomes = {}
    for r in runs:
        key = r["stage"] if r["stage"] in TERMINAL else "open"
        outcomes[key] = outcomes.get(key, 0) + 1
    finished = outcomes.get("merged", 0) + outcomes.get("closed", 0)
    spec_ok = [r for r in runs if r["reached_spec_approval"]]
    ship_ok = [r for r in runs if r["reached_ship_approval"]]
    prs = [r for r in runs if r["to_pr"] is not None]
    priced = [r for r in runs if r["tokens"] is not None]
    summary = {
        "runs": len(runs), "outcomes": outcomes,
        "merge_rate": outcomes.get("merged", 0) / finished if finished else None,
        "spec_approved_first_time": sum(r["spec_revisions"] == 0 for r in spec_ok) / len(spec_ok) if spec_ok else None,
        "ship_approved_first_time": sum(r["send_backs"] == 0 for r in ship_ok) / len(ship_ok) if ship_ok else None,
        "cap_hits": sum(r["cap_hits"] for r in runs),
        "pr_fix_rounds_per_pr": sum(r["rounds"].get("land", 0) for r in prs) / len(prs) if prs else None,
        "median_seconds_to_pr": median(r["to_pr"] for r in runs),
        "median_seconds_to_merge": median(r["to_merge"] for r in runs),
        "median_human_wait_seconds": median(r["seconds"]["human"] for r in runs),
        "tokens": sum(r["tokens"] for r in priced) if priced else None,
        "usd": sum(r["usd"] for r in priced if r["usd"] is not None) if any(r["usd"] is not None for r in priced) else None,
        "runs_with_usage": len(priced),
    }
    if as_json:
        print(json.dumps({"summary": summary, "runs": runs}, indent=2))
        return
    if not runs:
        print("no runs")
        return
    pct = lambda x: "-" if x is None else f"{x:.0%}"
    print(f"runs: {len(runs)} (" + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())) + ")")
    print(f"merge rate: {pct(summary['merge_rate'])} of {finished} finished PRs")
    print(f"spec approved without revision: {pct(summary['spec_approved_first_time'])} of {len(spec_ok)}")
    print(f"ship approved without send-back: {pct(summary['ship_approved_first_time'])} of {len(ship_ok)}")
    print(f"loop caps hit: {summary['cap_hits']}")
    fixes = summary["pr_fix_rounds_per_pr"]
    print(f"PR fix rounds per PR: {'-' if fixes is None else f'{fixes:.1f}'}")
    print(f"median time to PR: {duration(summary['median_seconds_to_pr'])} · to merge: "
          f"{duration(summary['median_seconds_to_merge'])} · human wait per run: "
          f"{duration(summary['median_human_wait_seconds'])}")
    if priced:
        cost = f" (${summary['usd']:.2f})" if summary["usd"] is not None else ""
        print(f"tokens: {summary['tokens']}{cost} across {len(priced)} runs with usage recorded")


def cmd_release(run_id):
    """Remove the run's worktree once the branch is pushed or the run is abandoned."""
    path, s = load(run_id)
    wt = s.get("worktree")
    if not wt:
        print("run uses the main checkout; nothing to release")
        return
    if s["stage"] not in TERMINAL:
        die(f"run is at '{s['stage']}'; release only a merged, closed, done or aborted run")
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
    "release": (cmd_release, 1), "pr-status": (cmd_pr_status, 1), "inbox": (cmd_inbox, 1),
    "checkpoint-post": (cmd_checkpoint_post, 1), "pr-update": (cmd_pr_update, 1), "usage": (cmd_usage, 3), "stats": (cmd_stats, 0),
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
    if name == "usage" and len(args) == 4:
        return fn(*args)
    if name == "stats":
        flags = [a for a in args if a == "--json"]
        rest = [a for a in args if a != "--json"]
        if len(rest) > 1:
            die("usage: stats [<run-id>] [--json]")
        return fn(*rest, as_json=bool(flags))
    if len(args) != nargs:
        die(f"'{name}' takes {nargs} argument(s); see --help")
    fn(*args)


if __name__ == "__main__":
    main(sys.argv[1:])

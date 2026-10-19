#!/usr/bin/env python3
"""Run-state helper for the /factory skill.

All run state lives in <repo>/.factory/runs/<run-id>/. This script is the only
thing that writes state.json, so stage names and terminal completion are
checked and every transition is logged the same way. Each write holds the run's
lock (<run>/.lock), so parallel sessions on one run don't lose each other's changes.

Usage:
  state.py new <slug> <input-file|-> [--in-place] [--parent <run-id>] [--skip <stages>]
                                         create a run in its own worktree (or in the
                                         current checkout); prints run id, folder, repo.
                                         --parent links a follow-up to the run it came from;
                                         --skip lists stages this run skips (like factory.skip)
  state.py list                          list runs with stage and summary
  state.py show <run-id>                 print state.json
  state.py get <run-id> <field>          print one field (dotted, e.g. rounds.review)
  state.py next <run-id> [--tokens N [--usd X]]
                                         the loop: read what the last dispatched stage wrote,
                                         take every move the loop calls for, and print one JSON
                                         instruction (dispatch, ship, ask, wait, finish); --tokens
                                         records what the dispatched stage cost
  state.py answer <run-id> [--to <stage>] [--from <source>] <answer|->
                                         apply a human's answer at a checkpoint, a blocked stage or
                                         land, then print the next instruction like `next`
  state.py advance <run-id> <stage> [note]
                                         move to <stage>, log it, bump the round counter for work
                                         stages; prints the round. Refuses a loop past its cap
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
  state.py seen <run-id>                 mark the comments read so far, so inbox starts after them
  state.py checkpoint-post <run-id>      write checkpoint-post.md for the current checkpoint and print
                                         the gh command that posts it on the issue or PR
  state.py pr-update <run-id>            write pr-update.md, the PR comment for a pushed fix round
  state.py usage <run-id> <stage> <tokens> [usd]
                                         record what a stage dispatch cost
  state.py stats [<run-id>] [--json]     time, rounds, outcomes and cost, for one run or all runs
  state.py policy <run-id>               whether the repo's policy auto-approves this spec (JSON)
  state.py approval <run-id>             one line saying who approved the spec: a human or the policy
  state.py learn <run-id>                merge retro.md's learnings into .factory/learnings.md
  state.py signals [<run-id>] [--days N] check merged runs for reverts, red CI on the merge commit,
                                         and the repo's own signal command; JSON
  state.py release <run-id>              remove a finished run's worktree (keeps the branch)
  state.py skips [<run-id>]              the stages skipped (factory.skip, plus the run's --skip), as JSON

`next` and `answer` make every routine move; `advance` is left for escapes such as `blocked`.
`advance` only takes the loop's own moves. `git config factory.skip <stages|all>` (every run) or
`new --skip <stages|all>` (one run) lets runs jump over triage, spec, checkpoint-1, review, verify and
checkpoint-2; implement, ship and land are never skipped.

Entering checkpoint-1, checkpoint-2, blocked, land, merged or closed runs the notify command,
if one is set in $FACTORY_NOTIFY or `git config factory.notify`, with the event JSON on stdin.
"""
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no advisory locks, so concurrent writers aren't serialized there
    fcntl = None

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
CAP_NOTE = {"review": "review did not converge", "verify": "verify did not pass", "land": "PR feedback did not converge"}
CAP_NOTES = tuple(CAP_NOTE.values())
# The road from input to PR. factory.skip and --skip may jump over any of these but implement and ship.
PATH = ["triage", "spec", "checkpoint-1", "implement", "review", "verify", "checkpoint-2", "ship"]
SKIPPABLE = ("triage", "spec", "checkpoint-1", "review", "verify", "checkpoint-2")
# Every other move a run may make, besides blocked and aborted from any stage. Loops go back to
# implement (or to spec from a checkpoint); blocked hands the run to a human, who may send it anywhere.
MOVES = {
    "triage": {"spec", "checkpoint-1"},
    "spec": {"checkpoint-1", "implement"},  # implement only when factory.autoApproveSpec allows it
    "checkpoint-1": {"spec", "implement"},
    "implement": {"review"},
    "review": {"verify", "implement", "checkpoint-2"},
    "verify": {"checkpoint-2", "implement", "ship"},  # ship only when fixing an open PR
    "checkpoint-2": {"ship", "spec", "implement", "verify"},
    "ship": {"land", "done"},
    "land": {"merged", "closed", "implement", "checkpoint-2"},
    "blocked": set(PATH) | {"land", "merged", "closed"},
}
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


@contextmanager
def locked(lock_file):
    """Hold an exclusive lock on lock_file, so read-modify-write cycles from parallel sessions don't interleave."""
    if fcntl is None or not lock_file.parent.is_dir():
        yield
        return
    with lock_file.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run_lock(run_id):
    """The lock for one run's state.json and feedback.md. Taken before load() by every command that writes them."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id):
        die(f"invalid run id '{run_id}'")
    return locked(runs_dir() / run_id / ".lock")


def save(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def check_base_is_current(base):
    """Refuse to start a run from a base branch that is behind its upstream.

    Every stage diffs against the local base branch, so a stale one hides files that already changed
    upstream (a real run missed a test added on origin/main this way, and CI failed on it).
    """
    upstream = git("rev-parse", "--abbrev-ref", f"{base}@{{upstream}}").stdout.strip()
    if not upstream:
        return
    remote, _, branch = upstream.partition("/")
    if git("fetch", "-q", remote, branch).returncode != 0:
        print(f"warning: could not fetch {upstream}; the run starts from local {base}, which may be stale",
              file=sys.stderr)
        return
    counts = git("rev-list", "--left-right", "--count", f"{base}...{upstream}").stdout.split()
    ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
    if behind and not ahead:
        die(f"{base} is {behind} commit(s) behind {upstream}; run `git pull --ff-only` first so the run "
            "starts from the latest code")
    if behind:
        print(f"warning: {base} and {upstream} have diverged ({ahead} ahead, {behind} behind); "
              f"the run starts from local {base}", file=sys.stderr)


def cmd_new(slug, source, in_place=False, parent=None, skip=None):
    root = repo_root()
    if parent:
        load(parent)
    skip = parse_skips(skip, "--skip") if skip is not None else []
    parse_skips(setting("skip", "FACTORY_SKIP"), "factory.skip")
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
    check_base_is_current(base)
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
        "parent_run": parent,
        "skip": skip,
        "history": [{"at": now(), "from": None, "to": "triage",
                     "note": "run created" + (f", skipping {', '.join(skip)}" if skip else "")}],
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


def parse_skips(value, source):
    """Stage names from a skip list, in loop order. Skipping spec skips its approval too."""
    names = [w.lower() for w in words(value)]
    if "all" in names:
        names = list(SKIPPABLE)
    bad = [n for n in names if n not in SKIPPABLE]
    if bad:
        die(f"{source} can't skip {', '.join(bad)}; it takes any of: all, {', '.join(SKIPPABLE)}")
    if "spec" in names:
        names.append("checkpoint-1")
    return [stage for stage in SKIPPABLE if stage in names]


def skip_setting(s=None):
    """The stages a run may skip: the repo's factory.skip, plus the run's own --skip."""
    return set(parse_skips(setting("skip", "FACTORY_SKIP"), "factory.skip")) | set((s or {}).get("skip") or [])


def skip_source(s, stages):
    """Where a skip came from, for the history and the reports."""
    own = set(s.get("skip") or [])
    if set(stages) <= own:
        return "--skip"
    if not set(stages) & own:
        return "factory.skip"
    return "--skip and factory.skip"


def cmd_skips(run_id=None):
    s = load(run_id)[1] if run_id else None
    print(json.dumps([stage for stage in SKIPPABLE if stage in skip_setting(s)]))


def skipped_between(s, stage):
    """The stages a forward jump to `stage` leaves out, if factory.skip allows all of them; else None."""
    here = s["stage"]
    if here not in PATH or stage not in PATH or PATH.index(stage) <= PATH.index(here) + 1:
        return None
    between = PATH[PATH.index(here) + 1:PATH.index(stage)]
    if stage == "ship" and s.get("pr_url"):
        # A PR fix round ships to a PR the human already approved, so ship approval isn't a skip there.
        between = [x for x in between if x != "checkpoint-2"]
        if not between:
            return None
    return between if set(between) <= skip_setting(s) else None


def path_next(s, here):
    """The next stage after `here` on the road to a PR that this run doesn't skip.

    In a PR fix round (the run has a PR) verified work goes straight back to ship, not to ship approval.
    """
    skips = skip_setting(s)
    for stage in PATH[PATH.index(here) + 1:]:
        if stage in skips or (stage == "checkpoint-2" and s.get("pr_url")):
            continue
        return stage
    return "ship"


def run_skips(s):
    """Every stage this run skipped, in order."""
    out = []
    for e in s["history"]:
        out += [x for x in e.get("skipped", []) if x not in out]
    return out


def unshippable(folder, s):
    """Why work can't skip ship approval: the last check it went through didn't pass. Empty if it can ship."""
    if s["stage"] == "verify":
        try:
            verdict = verification_results(folder, s)[0]
        except Invalid as exc:
            return f"the verification report is invalid ({exc})"
        return "" if verdict == "pass" else "verification did not pass"
    if s["stage"] == "review":
        m = re.search(r"^Verdict: (\S+)$", read(folder, "review.md"), re.MULTILINE)
        return "" if m and m.group(1) == "approve" else "review did not approve"
    return ""


class Refused(Exception):
    """A move or answer the loop doesn't allow. The message says what the run can do instead."""


class NeedsStage(Refused):
    """An answer that is valid, but needs the caller to say which stage it sends the run to (--to)."""


def cmd_advance(run_id, stage, note=""):
    path, s = load(run_id)
    try:
        out = advance_run(path, s, stage, note)
    except Refused as exc:
        die(str(exc))
    for line in out["lines"]:
        print(line)
    for warning in out["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    if out["post"]:
        print(f"post this checkpoint on GitHub: {out['post']}, then run the command it prints")


def advance_run(path, s, stage, note="", human=False):
    """Move the run to `stage`, log it, and save. Raises Refused for a move the loop doesn't make.

    `human` marks a move a person asked for (an answer), which may start a PR fix round past the cap.
    Returns the lines to print, warnings, and the checkpoint-post command when the move should be posted.
    """
    if stage not in STAGES:
        raise Refused(f"unknown stage '{stage}'; valid: {', '.join(STAGES)}")
    if s["stage"] in TERMINAL:
        raise Refused(f"run is {s['stage']}; it cannot advance")
    if stage == s["stage"]:
        raise Refused(f"run is already at '{stage}'; dispatch it without advancing")
    jumped = skipped_between(s, stage)
    if jumped is None and stage not in MOVES[s["stage"]] | {"blocked", "aborted"}:
        allowed = ", ".join(sorted(MOVES[s["stage"]] | {"blocked", "aborted"}))
        raise Refused(f"a run at '{s['stage']}' cannot move to '{stage}'; it can go to: {allowed}. "
                      "Skipping stages needs `--skip` when the run starts, or `git config factory.skip`")
    if stage == "done" and not s.get("pr_url"):
        raise Refused("done requires the ship stage and a recorded PR URL")
    # A blocked run with an open PR goes back to following it without a fake ship.
    if stage == "land" and not s.get("pr_url"):
        raise Refused("land requires the ship stage (or blocked, back to an open PR) and a recorded PR URL")
    if stage in ("merged", "closed") and not s.get("pr_url"):
        raise Refused(f"{stage} requires a recorded PR URL")
    # Shipping needs the human's ship answer, except to push fixes to a PR they already approved,
    # or when the repo skips ship approval and the work passed every check it didn't skip.
    if stage == "ship" and s["stage"] == "verify" and not s.get("pr_url") and not jumped:
        raise Refused("ship comes after checkpoint-2, or after verify when pushing fixes to an open PR")
    if jumped and stage == "ship" and not s.get("pr_url"):
        failing = unshippable(path.parent, s)
        if failing:
            raise Refused(f"ship approval is skipped, but this work can't ship without a human: {failing}")
    loop = s.setdefault("loop", {k: 0 for k in sorted(ROUND_CAP)})
    loop.setdefault("land", 0)
    # Loop caps are enforced here, not left to the caller: a capped loop goes to a human.
    if stage == "implement" and not human:
        if s["stage"] in ROUND_CAP and loop.get(s["stage"], 0) >= ROUND_CAP[s["stage"]]:
            raise Refused(f"{s['stage']} has used all {ROUND_CAP[s['stage']]} attempts; go to checkpoint-2 "
                          f"with the note '{CAP_NOTE[s['stage']]}'")
        if s["stage"] == "land" and loop["land"] >= LAND_CAP:
            raise Refused(f"the PR has had all {LAND_CAP} fix rounds; go to checkpoint-2 "
                          f"with the note '{CAP_NOTE['land']}'")
    # A skipped triage or spec is left without writing its output file.
    left = s["stage"] in ("triage", "spec") and s["stage"] in skip_setting(s) \
        and not (path.parent / f"{s['stage']}.md").exists()
    skipped = ([s["stage"]] if left else []) + (jumped or [])
    if skipped:
        note = (note + "; " if note else "") + f"skipped {', '.join(skipped)} ({skip_source(s, skipped)})"
    if s["stage"] == "spec" and stage == "implement" and not jumped:
        # Skipping spec approval is the repo's explicit choice, checked here rather than trusted to the caller.
        decision = spec_policy(path.parent, s)
        if decision["reasons"]:
            raise Refused("the spec needs human approval at checkpoint-1: " + "; ".join(decision["reasons"]))
        note = f"spec auto-approved by policy (size {decision['size']}, type {decision['type']})"
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
    entry = {"at": now(), "from": s["stage"], "to": stage, "note": note}
    if skipped:
        entry["skipped"] = skipped
    s["history"].append(entry)
    s["stage"] = stage
    s.pop("dispatched", None)
    save(path, s)
    lines, warnings, post = [], [], None
    if stage in ROUND_CAP:
        n, cap = loop[stage], ROUND_CAP[stage]
        last = "  LAST ATTEMPT: if this one fails, escalate to checkpoint-2" if n >= cap else ""
        lines.append(f"{stage} round {s['rounds'][stage]} (attempt {n} of {cap}){last}")
    elif pr_fix:
        n = loop["land"]
        last = "  LAST ATTEMPT: if the PR still needs fixes after this, escalate to checkpoint-2" if n >= LAND_CAP else ""
        lines.append(f"implement round {s['rounds']['implement']} (PR fix {n} of {LAND_CAP}){last}")
    elif stage in WORK_STAGES:
        lines.append(f"{stage} round {s['rounds'][stage]}")
    else:
        lines.append(stage)
    if stage in NOTIFY_STAGES:
        warning = notify(path.parent, s)
        if warning:
            warnings.append(warning)
    if stage in HUMAN_STAGES and (s.get("pr_url") or (s.get("thread_url") and posting_enabled())):
        post = f"python3 {Path(__file__).resolve()} checkpoint-post {s['id']}"
    return {"lines": lines, "warnings": warnings, "post": post}


def notify(folder, s):
    """Tell the human a run needs them. Returns a warning when it fails, which never fails the advance."""
    cmd = os.environ.get("FACTORY_NOTIFY") or git("config", "--get", "factory.notify").stdout.strip()
    if not cmd:
        return None
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
        return "notify command timed out after 30s"
    if r.returncode != 0:
        return f"notify command exited {r.returncode}: {r.stderr.strip()[:200]}"
    return None


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


def verification_results(folder, s=None):
    """Validate verification.md against spec.md; return (verdict, suite, {ACn: result}).

    A run that skipped the spec has no criteria, so only the suite is checked.
    """
    no_spec = s is not None and "spec" in run_skips(s) and not (folder / "spec.md").exists()
    try:
        spec = "" if no_spec else (folder / "spec.md").read_text()
        verification = (folder / "verification.md").read_text()
    except OSError as exc:
        raise Invalid(f"verification input missing or unreadable: {exc.filename}")
    expected = re.findall(r"^- \[[ xX]\] (AC\d+):", spec, re.MULTILINE)
    if (not expected and not no_spec) or len(expected) != len(set(expected)):
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
    path, s = load(run_id)
    try:
        verdict, suite, found = verification_results(path.parent, s)
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


def spec_approval(s):
    """Who approved the spec the code was built from: the policy, a human, or nobody yet.

    The latest move into implement from spec or checkpoint-1 decides, so a spec the policy
    auto-approved and a human later revised and approved counts as human-approved.
    """
    approvals = [e for e in s["history"] if e["to"] == "implement" and e["from"] in ("triage", "spec", "checkpoint-1")]
    if not approvals:
        return None
    last = approvals[-1]
    if "spec" in last.get("skipped", []):
        return "skipped; no spec was written and no human approved the work before implementation"
    if "checkpoint-1" in last.get("skipped", []):
        return "not approved: checkpoint 1 was skipped; no human reviewed it before implementation"
    if last["from"] == "spec":
        why = re.sub(r"^spec auto-approved by policy\s*", "", last["note"] or "")
        return f"auto-approved by policy {why}; no human reviewed it before implementation".replace("  ", " ")
    return "approved by a human at checkpoint 1"


def cmd_approval(run_id):
    _, s = load(run_id)
    approval = spec_approval(s)
    print(f"Spec: {approval}" if approval else "Spec: not approved yet")
    if "checkpoint-2" in run_skips(s):
        print(f"Ship: {SHIP_SKIPPED}")


SHIP_SKIPPED = "no human approved shipping; checkpoint 2 was skipped"


def skips_summary(s):
    """The line telling reviewers which stages the run skipped, or nothing."""
    skips = run_skips(s)
    if not skips:
        return []
    source = {"--skip": "this run's `--skip`", "factory.skip": "the repo's `factory.skip`"}.get(
        skip_source(s, skips), "this run's `--skip` and the repo's `factory.skip`")
    line = f"**Skipped by {source}:** {', '.join(skips)}."
    if "checkpoint-2" in skips:
        line += f" {SHIP_SKIPPED[0].upper()}{SHIP_SKIPPED[1:]}."
    return [line, ""]


def results_summary(folder, s):
    """The verification table, review verdict and rounds, as markdown lines."""
    criteria = spec_criteria(read(folder, "spec.md"))
    icons = {"verified": "✅", "failed": "❌", "unverifiable": "⚠️"}
    out = []
    try:
        if "verify" in run_skips(s) and not (folder / "verification.md").exists():
            out += ["**Verification:** skipped", ""]
        else:
            verdict, suite, found = verification_results(folder, s)
            out += [f"**Verification:** {verdict} · test suite {suite}", ""]
            if found:
                out += ["| Criterion | Result |", "|---|---|"]
            for ac, result in found.items():
                text = criteria.get(ac, "").replace("|", "\\|")
                out.append(f"| **{ac}** {text} | {icons[result]} {result} |")
            if found:
                out.append("")
    except Invalid as exc:
        out += [f"**Verification:** report did not validate ({exc})", ""]
    m = re.search(r"^Verdict: (\S+)$", read(folder, "review.md"), re.MULTILINE)
    if m:
        out += [f"**Review:** {m.group(1)}", ""]
    approval = spec_approval(s)
    if approval and approval.startswith("auto"):
        out += [f"**Spec:** {approval}.", ""]
    out += skips_summary(s)
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


def build_pr_report(folder, s, no_media=False):
    """Build a complete report and return its path and selected uploads."""
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
    marker_room = len(f"<!-- factory-pr: {s['id']} -->\n")
    out += details_sections(REPORT_LIMIT - marker_room - len("\n".join(out + tail)), (
        ("Approved spec", "spec.md", spec), ("Verification evidence", "verification.md", verification),
        ("Code review", "review.md", review), ("Human feedback", "feedback.md", feedback)))
    report = folder / "pr-report.md"
    report.write_text("\n".join(out + tail).rstrip() + "\n")
    return report, uploads


def cmd_pr_report(run_id, no_media=False):
    """Publish the current run report to its one PR comment."""
    path, s = load(run_id)
    report, uploads = build_pr_report(path.parent, s, no_media=no_media)
    print(report)
    print(publish_pr_comment(path, s, report, uploads))


GITHUB_URL = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)")
# Only these repository permissions can answer a checkpoint from a comment.
WRITE_PERMISSIONS = {"admin", "maintain", "write"}
# Every comment the factory posts carries this, so it never reads its own posts back as input.
MARKER = "<!-- factory"
ANSWER_KEYWORDS = ("approve", "ship", "abort", "pause", "steer")
COMMAND = re.compile(r"\A\s*/factory(?:\s+|\Z)(.*)\Z", re.DOTALL)


def rest_fallback(url, body_file):
    """The same comment through the REST API, for hosts that block the GraphQL API `gh ... comment` uses.
    It posts text only: attachments need `gh pr comment --attach`."""
    owner, repo, _, number = github_thread(url)
    return (f"fallback: gh api repos/{owner}/{repo}/issues/{number}/comments -F body=@{shlex.quote(str(body_file))} "
            "--jq .html_url")


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


FACTORY_COMMENT = re.compile(r"\A<!-- factory-(?:pr|run|checkpoint|update):[^\n]*-->")
UPLOADED_MEDIA = re.compile(
    r"https://(?:github\.com/user-attachments/assets|(?:private-)?user-images\.githubusercontent\.com)/[^\s)]+"
)


def pr_comments(base, number):
    return gh_items(f"{base}/issues/{number}/comments", ".[]")


def owned_factory_comments(comments, actor):
    factory = [c for c in comments if FACTORY_COMMENT.search(c.get("body") or "")]
    foreign = [c for c in factory if (c.get("user") or {}).get("login", "").lower() != actor.lower()]
    if foreign:
        die("factory comment belongs to another GitHub actor; cannot edit or remove it")
    return sorted(factory, key=lambda c: (c.get("created_at") or "", c["id"]))


def preserve_uploaded_media(body, old_body):
    """Keep GitHub-hosted media links while listing local evidence that an edit cannot upload."""
    # GitHub rewrites local evidence to its attachment hosts. Ordinary HTTP links in the
    # old spec, review, or feedback must not be copied into the new report.
    links = []
    for line in old_body.splitlines():
        line = line.strip()
        markdown = re.fullmatch(r"!?\[[^\]]*\]\((https?://[^)]+)\)", line)
        if (markdown and UPLOADED_MEDIA.fullmatch(markdown.group(1))) or UPLOADED_MEDIA.fullmatch(line):
            links.append(line)
    unique = list(dict.fromkeys(links))
    heading = "\n\n### Previously uploaded evidence\n\n"
    room = REPORT_LIMIT - len(body) - len(heading) - 1
    if room <= 0 or not unique:
        return body
    retained = []
    for link in unique:
        cost = len(link) + (2 if retained else 0)
        if cost > room:
            break
        retained.append(link)
        room -= cost
    return body.rstrip() + heading + "\n\n".join(retained) + "\n" if retained else body


def publish_pr_comment(path, s, body_file, uploads=()):
    """Create once or edit the oldest owned Factory issue comment on a PR."""
    owner, repo, kind, number = github_thread(s.get("pr_url"))
    if kind != "pull":
        die("PR comment publisher requires a pull request URL")
    base = f"repos/{owner}/{repo}"
    actor = gh("api", "user", "--jq", ".login").strip()
    if not actor:
        die("could not identify the authenticated GitHub actor")
    comments = owned_factory_comments(pr_comments(base, number), actor)
    # A second lookup immediately before creation narrows the race between independent runs.
    if not comments:
        comments = owned_factory_comments(pr_comments(base, number), actor)
    body = body_file.read_text()
    marker = f"<!-- factory-pr: {s['id']} -->\n"
    if not body.startswith(marker):
        body = marker + body
    body = body[:REPORT_LIMIT]
    created = False
    if comments:
        canonical = comments[0]
        if body_file.name == "pr-report.md":
            build_pr_report(body_file.parent, s, no_media=True)
            body = (marker + body_file.read_text())[:REPORT_LIMIT]
        body_file.write_text(preserve_uploaded_media(body, canonical.get("body") or ""))
        raw = gh("api", "--method", "PATCH", f"{base}/issues/comments/{canonical['id']}",
                 "-F", f"body=@{body_file}")
        updated = json.loads(raw)
        canonical = updated if updated.get("id") else canonical
    else:
        body_file.write_text(body)
        if uploads:
            command = ["gh", "pr", "comment", s["pr_url"], "--body-file", body_file.name]
            for upload in uploads:
                command += ["--attach", upload]
            result = subprocess.run(command, cwd=body_file.parent, capture_output=True, text=True)
            # The CLI can fail after GitHub accepted a comment. Always check before falling back.
            comments = owned_factory_comments(pr_comments(base, number), actor)
            if comments:
                canonical = comments[0]
                created = True
            elif result.returncode == 0:
                die("attachment command succeeded but its PR comment could not be found")
            else:
                # Rebuild without local media references before the REST text-only fallback.
                build_pr_report(body_file.parent, s, no_media=True)
                body_file.write_text((marker + body_file.read_text())[:REPORT_LIMIT])
        if not created:
            result = subprocess.run(["gh", "api", "--method", "POST", f"{base}/issues/{number}/comments",
                                     "-F", f"body=@{body_file}"], capture_output=True, text=True)
            if result.returncode == 0:
                canonical = json.loads(result.stdout)
            else:
                found = owned_factory_comments(pr_comments(base, number), actor)
                if not found:
                    die(f"GitHub PR comment creation failed: {result.stderr.strip()[:300]}")
                canonical = found[0]
            if not canonical.get("id"):
                die("GitHub created no identifiable PR comment")
    url = canonical.get("html_url")
    if not url:
        die("GitHub did not return the Factory comment URL")
    # Reconcile comments from previous runs and concurrent publishers only after a successful write.
    for extra in owned_factory_comments(pr_comments(base, number), actor):
        if extra["id"] != canonical["id"]:
            gh("api", "--method", "DELETE", f"{base}/issues/comments/{extra['id']}")
    s["report_url"] = url
    save(path, s)
    return url


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
    """When the run last took input: entering a human stage, leaving land to fix the PR, or a land
    check that marked what it read with `seen`."""
    h = s["history"]
    if s["stage"] == "land":
        left = [e["at"] for e in h if e["from"] == "land"]
        since = left[-1] if left else [e["at"] for e in h if e["to"] == "ship"][-1]
    else:
        since = h[-1]["at"]
    return max(since, s.get("inbox_seen") or since)


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
    if not (s.get("thread_url") or s.get("pr_url")):
        die("no thread_url or pr_url recorded for this run")
    print(json.dumps(inbox(s), indent=2))


def inbox(s):
    """The comments `cmd_inbox` prints, as a dict. Dies (SystemExit) when gh fails."""
    urls = list(dict.fromkeys(u for u in (s.get("thread_url"), s.get("pr_url")) if u))
    since, trust = inbox_since(s), Trust()
    answers, feedback, ignored = [], [], []
    for url in urls:
        for owner, repo, item in thread_items(url, since):
            m = COMMAND.match(item["body"])
            trusted = not item["bot"] and trust(owner, repo, item["author"], item["association"])
            if m:
                entry = {"answer": command_answer(m.group(1)), "author": item["author"], "at": item["at"], "url": item["url"]}
                if not m.group(1).strip():
                    ignored.append({**entry, "reason": "empty /factory command"})
                elif trusted:
                    answers.append(entry)
                else:
                    reason = "not in factory.approvers" if trust.allow is not None else "no write access"
                    ignored.append({**entry, "reason": "bot" if item["bot"] else reason})
            elif url == s.get("pr_url"):
                feedback.append({**item, "trusted": trusted})
    key = lambda e: e["at"]
    return {"since": since, "threads": urls, "answers": sorted(answers, key=key),
            "feedback": sorted(feedback, key=key), "ignored": sorted(ignored, key=key)}


def command_answer(text):
    """A /factory command's answer. A keyword alone on the first line is the whole answer, so a
    signature or footer under `/factory approve` doesn't turn the approval into feedback."""
    text = text.strip()
    first = text.split("\n", 1)[0].strip()
    return first.lower() if first.lower() in ANSWER_KEYWORDS else text


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
        files = [("Verification evidence", "verification.md", read(folder, "verification.md")),
                 ("Code review", "review.md", read(folder, "review.md"))]
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
        files = [("Approved spec", "spec.md", read(folder, "spec.md")),
                 ("Verification evidence", "verification.md", read(folder, "verification.md")),
                 ("Code review", "review.md", read(folder, "review.md"))]
        replies = ["`/factory ship` to open the PR" if not s.get("pr_url") else "`/factory ship` to push this round to the PR",
                   "`/factory <feedback>` to send the work back"]
    else:
        out += [f"### Blocked at {last['from']}", "", note or "A stage needs a human decision.", ""]
        if kind == "pull":
            out += results_summary(folder, s)
        files = [("Approved spec", "spec.md", read(folder, "spec.md")),
                 ("Verification evidence", "verification.md", read(folder, "verification.md")),
                 ("Code review", "review.md", read(folder, "review.md"))]
        replies = ["`/factory <answer>` to answer and continue"]
    replies.append("`/factory abort` to stop the run")
    footer = ["", "Reply with one of:", ""] + [f"- {r}" for r in replies] + [
        "", "_Only replies from people with write access to this repository (or listed in `factory.approvers`) are read._"]
    media, _ = media_section(folder, spec_criteria(read(folder, "spec.md")), attach=False) if kind == "pull" else ([], [])
    marker_room = len(f"<!-- factory-pr: {s['id']} -->\n") if kind == "pull" else 0
    out += details_sections(REPORT_LIMIT - marker_room - len("\n".join(out + media + footer)), files)
    body = "\n".join(out + media + footer)
    if len(body) > REPORT_LIMIT:
        cut = "\n\n_Truncated to fit GitHub's comment limit; see the run folder._\n"
        head = "\n".join(out)[:REPORT_LIMIT - len(cut) - len("\n".join(footer))]
        body = head + cut + "\n".join(footer)
    post = folder / "checkpoint-post.md"
    post.write_text(body.rstrip() + "\n")
    print(post)
    if kind == "pull":
        print(publish_pr_comment(path, s, post))
        return
    noun = "pr" if kind == "pull" else "issue"
    print(f"gh {noun} comment {shlex.quote(target)} --body-file {shlex.quote(str(post))}")
    print(rest_fallback(target, post))


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
    out = [f"{MARKER}-update: {s['id']} {n} -->", f"## Factory run `{s['id']}`: PR fix round {n}", ""]
    if s.get("input_summary"):
        out += [s["input_summary"], ""]
    out += ["### Addressed", "", addressed, ""] + results_summary(folder, s)
    details = (("Approved spec", "spec.md", read(folder, "spec.md")),
               ("Verification evidence", "verification.md", read(folder, "verification.md")),
               ("Code review", "review.md", read(folder, "review.md")),
               ("Human feedback", "feedback.md", read(folder, "feedback.md")))
    marker_room = len(f"<!-- factory-pr: {s['id']} -->\n")
    out += details_sections(REPORT_LIMIT - marker_room - len("\n".join(out)), details)
    media, _ = media_section(folder, spec_criteria(read(folder, "spec.md")), attach=False)
    out += media
    body = "\n".join(out)
    if len(body) > REPORT_LIMIT:
        body = body[:REPORT_LIMIT - 80] + "\n\n_Truncated; see `implementation.md` in the run folder._"
    post = folder / "pr-update.md"
    post.write_text(body.rstrip() + "\n")
    print(post)
    print(publish_pr_comment(path, s, post))


def cmd_seen(run_id):
    """Mark every comment so far as read, so the next inbox starts after them."""
    path, s = load(run_id)
    s["inbox_seen"] = now()
    save(path, s)
    print(f"inbox_seen = {s['inbox_seen']}")


def cmd_usage(run_id, stage, tokens, usd=None):
    path, s = load(run_id)
    if stage not in STAGES and stage != "retro":
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


AUTO_TYPES = "bug,chore"
LEARNING_CATEGORIES = ("Commands", "Conventions", "Pitfalls", "Review", "Verification", "Process")
LEARNINGS_CAP = 80
LEARNINGS_HEADER = ("# Factory learnings\n\n"
                    "What earlier runs learned about this repository. Every stage reads this file. It is written by\n"
                    "`state.py learn` from each run's retro; edit or prune it freely.\n")


def setting(key, env):
    return (os.environ.get(env) or git("config", "--get", f"factory.{key}").stdout).strip()


def words(value):
    return [w for w in re.split(r"[,\s]+", value) if w]


def section(text, heading):
    m = re.search(rf"^## {re.escape(heading)}[^\n]*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def spec_policy(folder, s):
    """Whether the repo lets this spec skip checkpoint 1: returns type, size and the reasons it can't."""
    triage, spec = read(folder, "triage.md"), read(folder, "spec.md")
    field = lambda name: (re.search(rf"^{name}: *(\S+)", triage, re.MULTILINE) or [None, ""])[1]
    kind, size = field("Type").lower(), field("Size").upper()
    sizes = [w.upper() for w in words(setting("autoApproveSpec", "FACTORY_AUTO_APPROVE_SPEC"))]
    kinds = [w.lower() for w in words(setting("autoApproveTypes", "FACTORY_AUTO_APPROVE_TYPES") or AUTO_TYPES)]
    reasons = []
    if not sizes:
        reasons.append("auto-approval is off (git config factory.autoApproveSpec)")
    else:
        if field("Verdict") != "proceed":
            reasons.append(f"triage verdict is {field('Verdict') or 'missing'}, not proceed")
        if size not in sizes:
            reasons.append(f"size {size or '?'} is not in {','.join(sizes)}")
        if kind not in kinds:
            reasons.append(f"type {kind or '?'} is not in {','.join(kinds)}")
        if s["rounds"].get("spec") != 1:
            reasons.append("the spec has been revised")
        if not spec_criteria(spec):
            reasons.append("the spec has no acceptance criteria")
        if section(spec, "Open questions").rstrip(".").lower() not in ("", "none"):
            reasons.append("the spec has open questions")
    return {"auto_approve_spec": not reasons, "type": kind or None, "size": size or None, "reasons": reasons}


def cmd_policy(run_id):
    path, s = load(run_id)
    print(json.dumps(spec_policy(path.parent, s), indent=2))


def learnings_path():
    return main_root() / ".factory" / "learnings.md"


def normalize(lesson):
    lesson = re.sub(r"(\s*_\([^)]*\)_)+\s*$", "", lesson)  # drop the provenance suffix
    return re.sub(r"\s+", " ", lesson).strip().rstrip(".").lower()


def read_learnings(path):
    """{category: [entry lines]} from learnings.md, in file order."""
    cats, current = {}, None
    for line in (path.read_text().splitlines() if path.exists() else []):
        if line.startswith("## "):
            current = cats.setdefault(line[3:].strip(), [])
        elif line.startswith("- ") and current is not None:
            current.append(line[2:].strip())
    return cats


def cmd_learn(run_id):
    """Fold a finished run's retro into the shared learnings file, so later runs start from it."""
    path, s = load(run_id)
    try:
        message, warning = learn_run(path, s)
    except Refused as exc:
        die(str(exc))
    print(message)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)


def learn_run(path, s):
    """Merge the run's retro into learnings.md. Returns (message, warning or None); raises Refused."""
    with locked(learnings_path().with_suffix(".lock")):
        return learn(path, s)


def learn(path, s):
    if s["stage"] not in TERMINAL:
        raise Refused(f"run is at '{s['stage']}'; learn from a merged, closed, done or aborted run")
    if s.get("learned_at"):
        raise Refused(f"already learned from this run at {s['learned_at']}")
    retro = read(path.parent, "retro.md")
    if not retro:
        raise Refused("no retro.md in the run folder; run the retro stage first")
    items = re.findall(r"^- \[([A-Za-z]+)\] +(.+?)\s*$", section(retro, "Learnings"), re.MULTILINE)
    target = learnings_path()
    cats = read_learnings(target)
    known = {normalize(e) for entries in cats.values() for e in entries}
    added = retired = duplicates = 0
    for category, lesson in items:
        if category.lower() == "retire":
            for name in cats:
                kept = [e for e in cats[name] if normalize(e) != normalize(lesson)]
                retired += len(cats[name]) - len(kept)
                cats[name] = kept
            continue
        name = next((c for c in LEARNING_CATEGORIES if c.lower() == category.lower()), "Other")
        if normalize(lesson) in known:
            duplicates += 1
            continue
        known.add(normalize(lesson))
        lesson = re.sub(r"(\s*_\([^)]*\)_)+\s*$", "", lesson)  # a retro may copy the suffix; add it once
        cats.setdefault(name, []).append(f"{lesson} _({s['id']})_")
        added += 1
    order = [c for c in LEARNING_CATEGORIES if cats.get(c)] + [c for c in cats if c not in LEARNING_CATEGORIES and cats[c]]
    body = LEARNINGS_HEADER + "".join(f"\n## {c}\n" + "".join(f"- {e}\n" for e in cats[c]) for c in order)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    s["learned_at"] = now()
    save(path, s)
    total = sum(len(v) for v in cats.values())
    warning = (f"{total} learnings is more than {LEARNINGS_CAP}; the next retro should retire stale ones"
               if total > LEARNINGS_CAP else None)
    return f"added {added}, retired {retired}, skipped {duplicates} duplicates; {total} learnings in {target}", warning


def merged_at(s):
    return next((e["at"] for e in reversed(s["history"]) if e["to"] == "merged"), None)


def custom_signals(s):
    """Lines printed by the repo's own signal command (error tracker, logs, ...), given the run as JSON."""
    cmd = setting("signals", "FACTORY_SIGNALS")
    if not cmd:
        return [], None
    event = {k: s.get(k) for k in ("id", "pr_url", "merge_sha", "base_branch", "branch", "input_summary")}
    event["merged_at"] = merged_at(s)
    try:
        r = subprocess.run(cmd, shell=True, input=json.dumps(event), cwd=main_root(), capture_output=True,
                           text=True, timeout=60, env={**os.environ, "FACTORY_RUN_ID": s["id"],
                                                         "FACTORY_MERGE_SHA": s.get("merge_sha") or ""})
    except subprocess.TimeoutExpired:
        return [], "signal command timed out after 60s"
    if r.returncode != 0:
        return [], f"signal command exited {r.returncode}: {r.stderr.strip()[:200]}"
    return [line.strip() for line in r.stdout.splitlines() if line.strip()], None


def run_signals(path, s):
    """Did this merged change hurt? Reverts on the base branch, red checks on the merge commit, custom signals."""
    owner, repo, _, number = github_thread(s["pr_url"])
    notes = []
    has_gh = shutil.which("gh") is not None
    if not s.get("merge_sha") and has_gh:
        pr = gh("api", f"repos/{owner}/{repo}/pulls/{number}", optional=True)
        if pr:
            s["merge_sha"] = json.loads(pr).get("merge_commit_sha")
    sha, since = s.get("merge_sha"), merged_at(s)
    reverted_by = []
    if git("fetch", "-q", "origin", s["base_branch"]).returncode != 0:
        notes.append(f"could not fetch origin/{s['base_branch']}; checked the local copy")
    log = git("log", f"origin/{s['base_branch']}", f"--since={since}", "--format=%H%x1f%B%x1e")
    if log.returncode == 0:
        markers = [f"Reverts {owner}/{repo}#{number}"] + ([f"This reverts commit {sha}"] if sha else [])
        for entry in log.stdout.split("\x1e"):
            commit, _, message = entry.strip().partition("\x1f")
            if commit and any(m in message for m in markers):
                reverted_by.append(commit)
    else:
        notes.append(f"no origin/{s['base_branch']} to check for reverts")
    ci, failing = "unknown", []
    if sha and has_gh:
        runs = gh_items(f"repos/{owner}/{repo}/commits/{sha}/check-runs", ".check_runs[]", optional=True)
        if runs is None:
            notes.append("could not read checks on the merge commit")
        else:
            ci, failing, _ = checks_summary(runs, [])
    elif not sha:
        notes.append("merge commit unknown")
    custom, warning = custom_signals(s)
    if warning:
        notes.append(warning)
    result = {"id": s["id"], "pr_url": s["pr_url"], "merge_sha": sha, "merged_at": since, "checked_at": now(),
              "reverted_by": reverted_by, "base_ci": ci, "failing": failing, "custom": custom,
              "regression": bool(reverted_by or ci == "fail" or custom), "notes": notes}
    s["signals"] = result
    # The checks above can take a while; merge into the current state rather than overwrite it.
    with run_lock(s["id"]):
        current = json.loads(path.read_text())
        current["signals"] = result
        if s.get("merge_sha"):
            current["merge_sha"] = s["merge_sha"]
        save(path, current)
    return result


def cmd_signals(run_id=None, days="14"):
    try:
        days = int(days)
    except ValueError:
        die("--days takes a whole number")
    if run_id:
        path, s = load(run_id)
        if s["stage"] != "merged":
            die(f"run is at '{s['stage']}'; signals are for merged runs")
        targets = [(path, s)]
    else:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        d = runs_dir()
        targets = []
        for p in sorted(d.glob("*/state.json")) if d.exists() else []:
            s = json.loads(p.read_text())
            if s["stage"] == "merged" and merged_at(s) and parse_at(merged_at(s)) >= cutoff:
                targets.append((p, s))
    print(json.dumps([run_signals(p, s) for p, s in targets], indent=2))


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
        "auto_approved_spec": any(e["from"] == "spec" and e["to"] == "implement" and not e.get("skipped") for e in h),
        "auto_spec_sent_back": any(e["from"] == "spec" and e["to"] == "implement" and not e.get("skipped") for e in h)
        and any(e["from"] == "checkpoint-2" and e["to"] == "spec" for e in h),
        "skipped": run_skips(s),
        "regression": (s.get("signals") or {}).get("regression"),
        "parent_run": s.get("parent_run"),
        "learned": bool(s.get("learned_at")),
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
        "auto_approved_specs": sum(r["auto_approved_spec"] for r in runs),
        "auto_specs_sent_back_to_spec": sum(r["auto_spec_sent_back"] for r in runs),
        "runs_with_skips": sum(bool(r["skipped"]) for r in runs),
        "shipped_without_ship_approval": sum("checkpoint-2" in r["skipped"] for r in runs),
        "merged_runs_checked_for_signals": sum(r["regression"] is not None for r in runs),
        "regressions": sum(bool(r["regression"]) for r in runs),
        "follow_up_runs": sum(bool(r["parent_run"]) for r in runs),
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
    if summary["auto_approved_specs"]:
        print(f"specs auto-approved by policy: {summary['auto_approved_specs']}, "
              f"{summary['auto_specs_sent_back_to_spec']} later sent back to spec at ship approval")
    if summary["runs_with_skips"]:
        print(f"runs that skipped stages: {summary['runs_with_skips']}, "
              f"{summary['shipped_without_ship_approval']} shipped without ship approval")
    if summary["merged_runs_checked_for_signals"]:
        print(f"regressions after merge: {summary['regressions']} of {summary['merged_runs_checked_for_signals']} "
              f"merged runs checked · follow-up runs: {summary['follow_up_runs']}")
    if priced:
        cost = f" (${summary['usd']:.2f})" if summary["usd"] is not None else ""
        print(f"tokens: {summary['tokens']}{cost} across {len(priced)} runs with usage recorded")


def cmd_release(run_id):
    """Remove the run's worktree once the branch is pushed or the run is abandoned."""
    _, s = load(run_id)
    try:
        print(release_run(s))
    except Refused as exc:
        die(str(exc))


def release_run(s):
    wt = s.get("worktree")
    if not wt:
        return "run uses the main checkout; nothing to release"
    if s["stage"] not in TERMINAL:
        raise Refused(f"run is at '{s['stage']}'; release only a merged, closed, done or aborted run")
    if not Path(wt).exists():
        git("worktree", "prune")
        return f"worktree {wt} already removed"
    r = subprocess.run(["git", "worktree", "remove", wt], cwd=main_root(), capture_output=True, text=True)
    if r.returncode != 0:
        raise Refused(f"could not remove worktree {wt}: {r.stderr.strip()}; commit, stash or delete its changes first")
    return f"removed worktree {wt}; branch {s['branch']} is kept"


# The loop itself. `next` reads what the last stage wrote, takes every move the loop dictates, and prints
# one instruction; `answer` applies a human's answer the same way. The orchestrator does what it's told:
# dispatch a stage, ship, ask a human, or stop. Judgment (what feedback means, presenting a checkpoint,
# steering) stays with the orchestrator; routing doesn't.

OUTPUTS = {"triage": "triage.md", "spec": "spec.md", "implement": "implementation.md", "review": "review.md",
           "verify": "verification.md", "land": "land.md", "retro": "retro.md"}
# Dispatches of one stage round that may come back without a usable output before the run goes to a human.
DISPATCH_LIMIT = 3
VERDICTS = {"triage": ("proceed", "needs-human", "reject"), "review": ("approve", "changes-requested"),
            "land": ("merged", "closed", "fix", "wait", "needs-human", "abort")}


def skill_dir():
    return Path(__file__).resolve().parent.parent


def fingerprint(p):
    """What a file looked like when a stage was dispatched, so a rewrite (even with the same text) counts."""
    if not p.exists():
        return None, None
    return p.stat().st_mtime_ns, hashlib.sha1(p.read_bytes()).hexdigest()


def field(text, name):
    m = re.search(rf"^{name}: *(\S+)", text, re.MULTILINE)
    return m.group(1) if m else None


def first_lines(text, limit=500):
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def checkpoint_kind(s):
    """What a human stop is asking for, from the note it was entered with."""
    note = s["history"][-1]["note"] or ""
    if s["stage"] == "checkpoint-1":
        if note.startswith("triage questions"):
            return "triage-questions"
        return "triage-rejected" if note.startswith("triage rejected") else "spec-approval"
    if s["stage"] == "blocked":
        return "steer" if note.startswith("steer requested") else "blocked"
    return "ship-approval"


class Driver:
    def __init__(self, path, s):
        self.path, self.s, self.folder = path, s, path.parent
        self.did, self.tell, self.warnings = [], [], []
        self.post = self.entered = self.github_note = None
        self.state_cmd = f"python3 {Path(__file__).resolve()}"

    # -- bookkeeping ---------------------------------------------------------------------------------------

    def save(self):
        save(self.path, self.s)

    def git(self, *args):
        """git in the run's repo: its worktree, or the human's checkout for an in-place run."""
        return subprocess.run(["git", "-C", self.s["repo"], *args], capture_output=True, text=True)

    def advance(self, stage, note="", human=False):
        before = self.s["stage"]
        out = advance_run(self.path, self.s, stage, note, human=human)
        entry = self.s["history"][-1]
        self.did.append(f"{before} → {stage}" + (f": {first_lines(entry['note'], 160)}" if entry["note"] else ""))
        self.warnings += out["warnings"]
        self.post = (out["post"] + ", then run the command it prints") if out["post"] else None
        self.entered = stage

    def feedback(self, heading, text):
        with (self.folder / "feedback.md").open("a") as f:
            f.write(f"## {heading}\n{text.strip()}\n\n")

    def result(self, action, instruction, **fields):
        out = {"run": self.s["id"], "stage": self.s["stage"], "action": action, "instruction": instruction}
        out.update({k: v for k, v in fields.items() if v not in (None, "", [])})
        if self.post and self.s["stage"] in HUMAN_STAGES:
            out["post_on_github"] = self.post
        for key in ("did", "tell", "warnings"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out

    def record_usage(self, tokens, usd):
        d = self.s.get("dispatched")
        if not d:
            self.warnings.append("no stage was dispatched, so --tokens was not recorded")
            return
        self.s.setdefault("usage", []).append({"at": now(), "stage": d["stage"], "round": d["round"],
                                               "tokens": tokens, "usd": usd})
        self.save()

    # -- the loop ------------------------------------------------------------------------------------------

    def run(self):
        for _ in range(20):
            out = self.step()
            if out:
                return out
        raise Refused("the run kept moving without stopping; check its state.json history")

    def step(self):
        st = self.s["stage"]
        if st in TERMINAL:
            return self.finish()
        if st in HUMAN_STAGES:
            return self.human()
        if st == "ship":
            return self.ship()
        if st in ("triage", "spec") and st in skip_setting(self.s):
            return self.skip(st)
        d = self.s.get("dispatched")
        if d and d["stage"] == st:
            return self.consume(st) if self.fresh(d) else self.dispatch(st, again=True)
        return self.dispatch(st)

    def round_for(self, stage):
        if stage == "land":
            return self.s["rounds"].get("land", 0) + 1
        return max(1, self.s["rounds"].get(stage, 0))

    def fresh(self, d):
        """Whether the dispatched stage wrote its output: changed since the dispatch, for this round."""
        p = self.folder / OUTPUTS[d["stage"]]
        if not p.exists() or list(fingerprint(p)) == [d.get("mtime"), d.get("sha")]:
            return False
        r = field(p.read_text(), "Round")
        return r is None or r == str(d["round"])

    def dispatch(self, stage, again=False, fix=None):
        old = self.s.get("dispatched") if (again or fix) else None
        n = (old or {}).get("dispatches", 0) + 1
        out = OUTPUTS[stage]
        if n > DISPATCH_LIMIT:
            return self.give_up(stage, f"{stage} wrote no usable {out} in {DISPATCH_LIMIT} dispatches")
        if stage == "implement" and not self.s.get("worktree"):
            problem = self.check_out_run_branch()
            if problem:
                self.advance("blocked", problem)
                return None
        rnd = self.round_for(stage)
        mtime, sha = fingerprint(self.folder / out)
        self.s["dispatched"] = {"stage": stage, "round": rnd, "at": now(), "dispatches": n,
                                "fixes": (old or {}).get("fixes", 0), "mtime": mtime, "sha": sha}
        self.save()
        if again and not fix:
            self.tell.append(f"{stage} round {rnd} didn't finish (no new {out}), so it is dispatched again")
        lines = [
            f"Instructions: read {skill_dir() / 'stages' / (stage + '.md')} and follow it.",
            f"Run folder: {self.folder}",
            f"Repository: {self.s['repo']}. This is the target repo. cd into it for every git, test, and run command.",
            f"Round: {rnd}",
            f"STATE: {self.state_cmd}",
            f"Learnings: {learnings_path()}. Read it first if it exists: it's what earlier runs learned about this "
            "repo. It's background; the spec, feedback, and your instructions win.",
            "Read your inputs from the run folder, write your output file there, and return a summary of at most "
            "10 lines.",
        ]
        if fix:
            lines.append(f"Fix from the orchestrator: your last {out} can't be used: {fix}")
        return self.result(
            "dispatch", f"Dispatch the {stage} stage as a subagent with `prompt`. If a named agent `factory-{stage}` "
            "exists in this session, use it and drop the prompt's first line; otherwise use a general-purpose "
            "subagent, and wait for it to finish (don't run it in the background). When it returns, run next again "
            "(add --tokens <n> if the harness reported its usage).",
            agent=f"factory-{stage}", round=rnd, prompt="\n".join(lines))

    def check_out_run_branch(self):
        """An in-place run works in the human's checkout, so implement needs the run branch there."""
        branch, base = self.s["branch"], self.s["base_branch"]
        if self.git("branch", "--show-current").stdout.strip() == branch:
            return None
        exists = self.git("rev-parse", "-q", "--verify", f"refs/heads/{branch}").returncode == 0
        r = self.git("switch", branch) if exists else self.git("switch", "-c", branch, base)
        if r.returncode != 0:
            return f"couldn't switch to {branch}: {r.stderr.strip()[:200]}"
        self.did.append(f"switched the checkout to {branch}")
        return None

    def fix(self, stage, problem):
        """Send a stage back once to correct an output that can't be read; after that, a human looks."""
        d = self.s["dispatched"]
        if d.get("fixes", 0) >= 1:
            what = "verification report invalid" if stage == "verify" else f"{OUTPUTS[stage]} invalid"
            return self.give_up(stage, f"{what}: {problem}")
        d["fixes"] = d.get("fixes", 0) + 1
        return self.dispatch(stage, fix=problem)

    def give_up(self, stage, reason):
        if stage == "retro":
            self.warnings.append(f"no retro for this run: {reason}")
            self.s["retro_skipped"] = reason
            self.s.pop("dispatched", None)
            self.save()
            return None
        self.advance("blocked", reason)
        return None

    def skip(self, stage):
        """A run at a skipped triage or spec goes on without it."""
        if stage == "triage":
            self.summarize()
            m = GITHUB_URL.search(read(self.folder, "input.md"))
            if m and not self.s.get("thread_url"):
                self.s["thread_url"] = m.group(0)
                self.save()
        self.advance(path_next(self.s, stage))
        return None

    def summarize(self):
        """A one-line summary from the input as the human wrote it; the orchestrator may set a better one."""
        if self.s.get("input_summary"):
            return
        source = read(self.folder, "input.md")
        line = next((x.strip("> ").strip() for x in source.splitlines() if x.strip("> ").strip()), "")
        self.s["input_summary"] = first_lines(line, 120)
        self.save()

    # -- reading what a stage wrote ------------------------------------------------------------------------

    def consume(self, stage):
        text = read(self.folder, OUTPUTS[stage])
        if stage in VERDICTS:
            verdict = field(text, "Verdict")
            if verdict not in VERDICTS[stage]:
                return self.fix(stage, f"it needs exactly one `Verdict: {' | '.join(VERDICTS[stage])}` line")
        return getattr(self, "after_" + stage)(text, field(text, "Verdict"))

    def after_triage(self, text, verdict):
        self.summarize()
        source = GITHUB_URL.search(field(text, "Source") or "")
        if source and not self.s.get("thread_url"):
            self.s["thread_url"] = source.group(0)
            self.save()
        if verdict == "proceed":
            self.advance(path_next(self.s, "triage"))
        else:
            self.advance("checkpoint-1", "triage questions" if verdict == "needs-human" else "triage rejected")

    def after_spec(self, text, _):
        if not spec_criteria(text):
            return self.fix("spec", "it has no acceptance criteria (`- [ ] AC1: ... **Verify by:** ...`)")
        spec = self.folder / "spec.md"
        if "checkpoint-1" in skip_setting(self.s):
            self.advance("implement")
            self.tell.append(f"No human reviewed the spec: checkpoint-1 is skipped ({skip_source(self.s, ['checkpoint-1'])}). "
                             f"Spec: {spec}")
            return None
        policy = spec_policy(self.folder, self.s)
        if policy["auto_approve_spec"]:
            self.advance("implement")
            self.tell.append(f"The spec was auto-approved by the repo's policy (size {policy['size']}, type "
                             f"{policy['type']}); no human reviewed it. Spec: {spec}")
        else:
            self.advance("checkpoint-1")

    def after_implement(self, text, _):
        status = field(text, "Status")
        if status not in ("done", "blocked"):
            return self.fix("implement", "it needs a `Status: done | blocked` line")
        if status == "blocked":
            self.advance("blocked", first_lines(section(text, "Blocked on")) or "implement is blocked")
            return None
        leaked = self.git("diff", "--name-only", f"{self.s['base_branch']}...{self.s['branch']}",
                          "--", ".factory").stdout.split()
        if leaked:
            return self.fix("implement", f"run files leaked into the branch ({', '.join(leaked[:5])}); remove them "
                            "with `git rm -r --cached .factory`, commit, and never commit .factory/")
        self.advance(path_next(self.s, "implement"))

    def after_review(self, _, verdict):
        if verdict == "approve":
            self.advance(path_next(self.s, "review"))
        elif self.s["loop"]["review"] < ROUND_CAP["review"]:
            self.advance("implement", "review changes")
        else:
            self.advance("checkpoint-2", CAP_NOTE["review"])

    def after_verify(self, *_):
        try:
            verdict, suite, found = verification_results(self.folder, self.s)
        except Invalid as exc:
            return self.fix("verify", f"correct verification.md: {exc}")
        if verdict == "pass":
            target = path_next(self.s, "verify")
            note = "" if target != "ship" else "PR fixes" if self.s.get("pr_url") else "ship approval skipped"
            self.advance(target, note)
            return None
        failed = suite == "fail" or "failed" in found.values()
        unverifiable = [k for k, r in found.items() if r == "unverifiable"] + (["suite"] if suite == "unverifiable" else [])
        if not failed and unverifiable:
            self.advance("checkpoint-2", f"unverifiable: {', '.join(unverifiable)}")
        elif self.s["loop"]["verify"] < ROUND_CAP["verify"]:
            self.advance("implement", "verify failed")
        else:
            self.advance("checkpoint-2", CAP_NOTE["verify"])

    def after_land(self, text, verdict):
        if verdict in ("merged", "closed"):
            self.advance(verdict)
        elif verdict == "abort":
            self.advance("aborted", "aborted on the PR")
            self.tell.append("The PR stays open; close it yourself if you don't want it.")
        elif verdict == "fix":
            if self.s["loop"]["land"] < LAND_CAP:
                self.advance("implement", "PR feedback")
            else:
                self.advance("checkpoint-2", CAP_NOTE["land"])
        elif verdict == "needs-human":
            self.advance("blocked", first_lines(section(text, "Needs a human")) or "land needs a human decision")
        else:  # wait: comments so far are read, and the next `next` checks the PR again
            self.s["inbox_seen"] = now()
            self.s.pop("dispatched", None)
            self.save()
            return self.result(
                "wait", "Nothing to do until reviewers or CI act. Stop with the run at land and tell the human in one "
                "line what it's waiting on, and that `<factory> <run-id>` checks the PR again. If the harness can "
                "deliver PR events to this session, subscribe to the PR and run next on each event. Never poll or sleep.",
                pr_url=self.s.get("pr_url"), waiting_on=first_lines(section(text, "Waiting on")))

    # -- ship ----------------------------------------------------------------------------------------------

    def pushed(self):
        """True when the run branch's head is on its remote, False when it isn't, None when that's unknown."""
        branch = self.s["branch"]
        rev = lambda ref: self.git("rev-parse", "-q", "--verify", ref).stdout.strip()
        upstream = self.git("for-each-ref", "--format=%(upstream)", f"refs/heads/{branch}").stdout.strip()
        remote = rev(upstream) if upstream else rev(f"refs/remotes/origin/{branch}")
        return None if not remote else remote == rev(branch)

    def ship(self):
        # A fix round pushes to the open PR. Keyed on the PR, not the report comment, which may have failed to post.
        fix_round = bool(self.s.get("pr_url")) and self.s["rounds"].get("land", 0) > 0
        unfinished = None
        if (self.s.get("dispatched") or {}).get("stage") == "ship":
            pushed = self.pushed() if self.s.get("pr_url") else False
            if self.s.get("pr_url") and pushed is not False:
                if pushed is None:
                    self.warnings.append("couldn't confirm the run branch was pushed (no remote-tracking branch); "
                                         "taking the recorded pr_url as shipped")
                self.advance("land", "pushed PR fixes" if fix_round else "PR opened")
                return None
            unfinished = ("no pr_url is recorded yet" if not self.s.get("pr_url")
                          else "the run branch has commits that aren't on the remote yet")
        self.s["dispatched"] = {"stage": "ship", "round": self.s["rounds"].get("land", 0), "at": now()}
        self.save()
        kind = "PR fix round (push to the open PR)" if fix_round else "first ship (open the PR)"
        return self.result(
            "ship", f"Read {skill_dir() / 'guides' / 'ship.md'} and follow it for a {kind}. When the branch is "
            "pushed and pr_url is recorded, run next.", kind="fix-round" if fix_round else "first",
            unfinished=unfinished)

    # -- human stops ---------------------------------------------------------------------------------------

    def human(self):
        out = self.github()
        if out is not False:
            return out
        s, kind = self.s, checkpoint_kind(self.s)
        entry, cmd = s["history"][-1], f"{self.state_cmd} answer {s['id']}"
        common = {"kind": kind, "note": entry["note"], "answer": f"{cmd} <answer>  (feedback text on stdin: "
                  f"{cmd} - <<'EOF' ... EOF)"}
        if self.github_note:
            common["check_github"] = self.github_note
        triage = read(self.folder, "triage.md")
        if kind == "triage-questions":
            return self.result(
                "ask", "Show triage's open questions and their suggested defaults, then ask. `approve` means use the "
                "defaults; any other text answers the questions; the run goes on to spec either way.",
                triage=str(self.folder / "triage.md"), open_questions=section(triage, "Open questions"),
                choices=["approve", "steer", "pause", "abort"], **common)
        if kind == "triage-rejected":
            return self.result(
                "ask", "Show triage's reason and suggested split, then ask. `abort` ends the run; any other text "
                "overrides triage and goes on to spec.", triage=str(self.folder / "triage.md"),
                reason=section(triage, "Reason"), choices=["abort", "steer", "pause"], **common)
        if kind == "spec-approval":
            return self.result(
                "ask", "Send spec_text verbatim in a user-visible message before opening the approval question. "
                "Then show the spec path and say the human can edit it before approving. Any text other than the "
                "choices is a revision request.",
                spec=str(self.folder / "spec.md"), spec_text=(self.folder / "spec.md").read_text(),
                choices=["approve", "steer", "pause", "abort"], **common)
        if kind == "ship-approval":
            return self.result(
                "ask", "Present the ship decision: the note if there is one, the diff stat and commits, the review "
                "verdict and any non-blocking notes that affect risk, the verification results, each UI criterion's "
                "replay command and video, and any loop cap hit, stated plainly. Then ask. Feedback goes back to a "
                "stage you choose: answer with --to spec (requirements wrong), implement (code wrong or missing), "
                "or verify (evidence insufficient), and say which and why in one line.",
                choices=["ship", "steer", "pause", "abort"], **common, **self.ship_details())
        if kind == "steer":
            return self.result(
                "ask", f"The human asked to steer. Read {skill_dir() / 'guides' / 'steer.md'} and follow it, then "
                "record the outcome with answer --to <stage>.", **common)
        latest = str(self.folder / OUTPUTS[entry["from"]]) if entry["from"] in OUTPUTS else None
        common.pop("note")  # it's the question
        return self.result(
            "ask", f"A stage is blocked. Lead with its question, then read {skill_dir() / 'guides' / 'steer.md'} "
            "and follow it. The human's answer goes back with answer.", question=entry["note"],
            raised_by=entry["from"], output=latest, **common)

    def ship_details(self):
        s = self.s
        span = f"{s['base_branch']}...{s['branch']}"
        git_out = lambda *a: self.git(*a).stdout.strip()
        details = {"diff_stat": git_out("diff", "--stat", span),
                   "commits": git_out("log", "--oneline", f"{s['base_branch']}..{s['branch']}"),
                   "review": field(read(self.folder, "review.md"), "Verdict"),
                   "review_file": str(self.folder / "review.md"),
                   "verification_file": str(self.folder / "verification.md")}
        try:
            verdict, suite, found = verification_results(self.folder, s)
            details["verification"] = {"verdict": verdict, "suite": suite, "results": found}
        except Invalid as exc:
            details["verification"] = f"invalid: {exc}"
        replays = re.findall(r"^(Replay|Video): *(.+)$", read(self.folder, "verification.md"), re.MULTILINE)
        details["replay"] = [f"{k}: {v}" for k, v in replays]
        details["skipped"] = run_skips(s)
        approval = spec_approval(s)
        if approval and not approval.startswith("approved by a human"):
            details["spec_approval"] = f"Spec: {approval}"
        return details

    def github(self):
        """Read /factory answers on the run's issue or PR, and apply the latest trusted one.

        Returns False to go on asking, None when an answer moved the run, or an instruction.
        """
        self.github_note = None
        s = self.s
        if not (s.get("thread_url") or s.get("pr_url")) or self.entered == s["stage"]:
            return False
        if not shutil.which("gh"):
            self.github_note = ("gh isn't installed: read new `/factory` comments on " + ", ".join(
                u for u in (s.get("thread_url"), s.get("pr_url")) if u) + " with your GitHub tooling. Only people "
                "with write access (or in factory.approvers) count; apply the latest with answer --from.")
            return False
        try:
            box = inbox(s)
        except SystemExit:
            self.warnings.append("couldn't read GitHub for answers; ask locally")
            return False
        for item in box["ignored"]:
            self.tell.append(f"Ignored `/factory` comment from @{item['author']} ({item['reason']}): {item['url']}")
        if not box["answers"]:
            if box["ignored"]:
                s["inbox_seen"] = now()
                self.save()
            return False
        latest = box["answers"][-1]
        for earlier in box["answers"][:-1]:
            self.tell.append(f"Not used: an earlier answer from @{earlier['author']}: {earlier['url']}")
        source = f"@{latest['author']} on GitHub: {latest['url']}"
        moves, outcome = len(s["history"]), None
        try:
            outcome = self.answer(latest["answer"], source=source)
        except NeedsStage as exc:
            return self.result(
                "ask", f"A trusted answer came from GitHub, but it needs a stage: {exc} Apply it with answer --to "
                f"<stage> --from '{source}'.", kind=checkpoint_kind(s), github_answer=latest)
        except Refused as exc:
            self.tell.append(f"Didn't apply the GitHub answer from {source}: {exc}")
        s["inbox_seen"] = now()
        self.save()
        if outcome == "paused":
            return self.result("paused", f"{source} paused the run. It stays where it is; `<factory> <run-id>` "
                               "picks it up again.")
        return None if len(s["history"]) > moves else False

    # -- answers -------------------------------------------------------------------------------------------

    def answer(self, text, to=None, source=None):
        """Apply a human's answer to the stop the run is at. Returns 'paused', or None once the run moved."""
        s, st = self.s, self.s["stage"]
        text = text.strip()
        if not text:
            raise Refused("the answer is empty")
        if st in TERMINAL:
            raise Refused(f"run is {st}; it takes no answers")
        kw = command_answer(text)
        src = f" (from {source})" if source else ""
        if kw == "pause":
            return "paused"
        if kw == "abort":
            self.advance("aborted", "aborted by a human" + src)
            return None
        if kw == "steer" and st != "blocked":
            self.advance("blocked", f"steer requested at {st}")
            return None
        if st == "checkpoint-1" and checkpoint_kind(s).startswith("triage"):
            if kw == "ship":
                raise Refused("the run is waiting on triage's questions: answer approve (use the defaults), your "
                              "answers, steer, pause or abort")
            self.feedback(f"Checkpoint 1: triage{src}", "Use triage's suggested defaults." if kw == "approve" else text)
            self.advance("implement" if "spec" in skip_setting(s) else "spec", "triage answered", human=True)
        elif st == "checkpoint-1":
            if kw == "ship":
                raise Refused("the run is waiting on spec approval (checkpoint 1), not ship approval: answer "
                              "approve, feedback, steer, pause or abort")
            heading = f"Checkpoint 1: round {s['rounds']['spec']}{src}"
            if kw == "approve":
                open_qs = section(read(self.folder, "spec.md"), "Open questions").rstrip(".").lower() not in ("", "none")
                self.feedback(heading, "Approved." + (" Accepts the defaults for the spec's open questions."
                                                      if open_qs else ""))
                self.advance("implement", "spec approved")
            else:
                self.feedback(heading, text)
                self.advance("spec", "revise")
        elif st == "checkpoint-2":
            heading = f"Checkpoint 2: round {s['rounds']['implement']}{src}"
            if kw == "approve":
                raise Refused("the run is waiting on ship approval (checkpoint 2), not spec approval: answer ship, "
                              "feedback, steer, pause or abort")
            if kw == "ship":
                self.feedback(heading, "Ship.")
                self.advance("ship", "ship approved")
            elif to not in ("spec", "implement", "verify"):
                raise NeedsStage("ship-approval feedback goes back to spec (the requirements were wrong), implement "
                                 "(the code is wrong or missing something), or verify (the evidence is insufficient): "
                                 "pass --to with one of them.")
            else:
                self.feedback(heading, text)
                self.advance(to, "sent back")
        elif st == "blocked":
            entry = s["history"][-1]
            steer = re.match(r"steer requested at (\S+)", entry["note"] or "")
            if steer:
                origin, target = steer.group(1), to or steer.group(1)
            elif entry["from"] == "land":
                if to not in ("implement", "spec", "land"):
                    raise NeedsStage("land asked a question: pass --to implement (make a change), spec (the "
                                     "requirements change), or land (keep waiting on the PR).")
                origin, target = "land", to
            else:
                origin, target = entry["from"], to or entry["from"]
            self.feedback(f"Steer: {origin}{src}", text)
            self.advance(target, "unblocked" if target == origin and not steer else "steered", human=True)
        elif st == "land":
            if kw in ("approve", "ship"):
                raise Refused("the PR is already open, and only a human merges it: give feedback for a fix round, "
                              "or pause, steer or abort")
            self.advance("implement", "PR feedback", human=True)
            self.feedback(f"Land: round {s['rounds']['land']}{src}", text)
        else:
            raise Refused(f"the run is at {st}; nothing is waiting on an answer. You can pause, steer or abort.")
        return None

    # -- the end -------------------------------------------------------------------------------------------

    def finish(self):
        s, st = self.s, self.s["stage"]
        if s.get("finished_at"):
            return self.result("ended", "This run has ended; there is nothing left to do.", outcome=st,
                               pr_url=s.get("pr_url"))
        before_work = st == "aborted" and not s["rounds"].get("implement") and not read(self.folder, "feedback.md")
        if not (s.get("learned_at") or s.get("retro_skipped") or before_work):
            d = s.get("dispatched")
            if not d or d["stage"] != "retro":
                return self.dispatch("retro")
            if not self.fresh(d):
                return self.dispatch("retro", again=True)
            try:
                message, warning = learn_run(self.path, s)
                self.did.append(f"learned: {message}")
                if warning:
                    self.warnings.append(warning)
            except Refused as exc:
                self.warnings.append(f"learn failed: {exc}")
        proposals = [line[2:].strip() for line in section(read(self.folder, "retro.md"), "Proposed repo changes").splitlines()
                     if line.startswith("- ") and line[2:].strip().rstrip(".").lower() != "none"]
        if s.get("worktree"):
            try:
                released = release_run(s)
            except Refused as exc:
                released = None
                self.warnings.append(f"{exc}. The worktree is left at {s['worktree']}")
        else:
            current = self.git("branch", "--show-current").stdout.strip()
            released = None
            if current == s["branch"]:
                r = self.git("switch", s["base_branch"])
                released = f"switched back to {s['base_branch']}" if r.returncode == 0 else None
                if r.returncode != 0:
                    self.warnings.append(f"couldn't switch back to {s['base_branch']}: {r.stderr.strip()[:200]}")
        s.pop("dispatched", None)
        s["finished_at"] = now()
        self.save()
        return self.result(
            "finish", "Tell the human the outcome and the PR link. List each proposed repo change in one line with "
            "the command that would start it as a run (`<factory> <proposal>`); never start them yourself. Then print "
            f"`{self.state_cmd} stats {s['id']}`. If you subscribed to PR events, unsubscribe.",
            outcome=st, pr_url=s.get("pr_url"), proposals=proposals, released=released)


def cmd_next(run_id, tokens=None, usd=None):
    path, s = load(run_id)
    driver = Driver(path, s)
    try:
        if tokens is not None:
            driver.record_usage(tokens, usd)
        print(json.dumps(driver.run(), indent=2))
    except Refused as exc:
        die(str(exc))


def cmd_answer(run_id, text, to=None, source=None):
    path, s = load(run_id)
    driver = Driver(path, s)
    try:
        if driver.answer(text, to=to, source=source) == "paused":
            print(json.dumps(driver.result("paused", "The run is paused where it is. `<factory> <run-id>` picks it "
                                           "up again."), indent=2))
            return
        print(json.dumps(driver.run(), indent=2))
    except Refused as exc:
        die(str(exc))


COMMANDS = {
    "new": (cmd_new, 2), "list": (cmd_list, 0), "show": (None, 1), "get": (cmd_get, 2),
    "advance": (cmd_advance, 2), "set": (cmd_set, 3), "feedback": (cmd_feedback, 2),
    "check-verification": (cmd_check_verification, 1), "pr-report": (cmd_pr_report, 1),
    "release": (cmd_release, 1), "pr-status": (cmd_pr_status, 1), "inbox": (cmd_inbox, 1),
    "checkpoint-post": (cmd_checkpoint_post, 1), "pr-update": (cmd_pr_update, 1), "usage": (cmd_usage, 3), "stats": (cmd_stats, 0),
    "policy": (cmd_policy, 1), "learn": (cmd_learn, 1), "signals": (cmd_signals, 0), "seen": (cmd_seen, 1), "approval": (cmd_approval, 1),
    "skips": (cmd_skips, 0), "next": (cmd_next, 1), "answer": (cmd_answer, 2),
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    name, args = argv[0], argv[1:]
    if name == "show":
        if len(args) != 1:
            die("usage: show <run-id>")
        path, s = load(args[0])
        print(path.read_text(), end="")
        if s["stage"] in HUMAN_STAGES and (s.get("thread_url") or s.get("pr_url")):
            # Resuming without an answer must look on GitHub first; `next` does, so say to use it.
            print(f"note: {s['id']} is waiting on a human and has a GitHub thread. Without an answer in the "
                  f"arguments, run `next {s['id']}`: it applies the latest trusted /factory answer before asking.",
                  file=sys.stderr)
        return
    fn, nargs = COMMANDS[name]
    if name in WRITERS and args:
        with run_lock(args[0]):
            return dispatch(name, fn, nargs, args)
    return dispatch(name, fn, nargs, args)


# Commands that change a run's state.json or feedback.md; each holds the run's lock while it runs.
WRITERS = {"advance", "set", "feedback", "seen", "usage", "learn", "next", "answer"}


def dispatch(name, fn, nargs, args):
    if name == "new":
        if len(args) < 2:
            die("usage: new <slug> <input-file|-> [--in-place] [--parent <run-id>] [--skip <stages>]")
        rest, opts = args[2:], {}
        while rest:
            if rest[0] == "--in-place":
                opts["in_place"], rest = True, rest[1:]
            elif rest[0] == "--parent" and len(rest) > 1:
                opts["parent"], rest = rest[1], rest[2:]
            elif rest[0] == "--skip" and len(rest) > 1:
                opts["skip"], rest = rest[1], rest[2:]
            else:
                die(f"unknown option '{rest[0]}' for new")
        return fn(*args[:2], **opts)
    if name == "signals":
        days = "14"
        if "--days" in args:
            i = args.index("--days")
            if i + 1 >= len(args):
                die("--days takes a number")
            days, args = args[i + 1], args[:i] + args[i + 2:]
        if len(args) > 1:
            die("usage: signals [<run-id>] [--days N]")
        return fn(*args, days=days)
    if name == "next":
        opts, rest = {}, list(args[1:])
        while rest:
            flag = rest.pop(0)
            if flag in ("--tokens", "--usd") and rest:
                try:
                    opts[flag[2:]] = int(rest[0]) if flag == "--tokens" else float(rest[0])
                except ValueError:
                    die(f"{flag} takes a number")
                if opts[flag[2:]] < 0:
                    die("usage cannot be negative")
                rest.pop(0)
            else:
                die("usage: next <run-id> [--tokens N [--usd X]]")
        if not args or ("usd" in opts and "tokens" not in opts):
            die("usage: next <run-id> [--tokens N [--usd X]]")
        return fn(args[0], **opts)
    if name == "answer":
        opts, rest = {}, list(args[1:])
        while rest and rest[0] in ("--to", "--from") and len(rest) > 1:
            opts["to" if rest[0] == "--to" else "source"] = rest[1]
            rest = rest[2:]
        if not args or not rest:
            die("usage: answer <run-id> [--to <stage>] [--from <source>] <answer | ->")
        text = sys.stdin.read() if rest == ["-"] else " ".join(rest)
        return fn(args[0], text, **opts)
    if name == "skips" and len(args) <= 1:
        return fn(*args)
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

# Factory evals

A fixed set of tickets on a small sample repo, for checking that a change to the factory's
stage prompts or orchestrator made it better, not worse. Each case starts from a fresh copy of
[`fixture/`](fixture/) (`textstat`, a tiny Python CLI with a word-count bug), installs the
factory skill there, and drives the factory headlessly until it stops where a human would decide.
The simulated human approves the spec and stops at ship approval, so nothing is ever pushed.

```sh
python3 testing/evals/run.py                         # every case, with Claude Code
python3 testing/evals/run.py --case json-output      # one case
python3 testing/evals/run.py --save-baseline         # record this run as the baseline
python3 testing/evals/run.py --keep                  # keep each case's repo to inspect
FACTORY_EVAL_AGENT='codex exec {prompt}' python3 testing/evals/run.py   # another harness
```

## What is graded

| Check | Passes when |
|---|---|
| `triage` | Triage's verdict is one the case allows. A vague or out-of-scope request should come back as `needs-human` or `reject`. |
| `stage` | The run stopped where the case expects: `checkpoint-2` for work the factory should finish, `checkpoint-1` for triage questions. |
| `verification` | `state.py check-verification` validates the report, and its verdict matches. |
| `hidden_test` | A test the agents never saw, from [`hidden/`](hidden/), passes on the run branch. It is copied in only for grading. This is the check that proves the change works, independent of the factory's own verify stage. |
| `files` | The run branch changes only files the case allows. |

Each result also records the run's rounds, agent time, spec revisions, loop caps hit, and tokens
(from `state.py stats`). `report.md` compares them with `baseline.json` when it exists, and names
each case that regressed or now passes. Results go to `results/<timestamp>/`, with one agent log
per case. That folder is git-ignored; commit `baseline.json` when you mean to move the bar.

## Adding a case

Add an entry to [`cases.json`](cases.json):

- `input`: the ticket text, exactly as a human would write it.
- `expect.triage`: the triage verdicts you'd accept.
- `expect.stage`: where the run should stop.
- For work the factory should finish, also set `verification`, `hidden_test`, and `files`, with glob patterns.
- `answers` (optional): what the simulated human says, e.g. `{"checkpoint-1": "approve"}`. `approve` is the default for spec approval; triage questions end the case.

Make the hidden test fail on the unmodified fixture, so a pass means the factory actually did the
work. The runner's own tests use [`fake_agent.py`](fake_agent.py) and cost no model time:
`python3 testing/test_evals.py`.

Real runs take several minutes per case and use model tokens. Run them when you change files in
`stages/`, `guides/`, `SKILL.md`, or `state.py`'s stage rules, not on every commit.

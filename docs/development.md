# Development

Run `python3 testing/test_state.py` for state helper tests and `python3 testing/test_evals.py` for the eval runner. CI runs both on every push to `main` and every PR, on Linux and macOS ([`.github/workflows/test.yml`](../.github/workflows/test.yml)). [`testing/README.md`](../testing/README.md) describes harness-specific stub agents and exercised end-to-end paths. [`testing/evals/`](../testing/evals/README.md) runs the factory against fixed tickets on a sample repo and grades the results with hidden tests, so you can tell whether a prompt change helped.

This version does not start runs from webhooks or cron, or watch PRs and signals on its own between sessions; a human or a scheduled job runs `<factory> <run-id>` and `<factory> signals`. With `factory.skip` set, such a job can take a run from input to PR without anyone answering a checkpoint.

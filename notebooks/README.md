# Reproducible examples

Empty notebook placeholders were removed. Use the tracked runnable examples:

- `python -m scripts.build_priority_benchmark`: original synthetic priority data.
- `python -m scripts.evaluate_priority --output models/new-run --report-dir docs/evaluation/new-run`: grouped evaluation.
- `python -m scripts.verify_local_only --model ... --embedding-cache ...`: cached-asset local-only check.
- `python -m scripts.demo_interview`: synthetic API, feedback and recovery rehearsal.

No notebook should load private mail or credentials by default.

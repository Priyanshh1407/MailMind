# Contributing to MailMind

Thank you for improving MailMind. This project handles email and OAuth state, so privacy and account isolation are part of correctness, not optional cleanup.

## Before changing code

1. Read the [architecture](docs/ARCHITECTURE.md), [security policy](SECURITY.md), and [privacy policy](docs/PRIVACY_AND_LOCAL_ONLY.md).
2. Create local configuration from `.env.example`; never commit a real `.env`.
3. Use only synthetic or explicitly approved test data.
4. Keep new features account-scoped, generation-fenced, bounded, and recoverable.

## Development checks

Run the backend suite from the repository root:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
.\venv\Scripts\python.exe -m pip check
```

Run the frontend gates:

```powershell
Set-Location frontend
npm test -- --run
npm run lint
npm run build
npm run test:browser
Set-Location ..
```

Every new backend or frontend feature must include a focused test. Changes to account ownership, migrations, external calls, retries, or side effects also require failure and recovery coverage.

## Privacy gate

Before staging changes:

- confirm `.env`, `credentials.json`, `token.json`, `data/`, `models/`, exports, databases, logs, and generated reports are ignored;
- inspect `git status --short --ignored`;
- run `git diff --check`;
- inspect staged filenames with `git diff --cached --name-only`;
- scan staged content for credentials and personal identifiers;
- never publish live-mail screenshots, raw prompts, provider responses, OAuth files, or account addresses.

Synthetic fixtures under `fixtures/` and `tests/fixtures/` are intentionally versioned. Do not replace them with copied inbox data.

## Documentation

- Update public behavior in the README, user guide, architecture, privacy, setup, and test guides as applicable.
- Keep public docs linked from the [documentation index](docs/README.md).
- Keep generated phase reports, evaluation output, and private planning documents out of Git.
- State limitations honestly; automated tests are not proof of live-provider reliability or production model accuracy.

## Pull requests

Keep each pull request focused. Explain what changed, why it changed, privacy or migration impact, recovery behavior, and the exact tests run. Do not include generated build output or local runtime artifacts.

No repository-wide software license is currently declared. External contributors should obtain maintainer guidance before submitting work intended for redistribution.

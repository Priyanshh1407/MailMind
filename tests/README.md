# Test guide

All automated tests use synthetic or temporary data. They do not read a real inbox, private model checkpoint, OAuth token, provider credential, or personal database.

## Backend

Run the complete suite:

~~~powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

At the 25 September 2026 documentation refresh, all 384 discovered backend tests passed, including the release-policy checks for exact dependency declarations.

The backend suite covers:

- versioned SQLite migrations;
- account/session isolation and generation fencing;
- OAuth cancellation and account switching;
- MIME and HTML extraction;
- bounded Gmail intake and history-cursor synchronization;
- durable tasks, leases, retries, and notification ambiguity;
- provider fallback and structured output;
- feedback revisions and conservative retrieval;
- local-only routing and asset verification;
- semantic search reconciliation and hybrid ranking;
- supervisor ownership and restart limits;
- prompt-injection and retrieval-poisoning boundaries; and
- privacy-safe failure behavior.

Focused security checks:

~~~powershell
.\venv\Scripts\python.exe -m unittest tests.test_security_hardening -v
.\venv\Scripts\python.exe -m scripts.audit_dependencies
~~~

The dependency audit sends package names and versions to OSV. Reviewed deployment-specific exceptions are defined in the repository security advisory policy.

## Frontend

~~~powershell
Set-Location frontend
npm test -- --run
npm run lint
npm run build
npm run test:browser
Set-Location ..
~~~

Current verified result:

- 26 Node unit tests passed;
- 19 Playwright browser scenarios passed;
- lint passed;
- production build passed.

If Playwright Chromium is unavailable, run <code>npx playwright install chromium</code> or configure a compatible installed Chrome/Chromium executable.

## Integration helpers

~~~powershell
.\venv\Scripts\python.exe -m scripts.verify_launcher
.\venv\Scripts\python.exe -m scripts.verify_local_only --model <classifier> --embedding-cache <cache>
.\venv\Scripts\python.exe -m scripts.verify_containers
.\venv\Scripts\python.exe -m scripts.check_clean_release
~~~

These helpers have different scopes:

| Helper | Purpose |
| --- | --- |
| <code>verify_launcher</code> | Owned startup, readiness, port conflict, and graceful-stop behavior |
| <code>verify_local_only</code> | Offline asset and provider-blocking behavior |
| <code>verify_containers</code> | Local-only container build/runtime boundary |
| <code>check_clean_release</code> | Private-file-free temporary source installation and verification |

The container verifier uses a unique Compose project and removes only its own resources.

## Live validation

Automated checks do not prove live OAuth, Gmail history behavior, provider retention, cloud-model reliability, Telegram delivery, or real-inbox accuracy.

A deliberate live validation should confirm:

- successful OAuth completion;
- account switching in both directions;
- live-sync timestamp movement;
- controlled new-message discovery and classification;
- continuous Gmail heartbeat during semantic indexing;
- semantic pending/indexed/failed movement;
- search results for known controlled mail;
- queue cap and historical-backlog separation; and
- account disconnect/deletion behavior.

Do not publish live validation logs or screenshots without removing account identifiers and mail content.

## Release rule

Do not describe the suite as fully passing while an unexpected failure remains. Resolve the dependency declaration mismatch, rerun the backend suite, and update public verification evidence before release.

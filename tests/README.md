# Test guide

All automated tests use synthetic or temporary data. They do not read a real inbox, private model checkpoint, OAuth token, provider credential, or personal database.

## Backend

Run the complete suite:

~~~powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

At the 19 October 2026 verification, all 669 discovered backend tests passed. Each of the 25 earlier test modules passed on its own on 6 October; the new `test_signals`, `test_risk`, `test_ledger`, `test_inbox_agent` and `test_follow_ups` modules also pass on their own.

One full run on 19 October 2026 reported a single error that did not recur in eight later full runs (two in place, six in an isolated copy). Its test name was not captured, so no cause is claimed. If it happens again, keep the run's output: `unittest` prints the failing test's name and traceback.

Regression and contract suites added after the audit:

- `test_audit_regressions.py`: every audit finding reproduced through the real code path (provider failover, shadow authority, grounding, deadlines, explanations, Telegram timing, retry-time repair, deletion errors, configuration messages, the action-type filter).
- `test_provider_faults.py`: 9 provider fault types × 2 scopes against a written contract (rendered to `docs/RESILIENCE.md`).
- `test_cloud_evaluation.py`: the evaluation harness scores recordings exactly as production parses them.
- `test_follow_ups.py`: names, amounts, links and addresses masked and restored locally (links without trailing punctuation), greetings that follow how the user addressed people, style learned from sent mail, every AI-tell and invented-fact rule, one revision then problems shown, a fake Gmail that fails the test if anything but a read is called, the sent list, draft job and copy marking, switched-off and local-only modes, purge, migration 19, privacy of every benchmark request, and the report replay.
- `test_inbox_agent.py`: cited answers kept and quotes verified, unread or invented quotes removed, honest abstention vs unverified, step and read limits, other accounts unreachable, money only as local tokens (the provider never sees the number), injected text delivered as data and unable to be cited into a lie, Groq function-call salvage, the question job (trace without email text, history, rate limit), provider failure, local-only and switched-off modes, purge, migration 18 and the report replay.
- `test_ledger.py`: amounts in Indian and foreign formats (currency before or after), the total cue beating items, subtotals, savings and balances, the placeholder privacy contract on every benchmark request (and a planted leak caught), cloud answers validated and filled in locally, subscriptions from monthly and yearly series but not irregular repeats, migration 17 keeping token rows, one cloud call per cycle, retry then rules fallback, corrections surviving rescans, account fencing, classification request bytes pinned, the routes, purge, and a report that replays byte for byte.
- `test_risk.py`: the scam rules (lookalikes, links, lures, the OTP-warning and statement-password exclusions), the property that adding a red flag never lowers a score, weights that refit identically, a report that replays byte for byte, a train/test split with no shared template, re-scoring when signals or rules change, a scoring failure that never undoes a classification, the high-risk alert prefix, purge, and the routes with the feature on and off.
- `test_signals.py`: security signals (Gmail's authentication verdicts preferred over forged earlier hops, reply paths, links, hidden text) are extracted without changing the visible body, saved with the email, backfilled without side effects, fenced on account switch, purged with the account, and exposed by a CSRF-protected route.

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
- supervisor ownership, unlimited restarts with backoff, and warn-before-stop on a startup crash loop;
- prompt-injection and retrieval-poisoning boundaries; and
- privacy-safe failure behavior; and
- configuration errors that name the exact setting to change.
- schema-14 bounded intelligence backfill, queue priority, recovery, side-effect suppression, rollout gating, CSRF, account isolation, and purge.

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

- 65 Node unit tests passed;
- 45 Playwright browser scenarios passed;
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
- one explicit action/deadline email, one informational update, and one ambiguous request;
- explanation source labels, action lifecycle persistence, and reminder restart recovery;
- provider-attempt token accounting and day/week/month reconciliation; and
- bounded saved-mail backfill with no historical alert, read-state, or automatic-reminder side effects.

Do not publish live validation logs or screenshots without removing account identifiers and mail content.

## Release rule

Do not describe automated verification as live-provider validation. Resolve every unexpected failure, rerun the affected complete gate, and record live OAuth/Gmail results separately.

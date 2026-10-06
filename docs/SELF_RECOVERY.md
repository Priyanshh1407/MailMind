# Self-recovery

MailMind recovers on its own from **temporary** failures: lost internet, Gmail, AI or Telegram outages, quota limits, and crashed processes. It **fails safe** on permanent ones: it stops only that piece of work, keeps your data visible, and says what needs a human.

The rule behind every layer below: **temporary problems retry with exponential backoff, forever; only a provably impossible situation stops, and the user is told first.**

## Layer 1: Processes (`scripts/launch.py`)

The supervisor owns four processes: indexer, API, worker and frontend server.

- **Restarts.** It checks every child 4 times a second. A child that exits is restarted after 1, 2, 4, 8, 16, 32, then 60 s, with no limit, the API included. A child that ran for at least 10 s before exiting starts the count again.
- **The only stop condition** is a startup crash loop: 5 exits in a row, each within 10 s of starting. The supervisor then:
  1. prints a `WARNING` in the terminal (service, exit code, log file) and shows it in the dashboard with a countdown;
  2. keeps restarting for 60 s;
  3. cancels the shutdown if the service recovers, or stops the owned set cleanly if it doesn't.
- **Status for the dashboard.** The supervisor's state is written to `.run/supervisor-status.json`, and the frontend server serves it as `/mailmind-supervisor.json`, so alerts work even while the API is down.
  - The file is rewritten only when it changes.
  - On Windows, a rename blocked by another reader ("Access is denied") is retried briefly, then skipped. A status-file problem can never stop MailMind.
- **Testability.** `supervise()` takes injected spawn, clock and stop functions; the tests drive it with fake processes and a fake clock.

## Layer 2: Durable work (SQLite)

- **Nothing is lost on a crash.** Every email is a `processing_tasks` row with a status and a next-retry time.
- **Worker lease.** The worker holds a 90-second lease and renews it on every step. If the worker dies, the lease expires, its running tasks return to `retry` (`interrupted`), and they run again.
- **Generation fencing.** Every restart, logout, disconnect and account switch increments a generation number. Late results from older work are refused.
- **Self-repair.** A non-numeric retry time, which could never become due, is reset every cycle.

## Layer 3: Restarts keep Google connected (`src/account_state.py`)

An API restart revokes browser sessions and bumps the generation. It keeps `connected` when the saved Google login still exists and no sign-in or deletion is in progress. The dashboard opens a new local session silently.

## Layer 4: Network and Gmail outages

- **Login failure vs temporary failure.**
  - Google rejecting the saved login (`RefreshError`, 401/403, wrong account) pauses the account.
  - A network error, DNS failure, timeout, 5xx or 429 raises `GmailUnavailable` instead: the cycle is skipped, the account stays connected, and no email spends a retry.
- **Backoff.** The worker waits 5, 10, 20, 40, then every 60 s, and resets after the first successful cycle.
- **Status.** `/status.connectivity` reports the failed-check streak and the seconds until the next check, computed with the same formula as the worker.
- **Catch-up.** Nothing is missed:
  - the Gmail history cursor never moves past a failed listing;
  - failed downloads are re-fetched by ID;
  - every 5 minutes a sweep lists the last 2 days of INBOX and fetches anything unsaved.

## Layer 5: Retry until it works

Each failure is classified as temporary or permanent (`provider_failure`).

- **Temporary failures retry forever, at most 5 minutes apart** (5 · 2ⁿ⁻¹ s, capped at 300 s):
  - classification (Gemini, then Groq);
  - Telegram alerts and reminders;
  - Gmail downloads;
  - search indexing (the backoff is timed from its last attempt);
  - feedback indexing (3 quick tries, then every 5 minutes).
- **Permanent failures stop at once, with the reason:**
  - a rejected request or API key, or an invalid AI answer → Needs Review;
  - unconfigured Telegram;
  - an unreadable or deleted email → quarantined after a few tries;
  - an alert that may already have been sent → never resent automatically.

See [RESILIENCE.md](RESILIENCE.md) for the tested outcome of every provider fault.

## Layer 6: The dashboard never goes blank

- **Data stays on screen.** On refresh errors the last data stays visible, writes pause, and the refresh backs off 5 → 60 s.
- **Gmail-style alerts**, one at a time, highest priority first:
  1. *MailMind may need to stop* (with a countdown);
  2. *You're offline*;
  3. *Restarting a MailMind service*;
  4. *Can't reach Gmail · Retrying in N s*.

  When the problem clears, *You're back online* or *Everything is running again* appears for a few seconds.
- **Login rejected → read-only mode.** Saved mail, history, sources and actions stay readable; changes are disabled until you sign in again.
- **Silent reconnect.**
  - Disconnect and Stop processing park the Google login unused for 24 hours.
  - Connect Google within that time renews it without Google's page, if Google still accepts it and it's the same account.
  - Switching accounts always opens Google's page.

## What still needs a human

| Situation | Why software can't fix it | What MailMind does |
| --- | --- | --- |
| Google rejected the login (revoked or expired) | Google requires the user's approval | Read-only mode and a clear "sign-in needs renewing" message |
| Bad API key, rejected request, invalid AI answer | Retrying returns the same answer | The email goes to Needs Review with the reason |
| Telegram not configured | A setup choice | Alerts stop; no endless retries |
| An alert that may have been sent | Retrying could duplicate it | Asks the user to confirm |
| Deleted or unreadable email | The same result every time | Quarantined and shown in the dashboard |
| Startup crash loop | Restarts can't fix the cause | Warns, retries for 60 s, then stops cleanly |
| PC reboot or supervisor closed | Nothing starts MailMind again | Not covered: run `start_all.bat` |
| Disk full or database corruption | Outside the app | Not handled beyond "storage busy, retry" |

## Tests

- **Backend:**
  - `tests/test_audit_regressions.py`: `NetworkLossTests`, `OfflineBackoffTests`, `RetryUntilRecoveredTests`, `SupervisorResilienceTests`, `RestartKeepsGoogleTests`, `ReadOnlyAfterLoginProblemTests`, `SilentReconnectTests`, `LoginErrorClearingTests`, `LockedFileRetryTests`;
  - `tests/test_phase5_recovery.py`.
- **Browser:** the Playwright scenarios "connectivity alerts", "service alerts" and "a rejected Google login".

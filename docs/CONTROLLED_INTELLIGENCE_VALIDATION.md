# Controlled intelligence validation

This checklist is the deliberate live step after synthetic automated verification. It is not run by the ordinary test suite and should use a dedicated test account with non-sensitive synthetic messages.

## Before connecting

- Use a dedicated Google test account and synthetic senders.
- Keep real personal mail, attachments, credentials, account identifiers, and provider responses out of screenshots and logs.
- Review provider retention settings and the privacy policy.
- Start with automatic Gmail mark-read and both reminder flags disabled.
- Confirm schema 14, the intended feature flags, and a clean privacy-safe diagnostics result.

## Staged checks

1. Connect Google through the dashboard OAuth flow.
2. Send a synthetic email containing one explicit action and future deadline.
3. Confirm classification, a source-labelled explanation, the action type, resolved deadline, and a token event.
4. Send one informational update and confirm no false action is created.
5. Send one ambiguous request and confirm review or conservative output with no automatic reminder.
6. Induce a retryable primary-provider failure in the controlled environment and confirm fallback attempts are counted once each.
7. Reconcile day, week, and month API totals against the account's stored token events.
8. Complete one action and snooze another, restart MailMind, and confirm both states survive.
9. Enable dashboard reminders only after the earlier checks; verify restart-safe delivery.
10. Enable Telegram action reminders only if explicitly desired, using a synthetic destination.
11. Select **Analyze up to 20 saved emails** and confirm live mail remains ahead of backfill.
12. Confirm backfilled messages produce no historical Telegram alert, Gmail read-state change, or automatic reminder.
13. Switch to a second dedicated account and confirm no first-account actions, usage, explanations, or backfill state appears.
14. Run semantic indexing and confirm the Gmail worker heartbeat remains current.
15. Delete the test account's local data and confirm mail, analysis, actions, reminders, token events, mutation gates, and backfill rows are removed without affecting the second account.

## Evidence record

Record only timestamps, synthetic message IDs, feature-flag values, safe state/count summaries, pass/fail, and the tested application revision. Do not copy message bodies, OAuth tokens, provider keys, raw prompts, raw provider responses, or account addresses.

Automated verification can be reported independently. Do not claim that live OAuth, Gmail, provider fallback, Telegram delivery, or real-inbox behavior passed until every applicable item above has been performed and recorded.

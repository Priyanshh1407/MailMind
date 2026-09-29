# Integrated intelligence feature contract

**Status:** implemented and regression-tested  
**Contract baseline:** 29 September 2026  
**Scope:** integrated extension of the MailMind application

This contract governs AI Action Center, explainable classification, and token
analytics. They are not separate applications or independent AI pipelines.
The features extend the existing Gmail intake, durable processing task, account
fencing, provider fallback, SQLite authority, API session, and dashboard
snapshot boundaries.

## Non-negotiable integration rules

- Gmail content remains inaccessible until Google OAuth is connected.
- Every persisted row, query, job, derived vector, and API result is account scoped.
- Logout, disconnect, account switch, restart, and deletion fence late work by
  the existing account generation.
- The processing-task cap and live-before-backlog priority remain intact;
  ordinary backlog also stays ahead of explicit intelligence backfill.
- The semantic indexer stays independent of the Gmail/classification worker.
- `NEEDS_REVIEW` remains a system presentation state, never a trained category.
- Cloud classification, explanation, and action candidates share one bounded,
  versioned provider request. A second request per email is not the default path.
- Classification remains valid if optional explanation/action persistence fails;
  derived work is reconciled separately.
- No email, payment, approval, calendar event, task-manager action, or reminder
  side effect occurs without the rule or user action defined here.
- Token events contain counts and safe metadata only—never prompts, email text,
  provider responses, credentials, action text, or explanation text.

## Action contract

### Types

The only initial wire values are:

| Value | Meaning |
| --- | --- |
| `reply_required` | The sender directly requests a reply or confirmation. |
| `approval_required` | The user is asked to approve or authorize something. |
| `payment_required` | A payment obligation is explicitly stated. |
| `document_required` | A document or form must be supplied or completed. |
| `meeting` | Attendance or a response to a meeting/appointment is requested. |
| `review_required` | Material must be reviewed by the user. |
| `follow_up_required` | The user is asked to follow up later. |
| `general_task` | A concrete task does not fit a more specific type. |

Provider aliases such as `approval`, `reply`, or `task` are invalid. Providers
must return these exact values.

### Status and transitions

New actions start as `open`. The allowed states are `open`, `completed`,
`dismissed`, and `snoozed`.

| From | Allowed next state | Authority |
| --- | --- | --- |
| `open` | `completed`, `dismissed`, `snoozed` | Explicit user action |
| `snoozed` | `open`, `completed`, `dismissed` | Expiry may reopen; other transitions require the user |
| `completed` | `open` | Explicit user reopen only |
| `dismissed` | `open` | Explicit user reopen only |

Reading the source email never completes an action. Re-analysis never overwrites
the user's current action state. Completing or dismissing clears any pending
snooze/reminder. Reopening does not recreate a reminder unless the deadline is
still valid under the reminder rules or the user explicitly schedules one.

Every mutation will require account ownership, CSRF, and the expected optimistic
revision. A stale revision returns a conflict instead of overwriting newer state.

### Confidence and limits

Confidence is a provider-facing band: `low`, `medium`, or `high`. It is not
displayed as measured probability or certainty.

- At most five action candidates may be accepted per email.
- Title: at most 160 characters.
- Description: at most 500 characters.
- Evidence: required and at most 320 characters.
- Evidence must be a normalized excerpt from the source email, not generated
  reasoning or retrieved precedent text.
- Low-confidence candidates are not persisted as user actions in the first
  release. Medium-confidence candidates may be retained for review but cannot
  create automatic reminders. High confidence is required for automation.

### Deadline resolution

The wire precisions are `exact_time`, `date_only`, `relative`, and `unknown`.

- All provider timestamps must include an explicit offset.
- Relative dates resolve against the source email timestamp in the configured
  timezone, never against the worker's current wall clock alone.
- Ambiguous expressions such as “sometime next week” become `unknown` with no
  `due_at`; MailMind does not invent a date.
- A deadline at/before the source timestamp is invalid for automatic scheduling.
- A deadline more than 366 days after the source timestamp is too distant for
  automatic scheduling in the first release.
- Date-only deadlines remain labelled date-only; the UI must not imply that the
  source specified an exact time.

### Reminder behavior

The first stable channel is `dashboard`. Telegram reminders remain disabled
until dashboard reminders pass recovery testing and the user opts in.

An automatic dashboard reminder is allowed only when all are true:

1. reminders and action extraction are enabled;
2. confidence is `high`;
3. bounded source evidence is present;
4. `due_at` is valid and `due_precision` is not `unknown`; and
5. the computed reminder is still in the future.

Default timing:

- `exact_time`: 30 minutes before the deadline; if that time has passed but the
  deadline has not, schedule at the deadline.
- `date_only`: 09:00 in the configured timezone on the due date.
- `relative`: use the resolved timestamp and the exact-time rule when a time was
  stated; otherwise use the date-only rule.
- `unknown`: never schedule automatically.

Manual reminders require an explicit user action and a future timestamp. Snooze
requires an explicit future `snoozed_until`. Snooze expiry changes the action to
`open`; reminder delivery never changes Gmail read state.

## Explanation contract

An explanation is a concise, observable justification—not chain-of-thought.

- Summary: required, plain text, at most 240 characters.
- Signals: zero to three fixed identifiers from `ExplanationSignal` in
  `src.intelligence_contract`.
- Optional evidence per signal: at most 240 normalized characters from the
  source email.
- Sources: `gemini`, `groq`, `local_heuristic`, or `system`.
- Model version is present for Gemini, Groq, and model-backed local output.
- `retrieval_used` records whether accepted account-owned feedback precedents
  contributed; retrieved text is never explanation evidence.
- HTML, URLs, executable content, hidden reasoning, and unsupported certainty
  claims are forbidden.

The explanation describes the original prediction. Human feedback changes the
effective category but does not rewrite the original prediction or explanation.
For Needs Review, `system` uses the existing safe reason code and user-facing
message. Provider failures cannot produce accepted actions.

## Token usage contract

### Providers and operations

Provider values are `gemini`, `groq`, `local`, and `embedding`. Operation values
are:

- `classification_analysis`;
- `local_shadow`;
- `document_embedding`;
- `query_embedding`;
- `manual_prediction`; and
- `action_reanalysis`.

Gemini and Groq are provider-billed cloud usage. Local classifier and embedding
tokens are locally processed usage. These groups are returned and displayed
separately; they are never combined into a monetary estimate.

Count methods are `provider_reported`, `tokenizer_counted`, `estimated`, and
`unavailable`. Outcomes are `success`, `failed`, `timeout`, and `cancelled`.

- Each actual provider/local attempt has a unique request ID.
- Retries and fallbacks are separate usage events.
- A database retry reuses the request ID and cannot duplicate an event.
- Missing usage is `unavailable` with nullable counts, never zero.
- Cancellation before work begins creates no event.
- A timeout after a request may have left the process records `timeout`; counts
  remain null unless trustworthy usage metadata later exists.
- Gmail and Telegram operations are never token consumers.

### Time windows

Storage timestamps are UTC. The default display timezone is `Asia/Kolkata`.
The week begins Monday and ends Sunday. Day, week, and month boundaries are
computed in the requested validated IANA timezone and converted to explicit UTC
query bounds. Cross-account aggregation is forbidden.

## Rollout configuration

The configuration is part of the existing `Settings` object:

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `MAILMIND_ACTION_EXTRACTION_ENABLED` | `false` | Accept/persist validated action candidates and permit explicit saved-mail backfill. |
| `MAILMIND_ACTION_REMINDERS_ENABLED` | `false` | Permit the durable reminder workflow; requires extraction enabled. |
| `MAILMIND_TELEGRAM_ACTION_REMINDERS_ENABLED` | `false` | Separately opt in to Telegram action-reminder delivery. |
| `MAILMIND_TOKEN_COLLECTION_ENABLED` | `true` | Record privacy-safe events once schema/instrumentation is installed. |
| `MAILMIND_TOKEN_ANALYTICS_VISIBLE` | `false` | Expose the validated read-only analytics UI. |
| `MAILMIND_EXPLANATIONS_VISIBLE` | `false` | Display validated source-labelled explanations. |
| `MAILMIND_DEFAULT_TIMEZONE` | `Asia/Kolkata` | Initial IANA timezone. |
| `MAILMIND_WEEK_START` | `monday` | Initial calendar-week boundary. |

Boolean values accept only `true` or `false`. Reminders cannot be enabled while
extraction is disabled. Analytics visibility cannot be enabled while collection
is disabled. Token analytics will be read-only (`GET`); there is no API for a
client to create or alter usage events.

The supported rollout order is token collection, analytics/explanations, action
extraction, dashboard reminders, and finally explicitly selected Telegram
reminders. Existing saved mail is never enriched automatically. The connected
dashboard offers a bounded backfill only after action extraction is enabled;
that path does not send historical alerts, mark mail read, or create automatic
reminders.

## Synthetic fixtures

`tests/fixtures/intelligence_features.json` contains only controlled
`example.test` identities. It covers every action type plus approvals, explicit
and relative deadlines, replies, meetings, an ambiguous date, spam, a no-action
update, prompt injection, and provider timeout. No live account or email data may
be added to this fixture.

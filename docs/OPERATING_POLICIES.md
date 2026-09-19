# MailMind operating policies

Current product policies for the supported single-user local deployment. They provide defaults that must be changed only through explicit product decisions and measured evaluation.

## Supported product scope

The initial release target is one locally hosted personal application on Windows
with one active Google account at a time. Switching between accounts is supported
only with isolated persisted records and retrieval. Linux/macOS and remotely
hosted multi-user use are not presently validated. Keep SQLite and Chroma for
this scope; infrastructure changes must solve an observed requirement.

Account ownership applies to mail, feedback, embeddings, jobs, and notification
state. Use account plus message identity, not Gmail message ID alone. Local HTTP
callers must still be authenticated for private operations. Each processing job
must check whether its account/session remains active before external effects.

## Categories versus processing outcomes

| Category | Meaning |
|---|---|
| IMPORTANT | Personal/direct or time-sensitive mail requiring attention |
| UPDATES | Legitimate useful information that does not require urgent action |
| SPAM | Unwanted/promotional/junk mail |

Use these labels consistently in APIs, feedback, storage, datasets, and checkpoint
metadata. IGNORE is a legacy label: migrate it only with documented semantics;
do not silently equate every nonurgent message with spam. Ham is not synonymous
with IMPORTANT.

ABSTAIN, UNAVAILABLE, and ERROR are processing outcomes rather than categories.
They must not become successful SPAM decisions. The structured API keeps these processing outcomes separate from categories. Vote share is not calibrated
confidence. Original predictions must remain available separately from effective
human-corrected categories and revised attempts.

## Gmail read-state and delivery policy

Default behavior preserves unread Gmail state. Automatic marking-read is an
explicit opt-in processing policy, not an incidental consequence of inference.
When enabled:

- IMPORTANT requires a valid classification and durably recorded successful
  required notification before an automatic mark-read action is attempted.
- UPDATES/SPAM require a valid classification and durably persisted processing
  result before the optional mark-read action.
- ABSTAIN/UNAVAILABLE/ERROR never automatically mark a message read.
- Failed notifications remain retryable. Disabled/missing required notification
  configuration is not successful delivery.
- A failed mark-read action is retried independently of a completed notification.
- Manual user-directed mark-read is distinct from automatic processing.

Gmail unread status must not be the sole durable queue. Attempts and completed
actions must be recorded. An ambiguous provider timeout needs an explicit delivery
state; do not promise exactly-once external delivery without provider support.
The durable worker implements these rules. Automatic marking-read defaults to OFF. Set
`MAILMIND_AUTO_MARK_READ=true` and restart both services to opt in. An uncertain alert requires a human decision; it is never silently retried.

## Account lifecycle and data retention

The application implements the local session, account separation, cancellation, disconnect, and active-record deletion rules below.

| Action | Target behavior | Data treatment |
|---|---|---|
| Logout | End local UI session, cancel/pause its processing, prevent unauthenticated access | Retain account-scoped mail/feedback unless purge is explicitly requested |
| Disconnect Google | Disable Gmail access for the account, invalidate active account work, remove local stored OAuth credential | Retain isolated local records; provider revocation must be a documented separate capability if implemented |
| Switch account | Cancel old account work, connect/authenticate the selected account, load only its records | No cross-account retrieval, examples, jobs, or notification state |
| Delete local account data | Explicitly remove that account's stored mail, feedback, vector documents, and associated queued work | Apply across both stores; preserve unrelated accounts; honor documented backup retention |

Logout and disconnect must never launch a new interactive OAuth flow from the
scheduler. Work that has already contacted an external provider cannot be undone,
but no subsequent side effects may proceed once cancellation is observed. Treat
session invalidation and provider credential revocation as different operations.

Data migrations preserve existing private records until the replacement is
verified. Do not silently delete data to repair schema/account issues. Purge is
an explicit operation, not the default behavior of a corrected logout.

## Development and interview safety

Use synthetic fixtures and temporary databases for regression tests. Do not read
live environment/token/credential files to establish baseline behavior. No test
should fetch real mail, mark it read, send a notification, or expose a service.
Model/provider integration checks require a separate deliberate validation step.

Measure dashboard statistics and label modes accurately. Offline mode must state
which assets need provisioning and which external features are disabled. Neither
agreement nor a softmax/vote value proves accuracy. Do not claim deployment,
performance, privacy guarantees, or scale beyond validated evidence.

## Dashboard behavior

The dashboard now uses account-wide saved counts, measured timing, loaded-model
readiness and observed worker heartbeat. Configured cloud providers are labelled
unverified. Pages and search use effective categories while original predictions
remain separate. Feedback can be revised or withdrawn without deleting history;
withdrawn vectors are immediately ineligible and their cleanup is retryable.
Refreshes are completion-based, cancellable and account-generation checked.

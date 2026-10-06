# MailMind operating policies

These policies define the supported behavior for the current single-user local deployment. Changes require an explicit product decision, corresponding tests, and updated public documentation.

## Supported scope

MailMind supports:

- one trusted user on one local workstation;
- one selected Google account at a time;
- sequential account switching with strict persisted-data isolation;
- SQLite as authoritative storage;
- embedded local Chroma collections as derived storage; and
- the owned native supervisor for normal operation.

Remote hosting, public network exposure, and concurrent multi-user operation are outside the supported scope.

## Connection boundary

MailMind must not access, display, classify, embed, or otherwise process account mail until Google OAuth connection succeeds.

The semantic-indexer process may start before OAuth only so the supervisor can own and monitor it. Before connection, it must not read saved mail, initialize the embedding runtime, or open the semantic search collection.

Account logout, disconnect, switching, deletion, restart, and authentication transitions must invalidate stale contexts through generation fencing.

## Categories and system outcomes

| Value | Meaning |
| --- | --- |
| IMPORTANT | Personal, direct, urgent, security-sensitive, or action-requiring mail |
| UPDATES | Legitimate useful information that does not require urgent attention |
| SPAM | Unwanted, promotional, deceptive, or junk mail |
| NEEDS_REVIEW | A system presentation state for uncertainty, incomplete processing, or failure |

Only Important, Updates, and Spam are model categories. Needs Review must never be added as a trained fourth class.

Abstain, unavailable, timeout, malformed output, and error are processing outcomes. They must not silently become Spam.

Original predictions, later provider attempts, local-shadow predictions, and effective human-corrected categories must remain distinguishable.

## Inbox intake

### Active-task cap

The default maximum is 100 active queued, running, or retry tasks. Intake must not exceed the configured cap.

### Initial historical batch

The first connected intake admits at most the first 100 older messages. Historical pagination pauses after the admitted batch.

### Live priority

New messages discovered from the Gmail history cursor receive priority over older backlog and may use newly freed queue capacity.

### User controls

- **Sync new messages** queues a durable, rate-limited live check.
- **Older mail** loads automatically, 20 emails at a time, when you page towards the end of your inbox and the previous older batch has been processed (`/ingestion/fetch-next`, ≤ 100 per request).

Sync must not authorize old backlog. Loading older mail must not disable or delay live monitoring.

## Classification

Normal-mode cloud classification is authoritative. Configured Gemini models and Groq fallback use independent bounded-call capacity and typed failure handling.

The local three-class model is a shadow evaluator in normal mode. It must not be advertised as production quality without adequate independent evidence.

Provider output must match the strict category contract. A failed or malformed provider result must enter a reviewable processing state rather than disappear.

## Feedback and retrieval

Human feedback is authoritative after it is committed to SQLite.

Derived feedback vectors may be updated asynchronously. A slow or unavailable vector store must not delay the feedback response or erase the saved correction.

Retrieval may support classification only when examples are current, account-owned, revision-valid, sufficiently near, independently supported, and dominant under the configured conservative vote. Otherwise it abstains.

Undo preserves prediction and feedback history.

## Semantic search

Lexical SQLite search is always the baseline. Semantic search may augment queries of at least three characters.

Exact lexical matches rank before semantic-only matches. Semantic failure falls back to lexical search.

The semantic index:

- is account scoped;
- is derived local state;
- uses <code>data/search_chroma_db</code>;
- is separate from feedback retrieval;
- caps derived documents at 6,000 characters;
- does not change the source email; and
- runs in an isolated process only after Google connection.

## Gmail read state and notifications

Automatic mark-as-read defaults to off.

When enabled:

- an Important message requires a valid classification and durably recorded required notification success before automatic mark-read;
- Updates and Spam require a valid saved decision;
- Needs Review and failed/unavailable outcomes are not automatically marked read;
- mark-read retries independently from classification and notification; and
- ambiguous external delivery does not trigger an unsafe automatic resend.

Telegram is optional. Missing configuration is not successful delivery.

## Account lifecycle

| Action | Processing effect | Data effect |
| --- | --- | --- |
| Stop processing | Ends the local session and pauses active work | Retains mail, feedback, and derived data; parks the OAuth credential for 24 h (silent reconnect), then deletes it |
| Disconnect Google | Fences work and prevents Gmail access | Parks the selected account’s OAuth credential unused for 24 h (silent reconnect), then deletes it; retains saved data |
| Switch Google account | Cancels old context and completes OAuth for the new selection | Loads only the selected account’s isolated records |
| Delete this account data | Pauses/fences work and runs managed purge | Removes selected account mail, histories, jobs, vectors, and credentials |
| Delete old unassigned data | No ordinary account transition | Removes only explicitly selected legacy/unassigned state |

Deleting local credentials does not revoke Google permission. Account purge does not erase backups, exports, copied files, provider requests, or delivered messages.

## Service ownership

The native supervisor owns four processes: indexer, API, worker, and frontend.

Every owned service, the API included, is restarted with backoff (1 → 60 s) and no limit. Only a startup crash loop (5 exits in a row, each within 10 s of starting) warns the user, keeps retrying for 60 s, and then shuts down the owned set (see [Self-recovery](SELF_RECOVERY.md)). Stop and fallback termination target only exact owned children.

Do not use global process-name or port-based termination.

## Dashboard truthfulness

The dashboard must distinguish:

- configured versus observed provider health;
- live sync versus historical backlog;
- classification progress versus semantic indexing;
- current failures versus historical failures;
- model agreement versus measured accuracy; and
- saved source data versus derived index state.

No invented latency, provider-online count, confidence, or success state may be displayed.

## Development and release safety

- Use synthetic fixtures and temporary storage for automated tests.
- Do not access live Gmail, provider keys, OAuth tokens, or private models during ordinary test runs.
- Do not publish real-account screenshots or logs.
- Resolve unexpected failures before describing a release as fully passing.
- Keep dependency declarations, lock files, documentation, and runtime behavior consistent.
- Do not claim production accuracy, hosted scale, or universal privacy beyond evidence.


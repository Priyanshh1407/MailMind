# MailMind security design showcase

This document explains implemented security decisions and their limits. It does not claim that MailMind is immune to attack, encrypted at rest, production-model accurate, or safe to expose as a public multi-user service.

## Security posture in one paragraph

MailMind separates the local browser, API, account lifecycle, background work, untrusted email content, external providers, and derived vector stores. Private API operations require a trusted loopback session, allowed Origin, CSRF token, and current account generation. Gmail and provider calls are bounded. Email and retrieval content remain untrusted structured data. SQLite is authoritative, while feedback and semantic-search vectors are local derived state. Logout, disconnect, account switching, deletion, and restart fence stale work. Ambiguous external sends are made visible instead of being retried blindly.

## Trust-boundary map

~~~mermaid
flowchart TB
  Browser[Local browser]
  API[FastAPI]
  Account[Session and generation guard]
  DB[(SQLite)]
  Worker[Gmail/classification worker]
  Indexer[Semantic indexer]
  Gmail[Gmail API]
  Cloud[Gemini / Groq]
  Telegram[Telegram]
  Feedback[(Feedback vectors)]
  Search[(Search vectors)]

  Browser -->|cookie + CSRF + Origin| API
  API --> Account
  API --> DB
  Worker --> Account
  Worker --> DB
  Indexer --> Account
  Indexer --> DB
  Worker -. bounded .-> Gmail
  Worker -. minimized payload .-> Cloud
  Worker -. durable outbox .-> Telegram
  Worker --> Feedback
  Indexer --> Search
~~~

## 1. Automatic local session without public authentication claims

The trusted dashboard opens a local session automatically. There is no pairing-code step.

Controls:

- loopback-only supported origin;
- random session secret represented server-side by a hash;
- HttpOnly and SameSite=Strict cookie;
- CSRF header for mutations;
- Origin and trusted-host enforcement;
- expiry and restart invalidation; and
- generation changes during account transitions.

This is appropriate to the supported trusted-workstation boundary. It is not internet-facing identity or multi-user authorization.

Relevant implementation: <code>api/app.py</code>, <code>src/account_state.py</code>, and <code>frontend/src/api.js</code>.

## 2. Account-generation fencing

Every active account context carries a generation. Account transitions increment or invalidate it. Slow work checks the context before and after external operations.

This prevents:

- late OAuth completion from restoring cancelled credentials;
- a worker from committing after logout;
- old-account responses appearing after a switch;
- stale vector work crossing accounts; and
- mark-read or notification stages continuing after cancellation.

Relevant implementation: <code>src/account_state.py</code>, <code>src/main.py</code>, and dashboard response validation.

## 3. OAuth credential isolation

MailMind never asks for a Gmail password. Google OAuth credentials are stored in account-specific local files. Credential refresh has its own bounded-call capacity.

Disconnect and Stop processing park only the selected account’s local credential: it is never used for background work, can be renewed silently by Connect Google for 24 hours (only for the same account, and only while Google still accepts it), and is then deleted. Delete account data removes it at once. Google-side revocation remains a separate operator action.

The desktop loopback flow is not presented as a hosted OAuth design.

## 4. Durable processing and recovery

SQLite records the state of email processing rather than using Gmail unread status as a queue.

Controls include:

- queued, running, retry, complete, and dead task states;
- expiring leases;
- stale-owner rejection;
- attempt caps and bounded retry delay;
- saved processing history;
- poison-message isolation; and
- explicit recovery endpoints.

A failed message does not block later messages.

## 5. Safe notification delivery

Telegram delivery is modeled as a durable outbox stage.

The application records sending intent before contacting Telegram. A timeout or crash that could have delivered becomes an explicit unknown state. MailMind does not automatically resend that state because doing so could duplicate an alert.

Users can deliberately confirm arrival or retry after reviewing the risk.

## 6. Gmail read-state safety

Automatic mark-as-read defaults to off.

When enabled, mark-read remains a separate stage. Important mail is not marked read before required notification success. Failed, unavailable, ambiguous, or review outcomes are not treated as successful processing.

## 7. Prompt and provider boundaries

Email and retrieval text are:

- normalized;
- length bounded;
- placed in a JSON data envelope;
- separated from system instructions; and
- treated as untrusted.

Provider responses must satisfy the strict three-category output contract. Unknown categories, additional commands, malformed output, or provider failure cannot silently produce Spam.

Best-effort masking and sender omission reduce external exposure, but do not anonymize mail.

## 8. Conservative feedback retrieval

Feedback retrieval does not trust Chroma results on their own.

Candidates must be:

- owned by the selected account;
- tied to the current feedback revision;
- labelled with a valid category;
- at a finite acceptable distance;
- independent from duplicate evidence; and
- sufficiently supported under the conservative vote.

If those conditions are not met, retrieval abstains.

## 9. Separate search and feedback stores

MailMind keeps two embedded stores:

| Store | Purpose |
| --- | --- |
| <code>data/chroma_db</code> | User-feedback retrieval |
| <code>data/search_chroma_db</code> | Saved-email semantic search |

No Chroma HTTP server is started or published. SQLite remains authoritative.

Search candidates are filtered back through account-owned SQLite records. Semantic search failure falls back to lexical results.

## 10. Semantic-index isolation and OAuth boundary

Semantic indexing runs in its own process so native embedding or Chroma startup cannot block Gmail polling.

Before Google connection, the process waits without reading saved mail or initializing semantic assets. After connection, it works in generation-fenced batches of ten and pauses across account/authentication/deletion transitions.

## 11. Data deletion boundaries

Account deletion targets the selected account’s managed mail, histories, tasks, feedback, vectors, credentials, and related runtime state while preserving other accounts.

Deletion does not promise forensic erasure and cannot remove backups, snapshots, copied exports, delivered Telegram messages, or earlier provider requests.

## 12. Local-only routing

Local-only mode blocks Gmail, cloud-provider, and Telegram application routes. It requires pre-provisioned verified assets and has no cloud fallback.

The boundary is enforced by application routing. It is not a replacement for an operating-system firewall or network policy.

## 13. Exact service ownership

The supervisor records and controls exact child processes. It checks port availability but does not terminate existing listeners. Stop is authenticated over loopback and signals only owned children.

Every owned service is restarted with backoff and no limit; only a startup crash loop warns, retries for 60 s, then shuts the set down. Its status file contains no secrets (see [Self-recovery](SELF_RECOVERY.md)).

## 14. Safe diagnostics

Runtime diagnostics expose counts, timestamps, safe state codes, provider-configuration presence, and semantic-index progress. They do not print Gmail content, account identifiers, OAuth material, or provider keys.

Application logs record allowlisted event types and exception classes rather than raw sensitive exception messages.

## 15. Honest limits

MailMind deliberately does not claim:

- encrypted application storage;
- perfect masking;
- prompt-injection immunity;
- exact-once Telegram delivery;
- hosted multi-user security;
- provider-side deletion;
- production classifier accuracy;
- native/container semantic-index parity; or
- protection from a compromised local operating system.

The security value of the project is not the absence of failure. It is that failures, uncertainty, ownership, and external effects are represented explicitly and conservatively.


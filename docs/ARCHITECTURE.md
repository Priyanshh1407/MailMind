# MailMind architecture

## Scope

MailMind is a local desktop application for one trusted user and one selected Gmail account at a time. Multiple accounts may be connected sequentially, but their mail, feedback, jobs, vectors, credentials, and runtime state remain isolated.

The supported native runtime binds the API and dashboard to loopback. It is not a public or multi-tenant service.

## Process model

```mermaid
flowchart LR
  S[Foreground supervisor]
  S --> I[Semantic indexer]
  S --> A[FastAPI]
  S --> W[Gmail/classification worker]
  S --> F[Built React frontend]

  A --> DB[(SQLite)]
  W --> DB
  I --> DB

  A --> SC[(Search Chroma)]
  I --> SC
  W --> FC[(Feedback Chroma)]

  W --> G[Gmail]
  W --> C[Gemini / Groq]
  W --> T[Telegram]
```

### Supervisor

`scripts.launch` owns exact child-process objects and a private state file under `.run`. It:

- refuses startup if ports 8000 or 5173 are already occupied;
- starts the semantic indexer first and waits for its readiness marker;
- starts the API and waits for its health response;
- starts the worker and built frontend;
- monitors every owned child;
- restarts the indexer, worker, or frontend within a bounded recent-window budget;
- shuts down the owned set if the API exits or a restart budget is exhausted;
- accepts authenticated loopback stop requests; and
- never kills unrelated processes by name or port.

### Semantic indexer

The indexer is isolated so embedding or Chroma work cannot block Gmail polling.

Before Google is connected, the process only announces that it is waiting and ready. It does not read saved mail, initialize the embedding runtime, or open the search collection. After connection, it obtains a generation-fenced account context and reconciles ten rows per batch.

Search documents contain normalized sender, subject, and searchable body text, capped at 6,000 characters. The original stored email is unchanged.

### API

FastAPI owns:

- trusted-loopback session creation;
- HttpOnly session cookies and CSRF enforcement;
- account lifecycle operations;
- dashboard status and telemetry;
- paginated email reads;
- lexical and hybrid search;
- feedback, undo, and history;
- Action Center reads/mutations, token analytics, and source-labelled explanations;
- forced live sync and bounded backlog authorization; and
- explicit bounded saved-mail intelligence backfill; and
- explicit recovery for unfinished tasks and ambiguous notification delivery.

The API opens/migrates schema version 14 and lazily initializes heavy or optional services.

### Gmail/classification worker

The worker runs small, newest-first cycles. Each cycle:

1. validates the active account generation;
2. acquires an expiring lease;
3. prioritizes live history-cursor changes;
4. fills remaining capacity with ordinary historical backlog before intelligence backfill;
5. processes a bounded number of durable tasks;
6. records classification, notification, and mark-read stages; and
7. updates heartbeat, success, and safe error state.

Cloud providers, Gmail, Telegram, retrieval, local inference, credential refresh, and search use isolated bounded-call capacity where applicable.

### Frontend

The production launcher serves `frontend/dist` through the owned Node server. React never receives Gmail OAuth credentials or provider keys. It polls coherent account snapshots, aborts stale requests, rejects mixed-account responses, and disables mutations when the last snapshot is stale.

## Persistence

### SQLite: authoritative state

SQLite stores:

- account/session generation state;
- emails and source text;
- ingestion cursors and historical backlog state;
- processing tasks and attempts;
- classification and local-shadow results;
- notification outbox state;
- feedback revisions;
- semantic-index reconciliation state;
- worker health; and
- source-labelled analysis, extracted actions, reminders, privacy-safe token events, mutation limits, and intelligence-backfill state; and
- background authentication and processing jobs.

Task states include `queued`, `running`, `retry`, `complete`, and `dead`. A successful category is never invented from a processing failure.

### Chroma: derived state

MailMind uses embedded Chroma only:

| Directory | Purpose |
| --- | --- |
| `data/chroma_db` | Current account-owned feedback examples for conservative retrieval |
| `data/search_chroma_db` | Saved-email embeddings for local semantic search |

SQLite remains the source of truth. Vector writes are account-namespaced, revision-checked where relevant, and retryable. The application does not run or expose a Chroma HTTP server.

## Intake model

### Live mail

A saved Gmail history cursor supports partial synchronization. Live changes are checked automatically and can be forced with **Sync new messages**. The forced action is durable and rate-limited.

### Historical backlog

Older mail is deliberately bounded:

- the initial connection admits at most 100 items;
- older pages pause after the admitted batch;
- **Fetch next 100** authorizes another historical batch only when allowed; and
- historical work cannot consume capacity reserved by the global active-task cap.

### Saved-mail intelligence backfill

Backfill is never automatic. A connected user must select **Analyze up to 20 saved emails**, and Action Center extraction must already be enabled. Admission is account-scoped, idempotent, limited to 100 per API request and 20 by the current UI, and constrained by the same active-task cap as ordinary work.

Backfill reuses the existing processing queue and provider/local instrumentation. It can persist classification, source-labelled analysis, action candidates, and token events. It suppresses historical Telegram alerts, Gmail mark-read calls, and automatic reminder creation. Its durable states are queued, running, retry, complete, and dead; an expired worker claim returns it to retry.

The private, CSRF-protected `POST /intelligence/backfill` route accepts `{"limit": 1..100}` and returns a durable job acknowledgement plus requested, admitted, eligible, and remaining-capacity counts. It returns conflict when no eligible mail or capacity remains. `GET /status` exposes rollout availability and account-scoped backfill counts. The existing task-retry route resets a failed backfill marker with the task; completed rows remain idempotently excluded.

### Priority

New live mail is admitted and processed before older backlog, and ordinary backlog is processed before intelligence backfill. The default active-task cap is 100. The worker time-slices processing to keep Gmail discovery responsive.

## Intelligence data flow

Validated provider output can add a bounded explanation and action candidates to the authoritative classification. Explanations remain source-labelled and do not expose hidden reasoning. Action candidates pass strict type, confidence, evidence, date, and lifecycle checks before becoming account-owned Action Center rows.

Token events contain counts and operational metadata, never email text or provider responses. Provider-billed and locally processed totals stay separate. Day, week, and month aggregation uses configured local-calendar boundaries.

## Classification model

```mermaid
flowchart TD
  E[Saved email] --> R[Conservative feedback retrieval]
  R --> P[Cloud provider policy]
  P --> G1[Gemini primary]
  G1 -->|retryable failure| G2[Gemini fallback]
  G2 -->|retryable failure| Q[Groq]
  P --> V[Strict category validation]
  E --> L[Local shadow classifier]
  V --> D[Authoritative decision]
  L --> H[Shadow comparison]
  D --> S[(SQLite)]
  H --> S
```

Only `IMPORTANT`, `UPDATES`, and `SPAM` are model categories. `NEEDS_REVIEW` is a system presentation state for unavailable, ambiguous, or failed processing.

Feedback retrieval may support a decision only when current account-owned examples are close enough, revision-valid, independently supported, and sufficiently dominant. Otherwise it abstains.

## Notification and read-state safety

Telegram delivery is recorded through a durable outbox. A request that might have reached Telegram but lacks a safe acknowledgement becomes `unknown`; it is not automatically resent.

Automatic mark-as-read defaults to off. When enabled, it remains a separate durable stage. Important mail is not automatically marked read before required notification success.

## Account transitions

Logout, disconnect, account switching, deletion, and restart advance or invalidate account/session generations. Slow operations check their original context before and after external work. A late response cannot commit into a new account generation.

The semantic indexer follows the same fence and pauses during authentication or purge transitions.

## Search ranking

Lexical SQL search is parameterized and treats wildcard characters literally. For queries of at least three characters, semantic candidates may be added. Hybrid ranking keeps exact lexical matches first and adds related semantic matches afterward.

If embedding or Chroma query work fails, the API logs a safe event and returns lexical results.

## Failure isolation

- Semantic indexing cannot block Gmail because it runs in another process.
- Feedback persistence does not wait for vector indexing.
- Provider timeouts do not consume unrelated Gmail or Telegram capacity.
- A poisoned message does not stop the batch.
- An expired lease prevents a stale worker from committing.
- An interrupted intelligence backfill is recovered through the same lease/retry path without repeating historical side effects.
- Unknown notification delivery requires explicit recovery.
- Historical transient runtime errors are cleared after restart or a successful worker cycle.

## Container boundary

The native supervisor owns four services. The current Compose demonstration owns API, worker, and frontend only and runs in local-only mode. It does not currently include the semantic indexer; therefore the container documentation does not claim complete semantic search indexing.


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
- restarts any owned service that exits, the API included, with backoff (1, 2, 4, 8, 16, 32, then 60 s) and no restart limit;
- treats only a startup crash loop (5 exits in a row, each within 10 s of starting) as unrecoverable: it warns in the terminal and, through `.run/supervisor-status.json` served by the frontend server as `/mailmind-supervisor.json`, in the dashboard, keeps retrying for 60 s, and shuts the owned set down only if the service never stays up. The status file is rewritten only when it changes, and a rename blocked by Windows ("Access is denied" while the dashboard server or antivirus has it open) is retried briefly, then skipped; it can never stop MailMind. Credential saves use the same retrying rename (`src/file_utils.py`), but still raise if the file stays locked;
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

The API opens/migrates schema version 20 and lazily initializes heavy or optional services.

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

The cursor only advances after the messages it covers have been admitted. A full batch stops paging (the rest is read from the same cursor next cycle), and a failed history call keeps the old cursor.

Two backstops make sure mail is never lost silently:

- a message whose download failed is fetched again by ID when its retry is due. Temporary failures (network, Gmail 5xx/429) retry until Gmail answers; a deleted or unparseable message is quarantined after the attempt limit;
- every five minutes the worker lists INBOX messages from the last two days and fetches any it has not saved, within the normal queue capacity.

### Historical backlog

Older mail is deliberately bounded:

- the initial connection admits at most 100 items;
- older pages pause after the admitted batch;
- after that, the dashboard authorizes the next 20 automatically when the user reaches the last two pages of the unfiltered inbox, and only after the previous older batch has been processed (`POST /ingestion/fetch-next`, limit 1–100, default 20); and
- historical work cannot consume capacity reserved by the global active-task cap.

### Saved-mail intelligence backfill

Backfill is never automatic. A connected user must select **Analyze up to 20 saved emails**, and Action Center extraction must already be enabled. Admission is account-scoped, idempotent, limited to 100 per API request and 20 by the current UI, and constrained by the same active-task cap as ordinary work.

Backfill reuses the existing processing queue and provider/local instrumentation. It can persist classification, source-labelled analysis, action candidates, and token events. It suppresses historical Telegram alerts, Gmail mark-read calls, and automatic reminder creation. Its durable states are queued, running, retry, complete, and dead; an expired worker claim returns it to retry.

The private, CSRF-protected `POST /intelligence/backfill` route accepts `{"limit": 1..100}` and returns a durable job acknowledgement plus requested, admitted, eligible, and remaining-capacity counts. It returns conflict when no eligible mail or capacity remains. `GET /status` exposes rollout availability and account-scoped backfill counts. The existing task-retry route resets a failed backfill marker with the task; completed rows remain idempotently excluded.

### Priority

New live mail is admitted and processed before older backlog, and ordinary backlog is processed before intelligence backfill. The default active-task cap is 100. The worker time-slices processing to keep Gmail discovery responsive.

### Email security signals

Every Gmail message is saved with bounded security evidence in `email_signals` (`src/email_signals.py`), written in the same transaction as the email:

- the SPF, DKIM and DMARC verdicts Gmail recorded in `Authentication-Results`. Gmail's own header (authserv-id `google.com`) is trusted over any earlier hop, which a sender could forge;
- the From, Reply-To and Return-Path domains, `List-Unsubscribe` and one-click unsubscribe support, and the Gmail thread ID;
- up to 50 links, each as visible text plus its real target domain, from both HTML anchors and bare URLs;
- up to 1,000 characters of text a reader cannot see (`display:none`, `visibility:hidden`, zero font size or opacity, the `hidden` attribute).

The visible body text is unchanged, so grounding, providers and the evaluation recordings are unaffected. A malformed header never fails the message.

Mail saved before signals existed is filled in by an explicit backfill. `POST /signals/backfill` (`{"limit": 1..1000}`) admits saved Gmail messages without signals into `signals_backfill_items`. Each worker cycle then re-reads up to ten due messages and saves **only** their signals: no task, alert, label or read state changes. Temporary download failures retry with the usual backoff, deleted messages stop after the attempt limit, and an expired worker claim returns a row to retry. `GET /status` reports `signals` counts.

### Scam and phishing checks

With `MAILMIND_RISK_ANALYSIS_ENABLED=true`, every email gets a report in `email_risk` (`src/risk_analysis.py`, `src/email_risk.py`). The checks are local rules; no provider or network call is made, so they also run in local-only mode.

| Group | Rules |
| --- | --- |
| Sender authentication | DMARC, SPF or DKIM failed; no result at all |
| Reply paths | Replies go to a personal mailbox or another domain; a brand name on a personal address |
| Lookalikes | The sender or a link imitates a brand or one of your trusted domains: the brand as a word (`sbi-kyc.in`), a visual confusable (`micros0ft`), or one typo (`hdfcbamk`); punycode |
| Links | Visible text names another domain than the target; shorteners; bare IP addresses |
| Lures | Requests to share an OTP/PIN/password (not warnings against sharing, nor "the password to open this statement is..."), UPI "receive money" tricks, KYC/PAN pressure, account-block threats, prizes, fees to release something, gift cards, credential checks |
| Pressure and hiding | Deadlines within hours; text a reader cannot see |

Each finding quotes its evidence. The score is a logistic model over the findings with weights fitted on the CC0 benchmark and constrained to be non-negative, so adding a red flag can never lower a score (`src/risk_weights.json`; see [the evaluation](RISK_EVALUATION.md)). Bands: high >= 70, medium >= 40. Trusted domains (never lookalikes) are ones you labelled Important or Updates, or that sent you at least two such emails and no spam.

An email is scored inside its classification transaction, behind a savepoint so a scoring failure never undoes the classification. Each worker cycle then scores up to 50 more: unscored mail, mail scored by older rules, and mail whose signals arrived after it was scored (`content_only` becomes `full`). A high-risk Important alert is sent as "⚠ Possible scam: ..." The score never hides, moves or deletes mail.

Routes: email rows carry `risk`; `GET /emails/{id}/risk` returns one report; `POST /emails/{id}/risk/review` marks it reviewed; `GET /status` reports `risk` counts.

### Money and subscriptions

With `MAILMIND_LEDGER_ENABLED=true`, each worker cycle scans up to 20 saved Important/Updates emails (newest first, Spam skipped) for money records (`src/ledger.py`, `src/ledger_store.py`):

1. A local keyword gate skips mail that records no transaction (adverts with prices, offers, newsletters) without any provider call.
2. Every amount is found in the original text and replaced by a numbered placeholder kept in a local map.
3. If a provider is configured, at most one extraction runs per cycle and at most one every 10 seconds, through the assistant call slots (`call_structured` in `src/llm_api.py`: same failover rules as classification, separate capacity so it never slows live mail, its own request builder so classification requests and their recordings are unchanged). The provider answers which placeholder is the total, the kind (purchase, subscription, refund, bill, debit, or none), the merchant and any stated renewal; the answer is validated, the merchant and evidence must be quoted from the email, and the real amount is filled in locally.
4. A temporary provider failure retries with backoff; after the attempt limit, on a permanent failure, or in local-only mode, local rules choose the total (the amount on the strongest cue's line: "order total", "debited", "amount paid" beat a bare "total"; subtotals, savings, MRP, balances and cashback never count).

`ledger_entries` holds one row per email; `ledger_scans` records what was scanned and with which extraction version. A correction you make in the Money tab sets `user_corrected` and is never overwritten by a rescan. Subscriptions are derived at read time: a stated subscription, or two or more charges from one merchant about a month (±5 days) or a year (±10 days) apart; overlapping categories (for example two streaming services) are reported as insights.

Migration 17 also rebuilds two CHECK constraints in place (`rebuild_check`, from each table's own stored definition) so token events and rate limits accept the assistant features' operations and scopes.

Routes: `GET /ledger/summary?month=YYYY-MM`, `GET /ledger/entries`, `PATCH /ledger/entries/{id}` (revision-checked), `GET /ledger/subscriptions`.

### Ask your inbox

With `MAILMIND_INBOX_QUESTIONS_ENABLED=true` (and a provider configured), `POST /questions` starts a background job (its own runner, so a slow answer never delays Google sign-in) and `GET /questions/{id}` returns its status and a live trace. One question runs at a time per account, rate-limited through `intelligence_mutation_limits` (`question` scope); a row left running by a restart is closed as interrupted.

The agent (`src/inbox_agent.py`) works in at most 6 steps within 45 seconds. Each step is one strict JSON object through `call_structured`: a tool call or the final answer. The tools only read, all through fenced account-scoped reads:

| Tool | Returns to the model |
| --- | --- |
| `search_mail` | Up to 8 matches from the existing lexical + semantic search: email ID, local date, sender domain, masked subject. The search itself runs locally over the full sender, subject and body |
| `read_email` | The same minimised text classification sends, capped at 4,000 characters, with each amount replaced by a session token (`{{amount:3}}`) the model can copy into an answer; at most 10 emails per question |
| `list_actions` | Action Center tasks with their due dates |
| `ledger_summary` | Spending and entries as `{{ledger:...}}` tokens, never numbers |
| `risk_report` | An email's scam band and warning-sign names |

The answer is verified, not trusted: a citation counts only if its email was opened with `read_email` in that session and its quote passes `quote_in_source` against that email's text. A sentence without a verified citation is removed (and counted in the trace). Amount and ledger tokens issued in the session are filled in locally (quotes show the amount as the email wrote it, so the source pop-up can highlight it); invented tokens become "[amount unavailable]". Status is `answered`, `unanswerable` (the model abstained), `unverified` (nothing survived verification), `failed` or `cancelled`. The stored trace has tool names, search words, counts and timings, never email text.

### Follow-ups

With `MAILMIND_FOLLOW_UPS_ENABLED=true`, the Follow-ups tab drafts a reply for a sent email; the user copies it into Gmail. MailMind has no send or draft code path, and a test fails if any Gmail method other than the reads below is touched.

- `POST /follow-ups/sent/refresh` (rate-limited) reads one page of `SENT` (last 90 days) with `format='metadata'` into `sent_messages`, and checks up to 10 threads' latest sender to mark them waiting or replied. `GET /follow-ups/sent` serves the cache with local search.
- `POST /follow-ups/drafts` starts a job: read the whole thread (`threads.get`), mask it (`src/follow_ups.py`: amounts, addresses, links and long numbers become numbered placeholders; the header names and the name the user addressed become `{{recipient_name}}` / `{{my_name}}`), learn or reuse the user's style (greeting, sign-off, signature, length, spelling from their recent sent mail, cached for a week), and call `call_structured` with its own schema (greeting, 1-3 paragraphs, closing, references).
- `check_draft` then rejects stock AI phrases, dash chains, exclamation marks, emojis, lists, rhetorical triples, wrong length, a greeting or sign-off other than the user's, references that do not quote the thread, and any number, date or weekday the thread never mentions. A failing draft gets one revision with the exact problems; whatever still fails is returned as "Check before sending" notes. The draft is assembled and unmasked locally.
- The recipient is addressed exactly as the user addressed them in their own last message ("Dear Ms. Menon", "Dear Sir/Madam", "Hi Neha"); team mailboxes never become "Dear Sales".

### Daily brief (chief of staff)

With `MAILMIND_DAILY_BRIEF_ENABLED=true`, `GET /briefs/today` writes today's brief in the background the first time the dashboard asks after `MAILMIND_DAILY_BRIEF_TIME` (default 08:30 local), once per day; `POST /briefs` rewrites it on demand (rate-limited). It runs in the API on its own runner, never in the worker, so it cannot delay Gmail polling.

`src/chief_of_staff.py`:

1. **Candidates, locally**: the last 24 hours of Important/Updates mail, open tasks due within 7 days (or overdue), unreviewed high scam scores, renewals within 7 days, and sent mail waiting 3+ days for a reply.
2. **Triage agent**: sees titles only and ranks at most 8; refs it invents are ignored.
3. **Research agents**: one per chosen item (at most 6, inside a 90-second budget), each reading only that email's minimised text (amounts as tokens) and returning at most 2 facts; a fact is kept only if its quote passes `quote_in_source`.
4. **Writer agent**: composes sections citing fact IDs only; a sentence with an unknown ID is removed. Proposals (`complete_action`, `snooze_action`, `draft_follow_up`, `mark_risky_reviewed`) are kept only for items the writer was given, of the matching kind.

Approving a proposal (`POST /briefs/{id}/proposals/{n}/approve`) runs the existing revision-checked Action Center transition or scam review; `draft_follow_up` opens the Follow-ups tab on that email. Nothing runs without approval. If any agent's provider call fails, a deterministic fallback brief (titles and counts from SQLite) is stored instead, so the brief always arrives. An optional Telegram note (`MAILMIND_BRIEF_TELEGRAM_ENABLED`) carries counts only.

### Prompt-injection defenses and the red-team

Two layers defend against instructions planted in email. The model is told that email text is untrusted data (`data_trust` envelopes and system instructions); that needs a live model to measure. MailMind's own deterministic layers then check whatever the model returns, and `scripts/redteam.py` measures them assuming the model is **fully hijacked** ([report](REDTEAM.md)):

- **Hidden-text stripping**: text styled to be invisible is not saved as body text, so no provider reads it; the scam check keeps it as a signal (`src/email_text.py`, `src/mime_parser.py`). Mail saved before this change keeps its old body.
- **Invisible-character removal** (`normalize_text` drops format characters such as zero-width spaces) before every check.
- **Homoglyph folding** of Cyrillic and Greek lookalike letters in domain checks, and a flag on any non-ASCII domain (`src/risk_analysis.py`).
- **Grounding**: answers, brief facts and action evidence must quote an email the model actually opened.
- **Token allowlists**: only amount and ledger tokens issued in the session are filled in.
- **Budgets**: step, read and time limits per question and brief.
- **Typed proposals** checked against the items given and their kind.
- **Follow-up checks**: numbers, dates and weekdays must come from the thread; links and claims of having acted (paid, transferred, attached...) must come from the user's own messages.

## Intelligence data flow

Validated provider output can add a bounded explanation and action candidates to the authoritative classification. Explanations remain source-labelled and do not expose hidden reasoning. Action candidates pass strict type, confidence, evidence, date, and lifecycle checks before becoming account-owned Action Center rows.

**Deadline time zone (TZ-01).**
- The model sees `email.received_at` in the configured zone (`MAILMIND_DEFAULT_TIMEZONE`, default Asia/Kolkata, e.g. `…T17:00:00+05:30`), and is told that a time written without a zone ("5 pm") is local and must carry the same offset.
- `_parse_due_at` backs this up in code: a deadline with no offset is local, and an exact-time deadline marked UTC is treated as local unless the email itself mentions UTC/GMT or a +00:00 offset. A zone the email states (e.g. EDT) is kept, and a bare date can't claim to be an exact time.
- Before this fix the model saw the received time in UTC and returned "5 pm" as 17:00Z, which the browser showed as 22:30 in India. The 14 affected stored deadlines were repaired once (backup first, fingerprints recomputed, one pending reminder shifted).

Token events contain counts and operational metadata, never email text or provider responses. Provider-billed and locally processed totals stay separate. Day, week, and month aggregation uses configured local-calendar boundaries.

## Classification model

```mermaid
flowchart TD
  E[Saved email] --> R[Conservative feedback retrieval]
  R --> P[Cloud provider policy]
  P --> G1[Gemini primary]
  G1 -->|route failed| G2[Gemini fallback]
  G2 -->|route failed| Q[Groq]
  G1 -->|key rejected 401/403| Q
  P --> V[Strict category validation]
  E --> L[Local shadow classifier]
  V --> D[Authoritative decision]
  L --> H[Shadow comparison]
  D --> S[(SQLite)]
  H --> S
```

Failover classifies each failure by what it proves (every case is tested in [the resilience matrix](RESILIENCE.md)):

- temporary (timeout, 429, 5xx): cool the model down, try the next route;
- broken model (400, 404, unknown error): cool it down, try the next route;
- rejected Gemini key (401/403): skip the other Gemini models, go to Groq;
- invalid answer: try the next route without a cooldown.

An email fails only after every route is tried. It stays retryable if any route failed temporarily; otherwise it goes to Needs Review. Gemini 3+ models are sent `thinking_level: low`; older models, which reject it, are not.

All routing rules live in `src/classification_service.py`, shared by the worker and the API:

- normal mode: the cloud decides; the local model runs only as an optional shadow (off by default, `MAILMIND_SHADOW_MODEL_ENABLED`), including on re-analysis. When it is off, no model process starts and no shadow is recorded;
- local-only mode: the local model decides, with the same sender-aware input it was trained on.

Grounding uses one rule, `quote_in_source()` in `src/email_text.py`, for both explanation signals and action evidence. It checks against the same privacy-minimized text the provider saw, ignores differences in quote marks, dashes, ellipses and whitespace, and requires the words to match exactly.

Only `IMPORTANT`, `UPDATES`, and `SPAM` are model categories. `NEEDS_REVIEW` is a system presentation state for unavailable, ambiguous, or failed processing.

Feedback retrieval may support a decision only when current account-owned examples are close enough, revision-valid, independently supported, and sufficiently dominant. Otherwise it abstains.

**Correction rule (cloud mode).** `classify_email` retrieves up to 8 of the account's corrections within cosine distance 0.40, validated by `CurrentFeedback` (current label and revision in SQLite). `match_correction` (`src/feedback_rules.py`) picks the nearest correction that is either from the same sender address within 0.40 or a near-copy within 0.20. If one matches, the final category is the user's label: the AI's actions are kept, and the explanation becomes "Matches your earlier correction" with the `feedback_precedent` signal. The AI's own examples are unchanged (3 nearest within 0.25, only when 3 exist). Without the SQLite-backed hook, retrieved text can never force a label. Before classifying a cycle's first email, the worker opens the feedback index (bounded, for this data folder only). Before this, it opened only when a new correction needed saving, so after a restart every lookup failed with `retrieval_not_initialized` and corrections were silently ignored (FB-02). The thresholds come from a leave-one-out evaluation on the owner's 83 labelled emails: 55 covered, 52 matching the owner's label (95%); 0.45 dropped to 90%.

## Notification and read-state safety

Telegram delivery is recorded through a durable outbox. A request that might have reached Telegram but lacks a safe acknowledgement becomes `unknown`; it is not automatically resent. The worker waits longer than the request's own connect and read timeouts, so a slow but successful send is recorded as sent. The worker also marks the moment a send is handed to Telegram: a failure before that point sent nothing and is simply retried.

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
- A malformed (non-numeric) retry time cannot strand a task: each worker cycle repairs it, because SQLite ranks text above every number.
- Temporary failures never give up. Classification (AI outage, quota, timeout), Telegram alerts, reminders and Gmail downloads keep retrying with backoff capped at 5 minutes; the search index retries on the same schedule, and feedback indexing gets a few quick tries, then retries every 5 minutes. Permanent failures stop at once with the reason: a rejected or invalid request, invalid model output, unconfigured Telegram, or an ambiguous send that might already have been delivered (never resent automatically).
- Disconnect and Stop processing park the OAuth credential (`<digest>.parked.json`, timestamped) instead of using or deleting it. Background work only reads the live credential, so a parked one is never used. Connect Google while disconnected first tries `renew_saved_login`: it refreshes the parked (or paused) credential with a bounded timeout and checks the account matches. On success the account reconnects with no browser flow; otherwise the normal OAuth page opens. Parked credentials older than 24 h are deleted on restart and before each reconnect, and Delete account data removes them at once. Switching accounts while connected never uses the silent path.
- An API restart keeps Google connected when the saved login file still exists and no deletion is pending. It still revokes browser sessions and bumps the generation, so work from before the restart is fenced out.
- Losing the internet does not sign the user out. The per-cycle Gmail check separates a proven login problem (rejected refresh, 401/403, wrong account), which pauses the account, from network trouble or a Gmail outage, which keeps it connected. The cycle is skipped, so emails don't spend their retries while offline, and the worker backs off exponentially: 5, 10, 20, 40, then every 60 s. `/status` reports the failed-check streak and the seconds until the next check, and the dashboard shows Gmail-style connectivity alerts while keeping the saved mail on screen.
- An interrupted intelligence backfill is recovered through the same lease/retry path without repeating historical side effects.
- Unknown notification delivery requires explicit recovery.
- Historical transient runtime errors are cleared after restart or a successful worker cycle.

## Container boundary

The native supervisor owns four services. The current Compose demonstration owns API, worker, and frontend only and runs in local-only mode. It does not currently include the semantic indexer; therefore the container documentation does not claim complete semantic search indexing.


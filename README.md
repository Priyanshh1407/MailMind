# MailMind

MailMind is a private, local-first Gmail intelligence layer for one trusted user on one workstation. It connects through Google OAuth, discovers new inbox mail, stores durable processing work in SQLite, classifies messages as **Important**, **Updates**, or **Spam**, and exposes uncertain or failed decisions as **Needs Review**.

The React dashboard combines live Gmail intake, recoverable background processing, user feedback, provider health, and hybrid lexical/semantic search. MailMind complements Gmail; it is not intended to replace a full email client or operate as a public multi-user service.

> [!IMPORTANT]
> Mail access and derived processing begin only after a successful Google connection. The semantic-indexer process may be running beforehand, but it waits without reading saved mail, loading the embedding runtime, or opening the semantic search store.

## Highlights

- Google OAuth with automatic completion-page close and safe dashboard fallback.
- Strict account isolation with generation fencing during logout, disconnect, deletion, and account switching.
- Durable SQLite jobs with bounded retries, expiring worker leases, and auditable processing history.
- Three-category classification through Gemini and Groq fallback, plus an optional local three-class shadow model (off by default).
- `NEEDS_REVIEW` as a system outcome—not a fourth model-generated category.
- Conservative feedback retrieval that abstains unless current, account-owned examples provide sufficient independent support.
- Live hybrid search across sender, subject, and body, with exact lexical matches ranked before semantic matches.
- Separate derived Chroma collections for feedback retrieval and inbox search.
- A responsive dark dashboard with health, intake, progress, search, review, feedback, and recovery controls.
- Source-labelled classification explanations, an account-scoped Action Center, and Recharts token-usage trends.
- Explicit saved-mail intelligence backfill in batches of at most 20, with live mail kept ahead of backfill work.
- Optional Telegram alerts and opt-in automatic Gmail mark-as-read behavior.
- Local-only mode with verified pre-provisioned assets and no cloud/Gmail/Telegram routing.

Added in October 2026 (scam checks, Money, Ask, Follow-ups and the brief each have their own flag, off by default; signals are always recorded):

- Security signals saved with every email: Gmail's SPF/DKIM/DMARC verdicts, reply paths, unsubscribe headers, links (visible text and real target) and hidden text, with an explicit backfill for older mail.
- Local scam and phishing checks: lookalike domains, misleading links, failed sender authentication and Indian payment lures (OTP, UPI, KYC), each with quoted evidence and a reproducible evaluation.
- A Money tab with spending, refunds, bills and subscriptions from receipts; every amount is replaced by a numbered placeholder before any provider call, so the AI picks the total without ever seeing a number.
- An Ask tab: questions about your saved mail answered by a read-only agent, where every sentence must cite a quote verified in an email it read; unverifiable claims are removed.
- A Follow-ups tab: pick a sent email and get a formal follow-up in your own greeting and sign-off, checked for AI clichés and for any fact the thread never mentions, to copy into Gmail. MailMind never sends mail; names, addresses and amounts reach the provider only as placeholders.
- A daily brief from three bounded agents (triage, research with verified quotes, writer citing only verified facts), with suggested actions that run only when you approve them, and a plain fallback when providers are down.
- A red-team harness that assumes the AI model is fully hijacked and measures what MailMind's own checks still stop: 27 of 30 attacks cause harm with the checks off, 1 with them on.

## Product workflow

```mermaid
flowchart LR
  A[Connect Google] --> B[Discover inbox changes]
  B --> C[Save durable tasks]
  C --> D[Classify newest mail first]
  D --> E{Result}
  E -->|Important / Updates / Spam| F[Persist decision]
  E -->|Unavailable or uncertain| G[Needs Review]
  F --> H[Optional Telegram alert]
  F --> I[Queue local semantic indexing]
  I --> J[Hybrid dashboard search]
  F --> K[User confirmation or correction]
  K --> L[Durable feedback + derived RAG index]
```

## Runtime architecture

The native supervisor owns four child processes:

1. **Semantic indexer** — waits for Google connection, then drains durable search-index rows in batches of ten.
2. **API** — serves the local session, account lifecycle, dashboard data, search, feedback, and recovery endpoints, and runs the on-demand assistant jobs (Ask questions, follow-up drafts, the daily brief) on their own bounded runners.
3. **Gmail/classification worker** — discovers live mail, admits bounded historical work, classifies messages, and handles notification/read-state stages.
4. **Frontend** — serves the built React application at `127.0.0.1:5173`.

```mermaid
flowchart TB
  Browser[React dashboard<br/>127.0.0.1:5173]
  API[FastAPI<br/>127.0.0.1:8000]
  DB[(SQLite<br/>authoritative state)]
  Worker[Gmail and classification worker]
  Indexer[Isolated semantic indexer]
  Feedback[(data/chroma_db<br/>feedback RAG)]
  Search[(data/search_chroma_db<br/>email search)]
  Gmail[Gmail API]
  Models[Gemini / Groq]
  Local[Local shadow classifier]
  Telegram[Telegram]

  Browser -->|HttpOnly session + CSRF| API
  API --> DB
  API --> Search
  API --> Local
  API -->|Ask, follow-ups, brief| Models
  API -->|sent mail and threads, read-only| Gmail
  Worker --> DB
  Worker --> Gmail
  Worker --> Models
  Worker --> Local
  Worker --> Telegram
  Worker --> Feedback
  Indexer --> DB
  Indexer --> Search
```

SQLite is authoritative. Both Chroma stores are local, embedded, account-scoped derived data that can be rebuilt or reconciled. Semantic indexing is isolated from the Gmail worker so ONNX or Chroma startup cannot block live inbox discovery.

Read [Current architecture](docs/ARCHITECTURE.md) for component boundaries and failure behavior.

## Gmail intake and queue policy

MailMind deliberately separates three kinds of work:

| Workflow | Purpose | User control |
| --- | --- | --- |
| Live sync | Discover messages arriving after the saved Gmail history cursor | **Sync new messages** forces a rate-limited live check |
| Historical backlog | Admit older inbox mail in small batches | Automatic: the first sign-in admits 100; after that, the next 20 load as you page towards the end of your inbox |
| Intelligence backfill | Add explanations/actions to already saved eligible mail | **Analyze up to 20 saved emails** is explicit and available only after Action Center extraction is enabled |
| Semantic indexing | Build local derived search embeddings for saved mail | Runs automatically after Google is connected |

Key rules:

- At most 100 processing tasks may be active by default.
- The first connection admits at most the first 100 older messages.
- New live mail uses freed capacity and is processed before old backlog.
- **Sync new messages** never authorizes another historical page.
- Older mail loads automatically in batches of 20 while you browse the end of the unfiltered inbox; a search or filter never triggers it.
- The Gmail worker processes small, newest-first slices to preserve responsiveness.
- Intelligence backfill reuses the durable queue, is capacity-limited, and never sends historical alerts, marks messages read, or creates automatic reminders.
- Semantic documents are capped at 6,000 characters; the stored source email is not shortened by that search-specific limit.

## Classification and feedback

Normal mode uses cloud classification as the authoritative decision. Provider attempts are isolated and bounded:

1. Configured Gemini primary model
2. Configured Gemini fallback model
3. Configured Groq model

A failed route never ends the chain by itself. Each failure is classified by what it proves:

| Failure | Meaning | What happens |
| --- | --- | --- |
| Timeout, 429, 5xx | Temporary | Cool the model down; try the next route |
| 400, 404, unknown error | This model is broken (e.g. retired) | Cool it down; try the next route |
| 401 / 403 | The shared Gemini key is broken | Skip other Gemini models; go to Groq |
| Non-JSON or out-of-schema answer | This answer is bad | Try the next route, no cooldown |

An email fails only after every route has been tried. It is retried later if any route failed temporarily; otherwise it goes to Needs Review. Invalid output never silently becomes Spam. All 18 fault × scope combinations are tested and rendered in [the resilience matrix](docs/RESILIENCE.md).

The local model is a three-category **shadow**: in normal mode it can run beside the cloud decision for comparison, is metered separately, and never decides — including on manual re-analysis. Because it never decides, it is **off by default** (`MAILMIND_SHADOW_MODEL_ENABLED=false`): MailMind then starts no PyTorch process and spends no CPU on it per email. Set it to `true` to collect shadow comparisons again. In local-only mode it is the authoritative classifier and receives the same sender-aware input it was trained on.

Feedback is committed to SQLite first. The dashboard immediately uses the corrected label, while the derived feedback vector is reconciled asynchronously. A vector-store delay cannot block the user’s correction. Original predictions and revision history remain available for audit and undo.

**Your corrections win for similar mail.** When a new email is close to one you corrected (from the same sender and within cosine distance 0.40, or a near-copy from anyone within 0.20), your category is applied. The AI still runs for the explanation and actions, but cannot overrule you, and "Why this category?" says *Matches your earlier correction*. Only corrections verified against your current labels in SQLite count, so text inside an email can never act as one. On the owner's inbox, a leave-one-out check over 83 labelled emails matched the owner's own label 95% of the time where the rule applied (`src/feedback_rules.py`).

## Scam and phishing checks

Switched on with `MAILMIND_RISK_ANALYSIS_ENABLED=true`. Each email gets a score out of 100 and a **Scam check** panel listing every rule that fired with its evidence. The rules are local and deterministic: no AI provider sees the email for this.

On the CC0 synthetic benchmark (178 cases, split by template so no test email shares wording with a training email):

| Flag when | Precision | Recall | False-positive rate |
| --- | ---: | ---: | ---: |
| High (>= 70) | 100% (19/19) | 73.1% (19/26) | 0% (0/32) |
| Medium or high (>= 40) | 86.7% (26/30) | 100% (26/26) | 12.5% (4/32) |

Authentication alone would catch 76.9%: attackers who register their own lookalike domain pass SPF, DKIM and DMARC. Removing the lookalike checks drops high-band recall from 73.1% to 26.9%. The weights are fitted with a non-negativity constraint, so adding a red flag can never lower a score. The benchmark and rules share an author, so these numbers are optimistic; see the [full report and limits](docs/RISK_EVALUATION.md).

## Ask your inbox

Switched on with `MAILMIND_INBOX_QUESTIONS_ENABLED=true` (and a provider key). Ask a question in plain English; a read-only agent searches, opens at most 10 emails and answers in a few steps. **Every sentence must cite a quote that MailMind verifies in an email the agent actually opened**; sentences that fail are removed and counted, and "I couldn't find that" is a normal answer. The tools can only read, so text planted in an email can at worst produce a wrong answer, never a change. Senders reach the provider as domains, and amounts as tokens filled in locally.

On a 34-email, 30-question synthetic benchmark, the first full run (Gemini `gemini-3.5-flash-lite`) answered 27 of 30 correctly, declined all 5 unanswerable questions and never repeated the 3 planted instructions; it exposed that amounts reached answers as a bare `[AMOUNT]`, which is now fixed. That run is not replayable, so the committed [report](docs/AGENT_EVALUATION.md) is marked pending until it is re-recorded.

## Red-team

`python -m scripts.redteam` plays a fully hijacked AI model against MailMind's deterministic checks ([report](docs/REDTEAM.md); the Usage tab shows the result). With the checks off, 27 of 30 attacks reach the user or the AI; with them on, 1 does.

| Attack | Off | On |
| --- | ---: | ---: |
| Instructions hidden from the reader reach the AI | 5/5 | 0/5 |
| Zero-width characters hide a scam lure | 4/5 | 0/5 |
| A Cyrillic or Greek lookalike domain passes | 3/5 | 0/5 |
| Ask shows a hijacked claim as cited fact | 4/4 | 1/4 |
| Ask shows an amount it was never given | 2/2 | 0/2 |
| Ask reads far more mail than it needs | 1/1 | 0/1 |
| The brief offers an action it should not | 4/4 | 0/4 |
| A follow-up passes its checks with injected content | 4/4 | 0/4 |

The one that gets through is the honest limit of grounding: a hijacked model can repeat the attacker's own email with a real quote from it. The answer then cites that email, which the scam check usually marks. Whether a live model obeys injections in the first place still needs a recorded run.

## Daily brief

Switched on with `MAILMIND_DAILY_BRIEF_ENABLED=true`. Each morning a triage agent ranks what needs you (it sees titles only), a research agent per item extracts facts whose quotes are verified in the email, and a writer agent composes the brief citing only those facts; sentences citing anything else are removed. Suggested actions are typed, checked against the items the writer was given, and run through the existing Action Center code only when you approve. If any provider call fails, a deterministic summary from SQLite is shown instead. The three-agent vs one-agent comparison is ready to record ([pending](docs/BRIEF_EVALUATION.md)).

## Follow-ups

Switched on with `MAILMIND_FOLLOW_UPS_ENABLED=true`. Pick a sent email; MailMind reads the thread and writes a follow-up you copy into Gmail. It addresses the person exactly as you did, ends with your own sign-off and signature (learned locally from your sent mail), and is checked by deterministic rules: no stock AI phrases ("I hope this email finds you well", "circling back", "kindly revert"), no exclamation marks, dash chains or lists, the right length, references that quote the thread, and **no number, date or weekday the thread never mentions**. A failing draft gets one revision with the exact problems; anything left is shown, never hidden. The live evaluation and a human blind test are [pending](docs/FOLLOW_UP_EVALUATION.md): providers were out of quota when this was built.

## Money and subscriptions

Switched on with `MAILMIND_LEDGER_ENABLED=true`. The **Money** tab shows spending, refunds, bills due, top merchants and recurring charges from saved receipts, with the sentence each amount came from and an editable correction that rescans never overwrite.

**Amounts never leave the computer.** Each amount is replaced by a numbered placeholder (`[AMOUNT_1]`, ...) before a provider call; the provider only answers which placeholder is the total, and the number is filled in locally. The evaluation recorder asserts this for every request.

On the CC0 receipts benchmark, with recorded Gemini answers replayable offline ([full report](docs/LEDGER_EVALUATION.md)):

| Held-out receipts (written after the rules were frozen) | Detected | Amount exact | Merchant |
| --- | ---: | ---: | ---: |
| Local rules only (local-only mode) | 77.8% | 72.2% | 61.1% |
| Production path (rules gate + provider picks the placeholder) | 83.3% | 83.3% | 83.3% |

No advert or price alert was mistaken for a transaction. The remaining misses are receipts the local keyword gate never passes to the provider ("Total Payable", "To pay"); widening that gate when a provider is configured is the next step, to be measured on a fresh holdout.

## Hybrid search

Search begins approximately 300 ms after typing pauses:

- One- and two-character queries use parameterized lexical search only.
- Longer queries can combine lexical results with local semantic candidates.
- Exact text matches remain first.
- Search gracefully falls back to lexical results if semantic search is unavailable.
- Search results remain account-scoped.
- The source email remains unchanged; only the derived embedding document is bounded.

The search index is stored separately from feedback retrieval:

- `data/chroma_db` — feedback/RAG collection
- `data/search_chroma_db` — saved-email semantic search collection

## Account and privacy boundary

The trusted local dashboard opens its loopback session automatically. There is no pairing-code step. Google OAuth is still explicit.

| Action | Effect |
| --- | --- |
| Connect Google | Authorizes Gmail and enables account mail access and processing |
| Switch Google account | Cancels/fences old work, completes OAuth for the new account, and loads only its data |
| Stop processing | Ends the local session and pauses work; saved mail remains. The saved Google login is parked unused for 24 hours so Connect Google can reconnect silently, then deleted |
| Disconnect Google | Stops Gmail access; saved local records remain. The saved Google login is parked unused for 24 hours so Connect Google can reconnect silently, then deleted |
| Delete this account data | Removes that account’s managed mail, jobs, feedback, vectors, and credentials |
| Delete old unassigned data | Separately removes explicit legacy/unassigned state |

Restarting invalidates the active local browser session; the trusted dashboard opens a fresh one automatically. If your saved Google login is still valid, Gmail work resumes on its own, with no Connect Google click. Late operations cannot commit across an account-generation change.

Read [Security policy](SECURITY.md) and [Privacy and local-only policy](docs/PRIVACY_AND_LOCAL_ONLY.md) before using real mail.

## Dashboard

The dashboard provides:

- One overview of eight KPI cards: saved, processed, feedback, average classification time, open actions, due soon, overdue, and tokens today, plus **Risky emails** and **Spent this month** when those features are on
- Live work progress and bounded-queue counts
- Separate live-sync and historical-backlog controls
- Worker, model, provider, and refresh health
- Four category lanes: Important, Updates, Spam, and Review
- Live sender/subject/body/meaning search and category filters
- Classification details, confirmation, correction, undo, and history
- Explicit retry/recovery controls for unfinished processing and ambiguous notifications
- Gmail-style system alerts (offline, can't reach Gmail, service restarting, back online) with exponential-backoff retries; a network drop or service crash never signs you out or blanks the dashboard
- Temporary failures (AI outage or quota, Gmail or Telegram errors) retry until they succeed; only permanent ones stop, with the reason
- Inbox, Action Center and Usage sections in the top navbar, plus **Money**, **Ask** and **Follow-ups** when those features are on; the daily brief sits at the top of the Inbox and the red-team result in Usage
- A scam badge and **Scam check** panel on each email, with every warning sign and its evidence
- A "Why this category?" explanation inside each email card: a summary, the signals, the quoted evidence that grounds them, and the whole decision (which model decided and whether a backup answered, what the category means, past corrections used as examples, the local second opinion, your own label, and the time taken)
- Action Center filters by status and by action type (only the types the account actually has, with counts), and **Open source** shows the original email in a pop-up with the quoted sentence highlighted (Esc closes it)
- Day, week, and month token charts that separate provider-billed from locally processed usage, with ranked per-provider and per-operation breakdowns
- An explicit, observable saved-mail intelligence backfill control
- A power-on sequence when the dashboard comes up: blocks roll up one after another, then the numbers spool up
- Framer Motion animations: sliding tab pill, card entrances, lane-to-lane moves on relabel, count-up numbers, growing share bars (all switched off by the system reduced-motion setting)
- Responsive keyboard-accessible layouts and reduced-motion behavior

See [User guide](docs/USER_GUIDE.md) and [Frontend guide](frontend/README.md).

## Requirements

Validated native target:

- Windows x64
- CPython 3.14 virtual environment
- Node.js 22.x
- CPU inference
- Ports `127.0.0.1:8000` and `127.0.0.1:5173`

The supplied Docker Compose configuration is a local-only demonstration. It currently contains API, worker, and frontend services; it does **not** contain the native supervisor’s semantic-indexer service. Do not claim complete semantic-search draining in the Compose demo until that service is added and verified.

## Installation

From PowerShell:

```powershell
py -3.14 -m venv venv
.\venv\Scripts\python.exe -m pip install torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt --index-url https://pypi.org/simple
.\venv\Scripts\python.exe -m pip check

Set-Location frontend
npm ci
npm run build
Set-Location ..
```

Copy `.env.example` to `.env` only when you intentionally configure providers. Never commit `.env`, `credentials.json`, OAuth tokens, databases, logs, exports, private mail, or private model assets.

For Gmail:

1. Enable the Gmail API in Google Cloud.
2. Configure the OAuth consent screen and test-user access.
3. Create a **Desktop app** OAuth client.
4. Save its ignored JSON file as `credentials.json` in the project root.
5. Start MailMind and choose **Connect Google**.

Detailed setup and release instructions are in [Setup and release](docs/SETUP_AND_RELEASE.md).

## Run

```powershell
.\start_all.bat
```

Open <http://127.0.0.1:5173>.

Status and privacy-safe diagnostics:

```powershell
.\venv\Scripts\python.exe -m scripts.launch status
.\venv\Scripts\python.exe -m scripts.runtime_diagnostics
```

Stop only MailMind-owned services:

```powershell
.\venv\Scripts\python.exe -m scripts.launch stop
```

The supervisor refuses conflicting ports instead of terminating unrelated processes. It restarts any owned service that stops (indexer, API, worker or frontend) with backoff (1, 2, 4, 8, 16, 32, then 60 s), for as long as it takes. The only exception is a startup crash loop, where a service fails within 10 s of starting 5 times in a row. MailMind then warns you in the terminal and the dashboard, keeps retrying for 60 s, and shuts down only if the service never recovers.

## Verification

Backend:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

Frontend:

```powershell
Set-Location frontend
npm test -- --run
npm run lint
npm run build
npm run test:browser
```

Latest local verification (20 October 2026):

- Backend discovery: 686 tests passed in 33 modules; the 25 earlier modules were each verified alone on 30 September, and the eight new modules were each verified alone on 20 October.
- Provider fault-injection matrix: 18 fault × scope cells match the documented contract (`python -m scripts.render_fault_matrix`).
- Frontend unit suite: 65 passed. Playwright browser suite: 47 passed.
- Frontend lint and production build passed; initial JavaScript is 377 kB (119 kB gzip); the chart (373 kB) and the animation engine (86 kB) load as separate chunks.

These are synthetic and temporary-data checks. They do not prove real-inbox model accuracy, provider retention behavior, Telegram delivery, or universal privacy.

See [Test guide](tests/README.md).

## Evaluation

Every route is evaluated on a 180-email CC0 synthetic benchmark with production-identical requests. The raw answers are recorded, so `python -m scripts.evaluate_cloud --replay` reproduces the [full report](docs/EVALUATION.md) offline, byte for byte.

| Route | Accuracy | Macro-F1 (95% CI) | Urgent misses | Latency p50 |
| --- | ---: | --- | ---: | ---: |
| Gemini `gemini-3.5-flash-lite` | 100% | 1.000 | 0 | 1.3 s |
| Groq `openai/gpt-oss-20b` | 95.6% | 0.974 (0.955–0.990) | 4 | 1.3 s |
| Local DistilBERT shadow | 47.8% | 0.400 (0.304–0.493) | 0 | local |

The perfect Gemini score is a ceiling effect: the benchmark is deliberately unambiguous and cannot rank strong models. The local shadow was trained on a private mailbox and is out of distribution here. `gemini-3.8-flash` (primary) could not be measured because the provider returned 503 "high demand" throughout. These are controlled route comparisons, not real-inbox accuracy.

## What I found in my own audit

A deliberate audit of this codebase found and fixed these defects. Each one was first reproduced as a failing test through the real code path:

| Defect | Impact | Fix |
| --- | --- | --- |
| Failover only on transient errors | A retired model (404) or rejected parameter (400) sent every email to review without trying the fallbacks | Failures are classified by what they prove; see [resilience matrix](docs/RESILIENCE.md) |
| Re-analyze used the shadow model | After startup, the local shadow's label replaced the cloud decision and no actions were extracted | One explicit authoritative route in `src/classification_service.py` |
| Grounding vs privacy mismatch | Actions quoting a redacted span (`[AMOUNT]`) passed validation, then were silently dropped at persistence, including on a retry path | One definition of "the text the provider saw" |
| Training-serving skew | Local-only re-analysis called the model without the sender field it was trained with | The sender is passed on every authoritative local path |
| No label provenance | Retraining could learn from the local model's own outputs; 87% of labels were cloud decisions | Provenance tracked; local self-labels excluded |
| Deadline contract mismatch (found by the evaluation) | The parser rejected plain calendar dates and unresolved deadlines, silently discarding 58% of Gemini's proposed actions | Date-only deadlines anchored to the local day; unresolved ones kept as "unknown": 25/60 → 60/60 on the same recordings |
| Telegram wait shorter than the request | Slow but successful alerts were recorded as "delivery unknown", and a send that never started was too | The worker outlasts the request's own timeouts; "unknown" now means the alert may really have been delivered |
| History paging skipped new mail (found in use) | When 20+ new emails arrived between checks, the next history page was requested with `maxResults=0` (HTTP 400) and the cursor still jumped past the whole batch; failed downloads were also never retried | The cursor only advances past admitted mail; failed downloads are re-fetched; a 5-minute sweep of the last two days' INBOX catches anything history sync misses |
| Text-typed retry times | Retry times stored as text never compared as due in SQLite, freezing the backlog so older mail stopped loading | Each worker cycle repairs non-numeric retry times |
| One quote voided an explanation | A single quote not found byte-for-byte in the email discarded the whole explanation | Punctuation-tolerant grounding shared by explanations and actions; unverifiable quotes are dropped individually |
| Misleading deletion error | An interrupted account deletion reported "no account to delete" while the data was still there | Only a missing account is a conflict; other failures say "retry" |
| Deadlines 5 h 30 min late (found in use) | The model saw the received time in UTC, so "by 5 pm" was saved as 17:00 UTC and shown as 10:30 pm in India | The model sees the received time in the user's zone, and the parser treats a zoneless or wrongly-UTC time as local unless the email names UTC/GMT; 14 stored deadlines repaired after a backup (`src/llm_api.py`) |
| Corrections ignored for similar mail (found in use) | A correction reached the model only as one of ≥ 3 examples within distance 0.25, the model could still ignore it, and after every restart the worker never opened the feedback index | A verified-correction rule: same sender ≤ 0.40, or near-copy ≤ 0.20 (95% agreement with the owner's own labels); the index opens before the first email (`src/feedback_rules.py`, `src/main.py`) |
| Scam rules misread real mail (found on the owner's inbox) | A bank's "to open the statement, enter your password as..." read as an OTP request, and a fund house's own hyphenated domain as a lookalike | Instructions for opening a protected file are excluded; domains with repeated good mail and no spam are trusted (`src/risk_analysis.py`) |
| Dashboard reads failed under load (found by load test, predates these features) | Every read took SQLite's write lock, so 20 parallel dashboard reads queued past the 5 s busy timeout: 13 to 17 of 300 failed with "storage busy" on the original code too | Pure reads use a read-only snapshot that neither waits for nor blocks writers; token metering waits at most 1 s. 0 of 300 failed afterwards, and 0 of 400 at 80 parallel requests (`src/account_state.py`, `tests/test_concurrency.py`) |
| An Ask question froze the database for its whole run (found on the live system) | The provider call and the embedding search ran inside a database transaction, so other requests failed with "storage busy" for up to 20 s | The account is checked before and after each call, with no lock held during it (`api/questions.py`, `src/inbox_agent.py`) |
| The red-team's first numbers were wrong (found by its own control) | Every follow-up attack looked blocked, but only because the test draft was too short; the clean draft failed the same way. Measured properly, a false "I have already transferred the amount" and a planted plain-text domain got through, and an injected message saying "transferred" made the claim look supported | Each family now has a control that must pass; links and claims of having acted must come from the user's own messages (`scripts/redteam.py`, `src/follow_ups.py`) |
| Hidden instructions reached the AI (found by red-team) | Text styled `display:none` or zero-size was saved as body text, so providers read instructions the user never saw | Invisible text is no longer saved as body text; the scam check keeps it as a signal |
| Follow-ups greeted mailboxes by role (found by benchmark) | "Sales Desk" and "HR Team" became "Dear Sales," and "Dear HR,", and surnames in the user's own greeting ("Dear Ms. Menon") reached the provider | The follow-up addresses people exactly as the user did in their last message, and that name is masked too (`src/follow_ups.py`) |
| Groq rejected half the agent's steps (found while recording) | Shown a list of actions, `openai/gpt-oss-20b` answered the JSON-schema request with a native function call, which Groq refuses (`tool_use_failed`), so the fallback route failed | The rejected call, which Groq returns, is rebuilt into the step it meant and validated like any other answer (`salvage_step`) |
| An interrupted recording destroyed a good one | Re-recording overwrote the evaluation file as it went, so a quota failure mid-run lost a complete earlier run | Recordings go to a side file that replaces the committed one only when complete |
| Privacy check joined separate numbers (found while recording) | The no-amount-leaks assertion glued every digit in a request together, so a date next to an order number ("2026-08-16 ... #40222") looked like a hidden ₹1,640 and stopped the run | It compares each amount with each standalone number in the email text only (`scripts/evaluate_ledger.py`); a planted leak is still caught |
| Amounts written after the number were unreadable (found by unit test) | "500 rupees" or "99 INR" crashed amount parsing because of a wrong regex group, which the receipts benchmark never exercised | Correct groups for both orders, with a test for each format (`src/ledger.py`) |
| Status file crashed MailMind (found in use) | On Windows, renaming over a file another process had open raised "Access is denied", and the uncaught error shut every service down | Status writes only on change, retry briefly, then skip, and never raise; credential saves retry the same way (`scripts/launch.py`, `src/file_utils.py`) |

Live evaluation also found that the previously configured fallback model (`gemini-1.5-flash`) is retired (HTTP 404), which is exactly the case the first fix handles.

## Current limitations

- The approved personal dataset is too limited to claim production classifier quality.
- The local shadow model is effectively a distillation of the cloud classifier: 87% of its training labels are cloud decisions (308 of 356), so its private test score measures agreement with Gemini, not accuracy.
- Cloud accuracy is measured only on a 180-email synthetic benchmark (see [evaluation](docs/EVALUATION.md)), not on real inboxes.
- There is no CI; the test suites are run locally.
- Regex masking is minimization, not anonymization.
- Local storage is not encrypted by MailMind.
- Local-only mode is an application routing boundary, not an operating-system firewall.
- Gmail desktop OAuth is not a remote hosted callback design.
- The Compose demo does not yet run the semantic indexer.
- Provider quotas, retention, and service policies remain external.
- The supported product is one trusted local user, not a hosted multi-tenant service.
- Automated tests use synthetic or temporary data; the controlled live OAuth/Gmail checklist must be run deliberately before claiming live-provider validation.
- The scam, money, Ask, follow-up and brief benchmarks are synthetic and written by the same person as the features, so their numbers are optimistic. The Ask, follow-up and brief live recordings and the human blind test for follow-ups are still pending (providers were out of quota); their reports say so.
- Grounding proves that an email said something, not that it is true: a hijacked model can still repeat an attacker's own email with a real quote (the one residual case in the [red-team](docs/REDTEAM.md)).
- Scam scores are warnings from local rules: innocent names one letter from a brand can be flagged, and senders you trust stop counting as lookalikes.

## Documentation

- [Documentation index](docs/README.md)
- [User guide](docs/USER_GUIDE.md)
- [Current architecture](docs/ARCHITECTURE.md)
- [Setup and release](docs/SETUP_AND_RELEASE.md)
- [Security policy](SECURITY.md)
- [Privacy and local-only](docs/PRIVACY_AND_LOCAL_ONLY.md)
- [Security design showcase](docs/SECURITY_FEATURES_SHOWCASE.md)
- [Classifier evaluation](docs/EVALUATION.md)
- [Scam and phishing evaluation](docs/RISK_EVALUATION.md)
- [Money extraction evaluation](docs/LEDGER_EVALUATION.md)
- [Ask your inbox evaluation](docs/AGENT_EVALUATION.md)
- [Follow-up evaluation](docs/FOLLOW_UP_EVALUATION.md)
- [Daily brief evaluation](docs/BRIEF_EVALUATION.md)
- [Red-team](docs/REDTEAM.md)
- [Self-recovery](docs/SELF_RECOVERY.md)
- [Provider resilience matrix](docs/RESILIENCE.md)
- [Performance measurements](docs/PERFORMANCE.md)
- [Frontend guide](frontend/README.md)
- [Test guide](tests/README.md)
- [Contributing and privacy checks](CONTRIBUTING.md)

## License and data note

Copyright © 2026 Priyansh Waghela. All rights reserved. The source is published for viewing and evaluation; no license to use, copy, modify, or distribute it is granted.

The synthetic benchmarks in `fixtures/priority_benchmark/`, `fixtures/phishing_benchmark/`, `fixtures/receipts_benchmark/`, `fixtures/inbox_qa/` and `fixtures/follow_up_benchmark/` are CC0, as documented in their provenance files; every person, address and company in them is fictional (personal-mailbox addresses start with `fixture.`). `fixtures/recorded_cloud/` holds recorded provider answers to those synthetic requests only. Model, dataset, Gmail, Gemini, Groq, Telegram, and dependency terms apply independently.

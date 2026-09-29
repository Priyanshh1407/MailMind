# MailMind frontend

The MailMind dashboard is a React 19 and Vite application for the local FastAPI control plane. It presents account-scoped Gmail processing, health, hybrid search, category lanes, feedback, history, and explicit recovery actions.

The production launcher serves the built application locally. The browser never receives Gmail OAuth credentials, model weights, or provider keys.

## Design

The current interface follows a dark, compact dashboard design with:

- local MailMind logo and favicon assets;
- Lucide React icons;
- account-wide KPI cards;
- inbox progress and intake panels;
- non-sticky search and category controls;
- four responsive category lanes;
- single-line email action controls where space permits;
- system-health and account popovers;
- dismissible success/error feedback;
- mobile overflow protection; and
- reduced-motion behavior.

## Data flow

~~~mermaid
flowchart LR
  Browser[React dashboard]
  APIClient[Validated API client]
  Hook[Dashboard polling and mutations]
  API[FastAPI on 127.0.0.1:8000]

  Browser --> APIClient
  APIClient --> Hook
  Hook --> API
  API --> Hook
  Hook --> Browser
~~~

- <code>src/api.js</code> owns request timeouts, credentials, CSRF headers, and response validation.
- <code>src/dashboard.js</code> coordinates account-consistent reads and presentation helpers.
- <code>src/hooks/useDashboard.js</code> owns completion-based polling, cancellation, stale-response rejection, and serialized mutations.
- Components render all server/email text through React. No raw-HTML rendering sink is used.

The integrated intelligence snapshot loads bounded action summary/page and
token summary reads alongside mail, status, and telemetry. Every response is
bound to the current account and generation. Action or analytics outages remain
section-local, while a session/account mismatch cancels the complete snapshot.

## Main components

| Component | Responsibility |
| --- | --- |
| <code>AppHeader</code> | Brand, system health, connected-account controls |
| <code>HealthPopover</code> | Model, provider, worker, prediction, and refresh state |
| <code>AccountControls</code> | Connect, switch, disconnect, delete, and stop-processing actions |
| <code>DashboardStats</code> | Account-wide saved, processed, feedback, and latency KPIs |
| <code>InboxProgress</code> | Current admitted batch and durable workflow counts |
| <code>InboxIntake</code> | Live sync, bounded historical backlog, and explicit saved-mail intelligence backfill |
| <code>SearchFilters</code> | Debounced hybrid query, category filter, and semantic progress |
| <code>EmailBoard</code> | Important, Updates, Spam, and Review lanes |
| <code>EmailCard</code> | Classification details, feedback, history, and recovery |
| <code>Pagination</code> | Stable 20-message pages |
| <code>AppFeedback</code> | Persistent outages and timed action notices |
| <code>ActionSummary</code> | Open, due-soon, overdue, and tokens-today KPIs |
| <code>ActionCenter</code> | Bounded action filters, lifecycle controls, snoozing, and source navigation |
| <code>ClassificationExplanation</code> | Validated category rationale, signal chips, source, and model details |
| <code>TokenUsagePanel</code> | Recharts daily trend, cloud/local split, and provider/operation breakdowns |

## Search behavior

Search input is debounced by approximately 300 ms.

- Short queries use lexical matching.
- Queries of at least three characters can combine lexical and semantic results.
- The current board remains visible while the new request is loading.
- Stale requests are aborted.
- The category filter and Search button remain grouped and aligned across responsive widths.
- The search panel scrolls with the document and is not sticky.
- Semantic failure does not prevent lexical results.

## Account behavior

The trusted loopback browser session opens automatically. There is no pairing-code UI.

Mail data is rendered only while Google is connected. Account actions have deliberately different meanings:

- stop processing retains mail and credentials;
- disconnect removes the local Google credential but retains saved data;
- delete removes managed data for the selected account;
- switch performs a fenced account transition.

A refresh that mixes account generations is rejected.

## Feedback behavior

Confirming or correcting a category saves authoritative feedback before derived vector indexing completes. The card moves to its effective lane after a successful response. Feedback can be edited or undone without deleting prediction or revision history.

Buttons are disabled while their mutation is pending, preventing duplicate actions.

## Install and build

From <code>frontend</code>:

~~~powershell
npm ci
npm test -- --run
npm run lint
npm run build
~~~

The production launcher serves <code>dist</code> through <code>serve.mjs</code>. Rebuild after changing frontend source.

Development server:

~~~powershell
npm run dev
~~~

## Browser tests

~~~powershell
npx playwright install chromium
npm run test:browser
~~~

The browser suite uses synthetic in-browser API fixtures. It exercises account menus, category lanes, search, pagination, feedback, error handling, sync/backlog controls, history, responsive overflow, reduced motion, account transitions, provider display, and local-only behavior.

It does not sign in to Google, read real mail, call live cloud providers, or send Telegram messages.

At the 29 September 2026 Phase 8 verification, the frontend unit suite contains 42
passing tests, including feature-specific validation for bounded backfill status
and acknowledgements; the browser suite contains 24 passing scenarios. The
Phase 8 scenario proves the control is explicit, capped at 20, observable, and
describes its historical side-effect suppression. Lint and the production build
also pass.

## Security and privacy

- Requests carry credentials only to the local API.
- Mutations include the session CSRF token.
- The API enforces host, origin, session, account, and generation boundaries.
- React escapes provider and email text.
- Account deletion requires explicit confirmation.
- A failed refresh retains the last snapshot but disables unsafe mutations.
- Local-only mode disables unavailable external controls.
- UI status distinguishes configured providers from observed health.
- Action and usage tabs are only rendered after the connected Google session
  guard succeeds; source-email navigation remains account-scoped.
- Token charts use Recharts with a visible table fallback and never present
  token counts as cost estimates.

The frontend cannot make an untrusted workstation safe. Review the root security and privacy documentation before using real mail.

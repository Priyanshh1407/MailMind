# Performance measurements

Measured on 2026-09-30 on the developer's Windows x64 workstation (CPU only). The data is synthetic, and the numbers are local and single-user; they are not a hosted-capacity claim.

## Dashboard email list (`GET /emails`)

**Question:** the list endpoint runs about five small SQLite queries per returned row (latest prediction, feedback, task, notification). Is that N+1 pattern a problem at realistic mailbox sizes?

**Method:** `python -m scripts.measure_email_list --emails 5000 --repeats 40`.
- Seeds 5,000 synthetic emails.
- Every row gets the side tables the endpoint reads: a prediction attempt and a processing task; plus a notification for one third of the rows and feedback for one tenth.
- Times 40 authenticated requests per case through the real FastAPI app, after one warm-up request.

| Case | Rows returned | p50 | p95 |
| --- | ---: | ---: | ---: |
| Page of 20 (dashboard default) | 20 | 13.2 ms | 16.4 ms |
| Page of 50 (API default) | 50 | 17.0 ms | 21.2 ms |
| Page of 200 (API maximum) | 200 | 37.4 ms | 44.4 ms |
| 2-character lexical search | 20 | 17.1 ms | 20.2 ms |
| Body-term search (`LIKE` over 5,000 bodies) | 1 | 25.6 ms | 29.4 ms |

**Decision: no change.**
- The pre-agreed threshold was p95 > 100 ms at 5,000 emails.
- The worst case, the maximum page size, is 44 ms, and the real dashboard page is 16 ms.
- Replacing per-row lookups with a joined query would add complexity for no user-visible gain at single-user scale.
- Revisit this if mailboxes grow by an order of magnitude or the page size increases.

## Frontend bundle

**Problem:** the production build shipped one 633 kB JavaScript chunk and triggered Vite's >500 kB warning. Recharts, the largest dependency, is used only by the Usage tab.

**Change:**
- `TokenUsageChart` is loaded with `React.lazy`.
- `App` mounts the Usage panel on the tab's first visit and keeps it mounted afterwards.

| Build | Initial JS | Initial JS (gzip) | Deferred chart chunk |
| --- | ---: | ---: | ---: |
| Before | 632.9 kB | 188.9 kB | — |
| After | 264.2 kB | 82.7 kB | 369.8 kB (loaded on first Usage visit) |

Initial JavaScript is 58% smaller before gzip and 56% smaller after gzip. The build no longer warns. The Playwright browser suite, including the Usage-tab chart test, passes 24/24.

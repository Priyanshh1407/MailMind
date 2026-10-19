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

## Concurrent dashboard reads

**Problem (found by load test, 20 October 2026):** every read went through `BEGIN IMMEDIATE`, which takes SQLite's write lock. With 20 requests at once on a copy of real data (398 emails), 13 to 17 of 300 failed with 503 "storage busy" and p95 was about 5.6 s. The original code, before the October features, failed the same way.

**Change:** pure reads (20 routes and the per-request session check) use `BEGIN DEFERRED` with `PRAGMA query_only=ON`, so they read a WAL snapshot that neither waits for nor blocks writers; a write inside one fails loudly. Token metering waits at most 1 s and then skips the event. Ask holds no transaction during a provider call or embedding search.

| Load (copy of real data, Windows laptop) | Failed | Median | p95 |
| --- | ---: | ---: | ---: |
| 300 mixed reads incl. hybrid search, 20 parallel, before | 13-17 | - | ~5.6 s |
| Same, after (8 runs) | 0 | 290-740 ms | 0.5-1.4 s |
| 400 dashboard reads, 80 parallel, after | 0 | ~87 ms | ~115 ms |
| Dashboard refresh mix, 10 parallel, with 1 GB free memory | 0 | 244 ms | 324 ms |

Hybrid search is bound by the query embedding (about 0.2 s alone with free memory, 0.9 s when the machine was short of memory). `tests/test_concurrency.py` holds the lock in another connection and asserts reads still succeed quickly.

## Frontend bundle

**Problem:** the production build shipped one 633 kB JavaScript chunk and triggered Vite's >500 kB warning. Recharts, the largest dependency, is used only by the Usage tab.

**Change:**
- `TokenUsageChart` is loaded with `React.lazy`.
- `App` mounts the Usage panel on the tab's first visit and keeps it mounted afterwards.

| Build | Initial JS | Initial JS (gzip) | Deferred chart chunk |
| --- | ---: | ---: | ---: |
| Before | 632.9 kB | 188.9 kB | — |
| After | 264.2 kB | 82.7 kB | 369.8 kB (loaded on first Usage visit) |

Initial JavaScript is 58% smaller before gzip and 56% smaller after gzip. The build no longer warns. The Playwright browser suite, including the Usage-tab chart test, passed 24/24 at the time.

### Animations (Framer Motion)

Adding Framer Motion with ordinary imports put 409 kB (130 kB gzip) in the initial chunk. That was too much for the effect, so:

- components use the small `m` elements inside `<LazyMotion>`, and the `domMax` feature set loads as its own chunk after first paint (`src/motionFeatures.js`);
- the number count-up uses a `requestAnimationFrame` loop instead of the animation engine.

| Build | Initial JS | Initial JS (gzip) | Deferred chunks |
| --- | ---: | ---: | ---: |
| Before animations | 264.2 kB | 82.7 kB | chart 369.8 kB |
| Framer Motion, direct imports | 408.9 kB | 129.6 kB | chart 372.9 kB |
| Framer Motion, `LazyMotion` (shipped) | 315.5 kB | 100.4 kB | chart 372.9 kB, motion 85.6 kB |

The CSS power-on sequence added later costs 0.6 kB. With the October tabs (Money, Ask, Follow-ups, brief, scam panel, security report) the initial chunk is 377.4 kB (118.7 kB gzip); the Money chart reuses the deferred Recharts chunk. The Playwright browser suite passes 29/29, including tests that the animations settle on exact values, that the power-on sequence plays once and ends, and that reduced motion skips it and shows final values at once.

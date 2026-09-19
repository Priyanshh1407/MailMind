# Local architecture decision

Date: 19 September 2026. Status: retain the local architecture; expansion deferred.

## Decision and scope

MailMind remains one local workstation, one active account and one processing worker.
Keep SQLite, the existing durable task/outbox tables and the derived Chroma index.
No PostgreSQL, Redis, external queue, distributed cache or extra inference service
is introduced. There is no requested multi-user hosting requirement. The measured
synthetic saved-backlog workloads complete without duplicate effects or dashboard
read errors. This is evidence for retaining the current local design, not proof
that every real inbox or arbitrarily large dataset is supported.

## What the measurement exercises

`python -m scripts.measure_local_scale` creates temporary storage and fake .test
accounts. The real API pairs/authenticates through fake OAuth. A stopped real worker
cycle adopts original synthetic bodies, then the real pipeline drains saved tasks
in batches of 20. One dashboard reader requests a 50-row page and telemetry about
every five seconds, matching the UI poll interval. This is a dashboard API subset: the actual UI uses
20 rows and also reads session/status. Those extra calls and browser rendering are
not included. A final replay verifies that
saved messages do not produce additional classifications, alerts or mark-read calls.

Four sequential scenarios compare 100 messages without dashboard reads, 100 with
reads, 500 with reads, and 100 with reads plus a fake 10ms classification delay.
They record cycle durations/backlog, API sample counts and p50/p95/max latency,
process CPU/RSS and SQLite storage. API calls use TestClient, not actual HTTP
transport. Gmail/cloud/Telegram/model/embedding adapters are synthetic. All body
adoption is preloaded intentionally to isolate saved-backlog processing; this is
not a Gmail page size or measured download rate. No private files are read.

The measurement script writes a local generated report under `docs/evaluation/`, which Git ignores. Memory is the whole
single Python test process, including imports and its API. It is not the combined
API/worker/browser/Docker memory. Sequential scenarios have different allocator
warmth. The sampler runs about every 50ms plus API call time; it can miss peaks.
Other host activity is uncontrolled. Small p95 sample counts are disclosed and
must not become an SLA or production capacity promise.

## Limits already present in the code

| Limit | Current setting and consequence |
| --- | --- |
| Active account | One global runtime account; switching invalidates stale work. This is not a tenant scheduler. |
| Processing batch | 20 by default; configured range 1..200. Raising it requires new timing/recovery checks. |
| Worker polling | Waits 60 seconds after a cycle by default. Dashboard reads do not drain tasks. |
| Gmail listing | Page size 10, up to 3 pages, maximum batch 20 by default. Already adopted bodies are not downloaded again. |
| SQL writes | Guarded transactions and one fenced cycle owner. A second worker does not provide supported parallel throughput. |
| Provider classification | Default 20-second budget; provider timeout default 5 seconds. Real provider delay is unmeasured here. |
| Retries | At most 3 processing attempts by default; delayed retries/dead/unknown states need separate recovery. |
| API pages | Default 50, maximum 200. History is queried per row; large pages need separate measurements. |
| Adapter workers | Two bounded provider workers; local, retrieval and indexing have one each. These are resource bounds, not provider quota guarantees. |
| Data growth | No automatic retention/capacity policy; bodies, histories and derived assets consume space as retained mail grows. |

For an existing queued backlog, assuming the first cycle starts immediately,
minimum scheduled drain estimate = measured immediate drain + 60 seconds times
(number of cycles minus one). 100 messages need five cycles, adding four minutes.
500 need 25 cycles, adding 24 minutes. An initial wait, ingestion, real model load,
provider calls and retries add more. The ideal batch/poll ceiling approaches 20
messages per minute only when processing time is negligible; it is not a measured
live Gmail throughput promise. A manually requested cycle may change waiting time.

The visible bottleneck for scheduled backlog clearance is the default polling wait.
The synthetic active-cycle time also includes SQLite transactions, Python routing,
thread handoff and bookkeeping. The workload is not instrumented finely enough to
blame a precise SQL statement or prove that production storage dominates inference.
Real pretrained-model and embedding cold/warm resource cost is deliberately unmeasured.
The Phase 7 tiny-model batched inference timings are a separate experiment and
cannot substitute for production API latency.

## When to reconsider the design

First measure an explicitly provisioned, nonprivate model and embedding path, real
transport latency, agreed inbox sizes and provider delays. Agree a response-time and
storage budget before declaring the result good or bad. Record oldest queued task,
queue growth, retry age, cycle time, provider error rates, RSS and disk consumption.
If backlog routinely grows while work completes or the agreed response target is
missed, distinguish polling delay, provider limits, inference and storage first.

A shorter polling wait or larger batch is a deliberate setting change, not an
unverified optimization made by this phase. It must respect provider quotas,
shutdown deadlines and recovery. Add a dedicated inference process only if measured
model contention/resource cost requires it. Consider PostgreSQL/external queues only
if independently tested concurrent users/processes need that architecture. Retain
message idempotency, outbox ambiguity and account fencing through any migration.

## Required gates before multi-user hosting

1. Replace desktop OAuth with a server-side redirect/state/PKCE design appropriate
   to the chosen provider; secure token storage and revocation/account lifecycle.
2. Design per-user browser sessions and tenant ownership on every SQL/vector/job
   operation. The current single global runtime account is not sufficient.
3. Add per-user scheduling budgets, provider quota limits, queue fairness, overload
   rejection and observability. A timeout cannot cancel an already sent request.
4. Design TLS, allowed origins, public routing, backups/restore and retention before
   exposure. The current loopback/default Compose target is not a public service.
5. Load-test that new process layout and rerun cross-account access, stale work,
   duplicate retries, ambiguous delivery, token expiry/quota and restart tests.

These are future acceptance gates, not a completed hosted architecture design.
Because this phase adds no new production process layout, existing account/recovery
checks are rerun; live quotas and a hypothetical migration remain unvalidated.

## Automation choice

GitHub CI was removed at the developer's request. No Actions workflow remains.
Use the documented local strict suite, frontend tests/build/lint/browser checks and
release helpers before sharing changes. A push does not run them automatically.


## Recorded observations and conclusion

Final measured immediate drain: 100/no reader 11.29s; 100/reader 12.23s;
500/reader 78.22s; 100/reader with a fake 10ms classification delay 21.32s.
The 500 case completed all tasks with no duplicate side effects. Email/telemetry
p95 were 136.14ms/130.15ms from 16 samples each. Peak single-process sampled RSS
was 89.7MiB; SQLite files were 1.54MiB. At default polling its saved-backlog drain
estimate is about 25m18s. No formal user latency target is assumed or claimed met.
These modest workloads support retaining the local design; they do not establish
an upper supported inbox size, real model resource budget or hosted capacity.
All 332 strict backend checks passed. Expansion is deferred under the roadmap's
explicit local-architecture exit criterion.

# Local architecture decision

**Status:** accepted for the current product scope  
**Last reviewed:** 25 September 2026

## Decision

MailMind remains a single-user, local-workstation application built around:

- one selected Gmail account at a time;
- a loopback FastAPI control plane;
- SQLite authoritative state;
- one Gmail/classification worker;
- one isolated semantic-indexer process;
- one built React frontend process;
- embedded feedback and search Chroma stores; and
- bounded external provider calls.

No hosted queue, distributed database, remote vector server, Kubernetes deployment, or multi-tenant identity system is introduced.

## Rationale

The current constraints favor a small, inspectable architecture:

- active processing is capped at 100 tasks by default;
- historical intake is deliberately user-controlled;
- one worker time-slices newest-first work;
- SQLite provides durable transactions and recovery for the supported workload;
- embedded vector stores avoid a network service and tenant boundary;
- account-generation fencing prevents stale local work;
- the isolated semantic indexer removes native embedding startup from the Gmail path; and
- the supervisor provides exact ownership and bounded recovery.

Additional infrastructure would add operational and security surface without solving a measured requirement.

## Current process topology

~~~mermaid
flowchart LR
  Supervisor --> Indexer
  Supervisor --> API
  Supervisor --> Worker
  Supervisor --> Frontend
  API --> SQLite
  Worker --> SQLite
  Indexer --> SQLite
  Worker --> Gmail
  Worker --> Providers
  Indexer --> SearchStore
  Worker --> FeedbackStore
~~~

The indexer starts as an owned process but waits for Google connection before touching account mail or semantic assets.

## Capacity policy

The system is designed for personal inbox workloads, not organizational throughput.

Scaling controls already present include:

- bounded Gmail pages and detail requests;
- maximum active-task capacity;
- small newest-first worker slices;
- typed retry limits and exponential backoff;
- independent provider capacity;
- durable notification state;
- batched semantic indexing; and
- paginated dashboard reads.

## When to reconsider

Revisit the decision only after measured evidence shows one or more of the following:

- sustained queue growth under the intended personal workload;
- SQLite lock contention that affects user-visible latency;
- a requirement for simultaneous active users or accounts;
- a requirement for multiple worker hosts;
- an embedding corpus that no longer fits the workstation;
- availability requirements that a single workstation cannot meet; or
- a supported hosted product requirement.

## Likely evolution path

If expansion becomes necessary:

1. Preserve account identity, task idempotency, revision checks, and generation semantics.
2. Move authoritative relational state to a server database.
3. Replace local leases with a durable distributed queue.
4. Split Gmail discovery, classification, notifications, and semantic indexing into independently scalable workers.
5. Introduce hosted authentication and authorization appropriate to the deployment.
6. Replace desktop OAuth with a hosted callback and protected credential store.
7. Add centralized observability and deployment-specific encryption/key management.
8. Re-run privacy, threat-model, load, and failure testing before claiming support.

## Explicit non-goals

The current release does not claim:

- public internet exposure;
- multi-tenant isolation;
- horizontal worker scaling;
- high availability;
- exactly-once external delivery;
- production classifier accuracy;
- unlimited automatic inbox extraction; or
- container/native semantic-index parity.

The local architecture is a deliberate product boundary, not an unfinished attempt at distributed infrastructure.


# Security maintenance report

**Review date:** 25 September 2026  
**Supported deployment:** one trusted user on one local workstation

## Result

The current documentation was reconciled against schema version 10, the four-process native supervisor, OAuth-gated semantic indexing, bounded inbox intake, account switching, hybrid search, current frontend behavior, and the current test inventory.

No Git commands, credentials, OAuth files, provider keys, private databases, Gmail content, or private model assets were used for this documentation review.

## Current verification

| Check | Result |
| --- | --- |
| Backend discovery | 384 passed |
| Backend release-policy check | Passed; direct frontend dependencies match their exact lockfile versions |
| Frontend unit tests | 26 passed |
| Playwright browser scenarios | 19 passed |
| Frontend lint | Passed |
| Frontend production build | Passed |

All checks listed above passed on 25 September 2026. This is evidence for the tested behaviors, not a claim of universal security, provider availability, or real-inbox model accuracy.

## Reviewed security boundaries

### Session and request protection

- trusted loopback session creation;
- HttpOnly, SameSite=Strict cookie;
- CSRF and Origin checks for mutations;
- trusted-host enforcement;
- session expiry and restart invalidation;
- automatic trusted-dashboard session creation without a pairing code.

### Account isolation

- account-scoped SQL and vector identifiers;
- generation fencing around slow/external work;
- account-switch response rejection;
- separate account-specific OAuth credentials;
- selected-account deletion that preserves unrelated accounts.

### Background reliability

- durable tasks and processing history;
- expiring worker leases;
- stale-owner rejection;
- bounded retries and provider pools;
- explicit unknown notification state;
- isolated semantic indexer;
- exact supervisor child ownership and restart budget.

### Untrusted-content handling

- MIME and HTML normalization;
- bounded email text;
- system/data prompt separation;
- strict provider output validation;
- conservative account/revision-aware feedback retrieval;
- React text rendering without raw HTML sinks.

### Privacy boundaries

- shared best-effort masking;
- sender omission from supported external payloads;
- local semantic embeddings;
- application-level local-only routing;
- verified asset manifests;
- safe diagnostic output.

## Current architecture observations

The native runtime owns four processes:

1. semantic indexer;
2. API;
3. Gmail/classification worker;
4. frontend.

The semantic indexer waits for Google connection before reading mail or initializing semantic assets. This preserves the explicit OAuth boundary while keeping embedding startup isolated from Gmail.

The current Compose demonstration runs API, worker, and frontend only. It does not include the semantic indexer and therefore does not establish native semantic-search parity.

## Dependency policy

Python dependency closure and npm integrity metadata are retained in lock files. Dependency status changes over time and must be re-audited for each release.

The current unexpected failure is a declaration-policy mismatch, not an observed runtime vulnerability: the installed Lucide React version matches the lock, but <code>frontend/package.json</code> permits a compatible range. Resolve the declaration before publishing a fully passing release claim.

## Remaining risks

- Local data is not encrypted by the application.
- Masking is not anonymization.
- Provider retention and deletion are outside local control.
- Desktop OAuth is not suitable for a public hosted service.
- Local-only routing is not an operating-system firewall.
- Statistical models remain vulnerable to misclassification and adversarial language.
- The current personal training evidence is insufficient for production-quality claims.
- The container topology lacks the semantic indexer.
- Live provider reliability requires deliberate operator-owned testing.

## Release requirements

Before release:

1. Resolve the exact dependency-declaration failure.
2. Rerun the complete backend suite.
3. Rerun frontend unit, lint, build, and browser checks.
4. Run dependency and clean-release audits.
5. Verify private files are excluded from source and Docker context.
6. Use synthetic demonstrations and screenshots.
7. Reconfirm OAuth-gated semantic indexing and account-switch isolation.
8. Preserve the limitations stated in the public README, security policy, and privacy policy.

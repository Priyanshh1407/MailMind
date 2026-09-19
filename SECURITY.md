# Security policy

## Supported deployment

MailMind supports one trusted user on a local workstation. The API and UI bind to
loopback in the native launcher. The supplied Compose file publishes API/UI ports
to host loopback and runs local-only mode. A public, remote or multi-user service is
outside the supported threat model.

## Controls

- A local pairing code creates an HttpOnly, SameSite=Strict session cookie.
- State-changing API calls require an allowed local Origin and CSRF token.
- Account generations cancel stale work; SQL, jobs and vector metadata are account scoped.
- Provider calls have bounded concurrency/time budgets and strict structured output.
- Email and retrieval text is bounded JSON data, separated from system instructions.
- Retrieval requires current revisions, matching account metadata, finite distance,
  independent support and a conservative vote before it can classify.
- Chroma is permitted only as an embedded Rust PersistentClient. MailMind does not
  start or publish the Chroma server affected by the reviewed server/RBAC advisories.
- Local-only mode blocks application provider routes and verifies cached assets.
- Private files are excluded by Git and Docker allowlists.

## Reporting and handling sensitive material

Do not put credentials, real email, tokens, exports, databases or proprietary model
weights in a public issue. Remove secrets from reproduction steps. Rotate any key
that was exposed before treating a code fix as sufficient.

## Known limits

Prompt-injection controls reduce instruction/data confusion; they cannot prove a
model will classify adversarial natural language correctly. Regex masking is not
anonymization. Local-only mode is an application boundary rather than an OS firewall.
Dependency advisories, provider behavior and model quality change over time. Run
`scripts.audit_dependencies`, all local tests and the clean-release checks before a
release. Read `docs/SECURITY_MAINTENANCE_REPORT.md` for the latest reviewed evidence.

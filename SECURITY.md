# Security policy

## Supported deployment

MailMind supports one trusted user on one local workstation. The native API and frontend bind to loopback. The application is not designed to be exposed directly to a public network or used as a hosted multi-tenant service.

The Docker Compose demonstration also publishes only loopback ports and runs in local-only mode.

## Trust boundaries

MailMind treats the local browser, FastAPI, Gmail/OAuth, email content, cloud providers, local models, SQLite, vector stores, workers, configuration, logs, exports, and model assets as separate trust boundaries. Email and retrieved feedback text are untrusted data.

## Implemented controls

### Local browser session

- Only the allowed loopback dashboard origin can open a session.
- The random session token is represented server-side by a hash and sent in an HttpOnly, SameSite=Strict cookie.
- State-changing requests require an allowed Origin and CSRF token.
- Trusted-host checks reject unexpected hosts.
- Sessions expire and are invalidated by restart and account-generation changes.
- The trusted dashboard can open a fresh local session automatically; there is no pairing-code workflow.

### Account isolation

- Mail, feedback, jobs, notification state, credentials, and vector identifiers are account scoped.
- A generation fence invalidates slow work after logout, disconnect, switching, deletion, or restart.
- Gmail message IDs are not treated as globally unique across accounts.
- The dashboard rejects mixed-account snapshots.
- Account deletion preserves unrelated accounts.

### OAuth and Gmail

- Gmail uses Google OAuth instead of mailbox passwords.
- Credentials are stored in account-specific local files.
- Credential refresh and Gmail requests are bounded.
- A disconnected worker does not launch OAuth.
- Late OAuth completion cannot restore credentials after cancellation.
- Google permission revocation is separate from deleting local credentials.

### Provider and prompt safety

- Email and retrieval text are normalized, bounded, and placed in an untrusted JSON envelope.
- System instructions remain separate.
- Provider responses must match the strict three-category schema.
- Unknown labels, commands, malformed JSON, and unavailable providers do not become successful Spam classifications.
- Provider pools and fallback behavior are isolated and bounded.
- Best-effort masking and sender omission reduce outgoing data.

These controls reduce instruction/data confusion; they do not make statistical models prompt-injection proof.

### Durable background work

- SQLite records queued, running, retry, complete, and dead states.
- Expiring leases prevent overlapping workers.
- Stale owners cannot commit after losing their generation fence.
- Retry counts and delays are bounded.
- Ambiguous Telegram delivery is stored as unknown and is not automatically resent.
- Mark-read is a separate durable stage and defaults to off.
- The isolated semantic indexer cannot block Gmail polling.

### Retrieval and vector storage

- Chroma is used only through the embedded persistent client.
- No Chroma server or port is started.
- Search and feedback use separate local directories.
- Feedback candidates are revalidated for account ownership, current revision, valid label, finite distance, and independent support.
- Search candidates are filtered through authoritative account-owned SQLite rows.
- SQLite remains authoritative; vectors are derived and retryable.

### Local-only mode

Local-only mode blocks application routes to Gmail, Gemini, Groq, and Telegram. It requires verified pre-provisioned assets and fails closed when required assets are absent or changed.

This is an application-routing boundary, not an operating-system firewall.

### Sensitive files

Ignore rules and the Docker build allowlist exclude expected private paths. Operators must still protect the workstation, backups, screenshots, copied exports, and manually created model or data files.

## Data at rest

MailMind does not implement application-level encryption for SQLite, vector stores, OAuth files, or logs. Use operating-system access controls and full-disk encryption where appropriate.

Account deletion is managed application deletion, not forensic secure erasure. It cannot remove backups, filesystem snapshots, exports, copied files, delivered Telegram messages, or data already sent to a provider.

## Known limitations

- Regex masking is not anonymization.
- Local model quality is not production validated.
- Provider retention, policies, outages, and quotas remain external.
- Desktop OAuth is not a hosted callback design.
- Loopback access is not endpoint security on an untrusted workstation.
- The Compose demo does not currently include the semantic indexer.
- Dependency and model risks change over time.
- No control guarantees correct classification of every adversarial message.

## Reporting a vulnerability

Do not include credentials, real Gmail content, tokens, databases, private models, or sensitive logs in an issue.

A useful report includes the affected version, a safe synthetic reproduction, expected and observed behavior, security impact, relevant account/external-operation context, and a suggested mitigation when known.

Rotate an exposed credential before considering a code correction sufficient.

## Security verification

~~~powershell
.\venv\Scripts\python.exe -m unittest tests.test_security_hardening -v
.\venv\Scripts\python.exe -m scripts.audit_dependencies
.\venv\Scripts\python.exe -m scripts.check_clean_release
~~~

Passing synthetic tests does not establish universal security. Review [Privacy and local-only](docs/PRIVACY_AND_LOCAL_ONLY.md), [Architecture](docs/ARCHITECTURE.md), and [Setup and release](docs/SETUP_AND_RELEASE.md) before using real mail.


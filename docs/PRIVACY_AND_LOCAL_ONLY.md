# Privacy and local-only policy

## What leaves the computer

| Destination | Why it is contacted in normal mode | Email data sent |
| --- | --- | --- |
| Google Gmail | Sign in, list/read messages, optional mark-read | OAuth requests, account/message IDs and read-state changes; Google already hosts this inbox. Gmail input is not masked before local ingestion. |
| Gemini | Primary cloud classification | Sender replaced by `[SENDER]`; masked subject/body; up to three masked retrieved precedents with category labels. |
| Groq | Cloud fallback | The same minimized email and precedents. Keys go in authorization headers, not the prompt. |
| Telegram | IMPORTANT alerts | Sender replaced by `[SENDER]`, masked subject and masked short body snippet. Bot/chat identifiers are necessary to deliver the alert. |
| Public asset servers | Explicit first-time provisioning in normal mode | Public model/embedding downloads. The normal embedding function may download if its cache is missing. Do not confuse local execution with guaranteed offline availability. |
| Local browser/API | Dashboard, pairing and predictions | Saved account data stays between the local browser and API. Pairing and CSRF checks still apply. |

The app does not guarantee provider-side retention limits or deletion. Check each provider's current account settings and terms before sharing sensitive data. Local purge cannot retract previous provider requests or delivered Telegram messages. Framework/server diagnostics outside the app's logger need separate operational review.

## Masking and safe exports

`src/privacy.py` is the shared, best-effort policy. It masks currency amounts with `$`, `₹`, Rs/Rs., INR or USD prefixes; full email addresses; VPA/UPI-like handles; PAN-shaped IDs; phone/long numeric sequences; and HTTP/HTTPS/www links. A phone or long number uses the generic `[ACCOUNT_NUM]` placeholder. Sender names are omitted entirely from external email payloads.

Email addresses are masked before VPA handles, so a domain suffix is not left behind. Non-string/null inputs become empty text. Mask before truncating outgoing snippets and retrieved examples. The cloud prompt retains category instructions separately from untrusted data.

This is minimization, not anonymization. Names inside subject/body, postal addresses, short IDs, unusual currencies, encoded identifiers, attachments and contextual clues can remain. Numeric/date false positives are possible. Regex cannot guarantee sensitive information is absent or preserve classification quality. Do not upload private mail merely because a masker ran.

The export CLI requires an explicit database, assigned account and fresh output path. Only labelled rows for that account are selected. Only subject, body and canonical human label are exported; subject/body are masked. Sender, account/message IDs, credentials, timestamps, histories and model metadata are omitted. Leading spreadsheet formula characters are neutralized with an apostrophe. Parent directories are created. Existing exports are never overwritten. Review exports before sharing. Exported labels do not imply independently reviewed dataset provenance.

## What is retained locally

SQLite retains original sender, subject/body, predictions, feedback revisions, processing attempts, notifications and jobs. Chroma holds account-scoped feedback text/labels and derived embeddings. Google OAuth credentials are account-specific local files. Application events contain allowlisted codes/types rather than mail or raw exception messages. The normal application is not an encrypted vault. Local disk permissions, encryption, backups and who can access the machine remain the operator's responsibility.

There is no automatic expiry policy. Data stays until deliberately deleted. Logout ends the session and pauses work; saved mail/credentials remain. Disconnect stops Gmail access and deletes local Google credentials; mail/feedback remain. Delete-account removes that account's managed mail/histories/jobs/vectors/credentials while preserving other accounts. Failed vector deletion remains pending and retryable; the UI does not claim complete success. The selected account name can remain in local account state. Delete-legacy is a separate explicit operation for unassigned records/archives/shared old credentials. See `OPERATING_POLICIES.md` and the Phase 2 review.

Google permission revocation is separate from deleting local credentials. Backups, filesystem snapshots, manual CSV exports, copied training datasets and model assets outside managed account storage are not removed by account purge. Purge is not a promise of forensic secure erasure. Do not manually delete a database while the app is using it.

## Local-only contract

Set `MAILMIND_LOCAL_ONLY=true` in the process environment before starting API and worker. This bypasses `.env` loading, so cloud credential files need not be read. If the flag comes only from `.env`, the file must first be read to discover it; routing still becomes local, but that does not meet a no-credential-file-read claim. Unknown flag values fail instead of quietly selecting online behavior.

Local-only mode does not construct cloud email prompts, initialize cloud clients, call Gemini/Groq, run Google authentication/refresh/list/read/write operations, or deliver Telegram alerts. The API rejects Google authentication and inbox-check requests. UI controls disable those actions and telemetry labels disabled providers. The worker classifies already saved, due tasks locally for an existing active account. It does not fetch new Gmail messages.

Standalone prediction works for a paired local session even without Google connected. On a fresh offline demo profile, there is no inbox to fetch; use `/predict` after pairing. Full saved-message processing requires already assigned account records and an active, unpaused account state. Logout/disconnect still pause worker processing. This phase does not add an offline account-import or account-picker feature.

IMPORTANT messages whose alerts were not sent remain blocked for user attention; they are not falsely marked notified or silently marked read. Already sent alerts and unknown/sending states remain intact, preventing duplicate delivery after mode changes. Local-only mode never executes Gmail mark-read, even if the old setting requested it. Switching back online does not automatically recover dead/blocked tasks: inspect saved state and deliberately use the existing retry/resolution actions where appropriate.

The API/worker remain local processes and the UI still needs localhost HTTP. Removing internet access is compatible with local-only mode; shutting down localhost access is not. Fonts use locally available system fonts/fallbacks. UI code has no third-party avatar seeds or font requests; static JS/CSS/favicon are served locally. No install/build/download is guaranteed to work without internet. Do not claim a service-worker-cached app or offline OAuth.

## Verified assets and missing-file behavior

The asset packager copies only required files from an explicitly chosen classifier and an already cached public embedding. It downloads nothing. Its manifest records SHA-256 hashes for both roles and the default all-MiniLM-L6-v2 embedding identity. Paths must stay inside the declared asset root. Required files, integrity and classifier path are checked before local loading. The inventory proves local integrity/availability, not independent upstream authenticity.

The local-model factory sets Hugging Face offline and telemetry-disable flags before framework loading. Restart processes when changing modes; these process flags are not silently undone. The local embedding adapter retains the benchmark's default model and cosine behavior but overrides download hooks to fail. Chroma anonymized telemetry is disabled in both modes. A missing or changed classifier leaves inference unavailable; missing embeddings reject retrieval/indexing and can fall back to the verified classifier. No cloud fallback occurs. Initialization/model loading may be slower because large files are hashed. Assets must be provisioned before disconnecting internet. After moving the package, rebuild a manifest/package for the new paths.

The selected demonstration checkpoint has synthetic-only evidence and is not production validated. Phase 7's urgent personalization regressions remain unresolved; local-only mode does not fix ML quality.

For underlying behavior, see [Chroma embedding documentation](https://docs.trychroma.com/docs/embeddings/embedding-functions) and [Hugging Face offline/telemetry variables](https://huggingface.co/docs/huggingface_hub/main/package_reference/environment_variables). The adapter is checked against installed Chroma; clean-machine/version compatibility remains Phase 9.

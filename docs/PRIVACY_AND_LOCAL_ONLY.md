# Privacy and local-only policy

## Summary

MailMind is local-first, not exclusively local in normal mode. Gmail already hosts the inbox, and configured cloud providers may receive minimized, masked text for classification. Semantic search and feedback retrieval use local embedded stores.

No account mail is accessed before successful Google OAuth connection. The semantic-indexer process may be running, but it waits without reading saved mail, loading embeddings, or opening the search collection until the selected account is connected.

## Data destinations in normal mode

| Destination | Purpose | Data involved |
| --- | --- | --- |
| Google Gmail | OAuth, profile, message discovery/read, optional mark-read | OAuth requests, account/message identifiers, Gmail content, and optional read-state changes |
| Gemini | Primary and fallback classification | Sender omitted or replaced; masked and bounded subject/body; optionally bounded masked feedback examples |
| Groq | Provider fallback | The same minimized classification payload |
| Telegram | Optional Important alerts | Minimized masked subject and short body snippet plus delivery identifiers |
| Local API/browser | Dashboard and controls | Account-scoped saved data over loopback |
| Embedded Chroma | Feedback retrieval and semantic search | Local account-scoped derived text and embeddings |

Provider keys are carried through configuration or authorization mechanisms, not inserted into email prompts.

## Masking

The shared privacy policy in <code>src/privacy.py</code> applies best-effort masking before supported external classification, Telegram, and export flows. It targets email addresses, payment handles, phone and long numeric identifiers, supported currency-prefixed values, PAN-shaped identifiers, and web links.

Sender identity is omitted from supported outgoing email payloads.

Masking is minimization, not anonymization. Names inside subjects or bodies, addresses, short identifiers, attachments, unusual formats, and contextual clues can remain. Regex can also produce false positives. Review provider policies before using sensitive mail.

## Local retention

SQLite stores account-scoped source mail, ingestion cursors, predictions, feedback, processing attempts, notification state, semantic-index reconciliation, source-labelled analysis, Action Center rows, reminders, token-usage events, intelligence-backfill state, and worker/account runtime state.

Token events contain provider/operation/outcome categories and non-negative counts when available. They do not contain prompts, email text, action text, or provider responses. Provider-billed and locally processed tokens are reported separately and are not interchangeable cost figures.

Embedded Chroma stores:

- <code>data/chroma_db</code> for feedback retrieval;
- <code>data/search_chroma_db</code> for semantic inbox search.

The search embedding document is capped at 6,000 characters. This does not shorten the source email stored in SQLite or the classification input.

OAuth credentials are stored in account-specific local files. MailMind does not encrypt these stores at the application layer.

## Retention and deletion

MailMind has no automatic expiry policy.

| Action | Local effect |
| --- | --- |
| Stop processing | Pauses work and ends the local session; mail and credentials remain |
| Disconnect Google | Removes the selected account’s local OAuth credential; saved mail and feedback remain |
| Delete this account data | Removes that account’s managed mail, jobs, histories, vectors, and credentials |
| Delete old unassigned data | Separately removes explicit legacy and unassigned storage |

Google-side permission revocation must be performed separately. Application deletion cannot remove backups, snapshots, manual exports, copied models or datasets, delivered Telegram messages, or earlier provider requests.

## Account switching

Switching accounts advances the generation fence before the new account becomes active. Old asynchronous work cannot commit into the new generation. Dashboard responses and vector candidates are rechecked against the selected account.

Only one account is active in the dashboard at a time.

## Local-only contract

Set <code>MAILMIND_LOCAL_ONLY=true</code> in the process environment before startup for the strongest documented boundary.

Local-only mode:

- does not authenticate to or read Gmail;
- does not construct or send Gemini or Groq prompts;
- does not initialize cloud provider clients;
- does not send Telegram alerts;
- rejects Google and inbox actions through the API;
- uses explicitly prepared local model and embedding assets;
- disables cloud fallback; and
- reports external providers as disabled.

The UI and API still communicate over localhost. Local-only mode is an application routing boundary, not an operating-system firewall.

## Local-only saved work

Local-only mode can process eligible already-saved tasks for an existing account when local runtime state permits it. It cannot fetch new Gmail mail.

Important items whose alert cannot be delivered remain blocked or reviewable. They are not falsely marked notified or read.

Switching back to normal mode does not blindly repeat ambiguous external sends. Inspect the saved recovery state and use explicit recovery controls.

## Verified assets

The asset packager copies an explicitly selected classifier and cached embedding package into a new directory. Its manifest records paths and SHA-256 hashes.

Verification establishes that required files exist, paths remain inside the asset root, and files match recorded hashes. It does not establish upstream authenticity, licensing, or model accuracy.

Missing or modified required assets fail closed. No cloud fallback is used in local-only mode.

## Exports

Safe export tooling requires an explicit database, assigned account, fresh output path, and human-labelled rows.

Exports omit sender, account/message identifiers, credentials, timestamps, histories, and provider metadata. Subject and body are masked, and spreadsheet formula prefixes are neutralized. Review every export before sharing.

## Semantic-index privacy

Semantic indexing is local derived work. It starts only after Google connection, operates on the selected account, and is fenced during account, authentication, and deletion transitions.

Semantic embeddings are not sent to Gemini or Groq. Search falls back to lexical SQLite matching when the derived store is unavailable.

## Saved-mail intelligence backfill

Backfill is explicit and bounded. It reads eligible mail already saved in the selected account's SQLite database; the backfill admission endpoint itself does not call Gmail. In normal mode, classification may send the same bounded, minimized, masked content to configured Gemini or Groq providers, so backfill is not an offline operation.

Backfill does not send historical Telegram alerts, mark Gmail messages read, or schedule automatic action reminders. It can create local analysis, action, and privacy-safe token rows. Local-only mode keeps cloud providers disabled, but requires verified local assets and compatible saved work.

## Operator responsibilities

- Protect the workstation with OS access controls and disk encryption.
- Keep configuration, logs, databases, and private data outside version control.
- Review screenshots and logs before sharing.
- Check current provider retention and account settings.
- Revoke Google access separately when required.
- Treat masking as exposure reduction, not a privacy guarantee.
- Do not claim production model quality without independent evaluation.


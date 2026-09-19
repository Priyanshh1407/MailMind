# Security Maintenance Report

**Review date:** 19 September 2026  
**Supported deployment:** one trusted user on one workstation

## Result

The dependency lock was rebuilt in a new virtual environment against current upstream releases. The exact Python closure contains 120 packages. `pip check` passed, all 345 strict backend tests passed, all 23 frontend unit tests passed, all 16 browser tests passed, and the frontend production build and lint passed. The clean Linux container build and runtime scenario also passed.

This result does not certify MailMind for public hosting or multiple users. The final checks used synthetic messages and public cached demo assets. Real Gmail/OAuth, Gemini/Groq, Telegram delivery, provider retention behavior, and model accuracy on a real independently labelled inbox remain unvalidated.

## Dependency security

- Python direct requirements and the exact transitive lock were refreshed.
- PyTorch 2.14.0 is installed from the official CPU wheel index before the PyPI lock.
- The OSV batch audit checked all 120 locked Python package/version pairs.
- OSV returned zero unexpected findings.
- npm audit returned zero frontend findings.
- Eight OSV records remain for ChromaDB 1.5.9. They represent four advisory families plus database aliases and are accepted only for the embedded boundary described below.
- `scripts/audit_dependencies.py` fails if OSV returns an advisory outside the reviewed policy or if a policy ID becomes stale.
- `config/security-advisory-policy.json` is a narrow exception list, not a claim that ChromaDB is generally safe.

The older local phase audit contained findings for the previous lock. It is generated evidence, is ignored by Git, and is superseded by this review.

## ChromaDB assessment

Upstream lists no patched release for the four reviewed ChromaDB 1.5.9 advisory families:

| Advisory | Upstream affected surface | MailMind boundary |
| --- | --- | --- |
| [GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c) | Pre-authentication code injection through a Chroma server collection endpoint | No Chroma HTTP server or collection-creation endpoint is started or exposed |
| [GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf) | Authenticated server code injection with collection-update permission | MailMind has no Chroma users, RBAC API, or remote update endpoint |
| [GHSA-2wm9-hf6c-p5cr](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr) | Cross-tenant collection access in the server authorization path | MailMind does not use Chroma tenants as a security boundary; application account and revision checks are re-applied before a result may vote |
| [GHSA-xph7-9rjv-w5fr](https://github.com/advisories/GHSA-xph7-9rjv-w5fr) | Simple RBAC scope checks in the server | The SimpleRBAC server provider is not configured or reachable |

MailMind creates only `chromadb.PersistentClient` and now pins `chromadb.api.rust.RustBindingsAPI`. Startup fails closed if a different backend is returned. Compose has no Chroma service or published Chroma port. The vector index is derived state; SQLite remains authoritative, and every retrieved row is checked for account, revision, label, distance, and independent evidence.

This is a deployment-specific risk acceptance. The vulnerable package code is still installed. Do not switch to `HttpClient`, start a Chroma server, add remote tenants, or host this stack without removing the exception and performing a new review.

## Prompt-injection and RAG controls

Email text and retrieved precedents are treated as untrusted data:

- System instructions and user data are separate.
- The user payload is a bounded JSON object.
- Sender, subject, body, and precedents are normalized, redacted, and truncated.
- At most three unique, schema-valid precedents are sent.
- Provider output must be one JSON object with one known category; duplicate keys, trailing text, commands, and unknown labels are rejected.
- Retrieval rows with malformed shapes, non-finite distances, stale revisions, wrong accounts, or invalid labels are ignored.
- Replayed or correlated examples cannot inflate independent support.
- Retrieval abstains unless the configured support and majority rules pass.
- The React source contains no `dangerouslySetInnerHTML`, `innerHTML`, `document.write`, `eval`, or `new Function` sink.

The focused adversarial suite contains 13 tests and passed. These controls constrain instructions and evidence. They do not prove that a statistical classifier will understand every adversarial email.

## Runtime and release checks

| Check | Result | Scope |
| --- | --- | --- |
| Upgraded isolated virtual environment | Passed | Exact lock installed; `pip check`; 345 strict backend tests |
| Prompt/RAG security suite | Passed 13/13 | Synthetic hostile messages, poisoned precedents, malformed retrieval, backend pinning |
| Frontend | Passed | 23 unit tests, build, lint, 16 Chrome browser scenarios |
| Public offline assets | Passed | Real packaged synthetic classifier and embedding cache; provider credentials empty; Python external networking blocked |
| Native launcher | Passed | API/worker/frontend ready; unauthorized stop rejected; graceful owned-process shutdown |
| Docker Compose | Passed | Clean CPU Linux build; UID 10001; local-only actions rejected; persistence and restart/session behavior verified |
| Python OSV policy audit | Passed | 120 packages; zero unexpected findings; eight accepted Chroma records |
| npm audit | Passed | Zero findings |
| Fresh Windows source snapshot | Limited | PyTorch installed, then the remaining lock download exceeded the helper's fixed 600-second timeout before tests began |

The fresh-snapshot timeout is an installation/download limit, not a passing result. Re-run `python -m scripts.check_clean_release` on a stable connection before a release tag. The separately created clean maintenance environment and clean container both installed the refreshed lock successfully.

## Remaining limits

- No real inbox or independently labelled private corpus was read.
- The personalized model candidate was rejected because urgent-message regressions worsened.
- No live Google OAuth/Gmail operation was performed.
- No live Gemini, Groq, or Telegram operation was performed.
- Regex masking is best effort and is not anonymization.
- Local-only routing blocks application adapters; it is not an operating-system egress firewall.
- Local databases are not an encrypted vault.
- The supported scale is one local user with SQLite and one worker.

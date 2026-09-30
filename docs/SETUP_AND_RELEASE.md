# Setup and release guide

## Supported environment

The current native target is Windows x64, CPython 3.14, Node.js 22.x, CPU inference, and one trusted local user. The API and dashboard use loopback ports 8000 and 5173.

Linux is used by the local-only container demonstration. Remote hosting, macOS, ARM, GPU execution, and concurrent multi-user deployment are not validated.

## Clean installation

From the project root in PowerShell:

~~~powershell
py -3.14 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt --index-url https://pypi.org/simple
.\venv\Scripts\python.exe -m pip check

Set-Location frontend
npm ci
npm run build
Set-Location ..
~~~

<code>requirements.lock</code> records the exact Python dependency closure. <code>frontend/package-lock.json</code> records npm integrity metadata. These improve repeatability but do not replace recurring vulnerability review.

## Configuration

Copy the example only when needed:

~~~powershell
Copy-Item .env.example .env
~~~

Never commit environment files, OAuth JSON, tokens, databases, launcher logs, exports, private mail, or private model checkpoints.

### Core settings

| Variable | Default | Purpose |
| --- | --- | --- |
| <code>MAILMIND_LOCAL_ONLY</code> | <code>false</code> | Disable Gmail, cloud providers, and Telegram routing |
| <code>MAILMIND_AUTO_MARK_READ</code> | <code>false</code> | Opt in to the durable mark-read stage |
| <code>MAILMIND_POLL_INTERVAL_SECONDS</code> | <code>5</code> | Worker interval; values below five are rejected |
| <code>MAILMIND_MAX_PENDING_TASKS</code> | <code>100</code> | Maximum active queued, running, and retry tasks |
| <code>MAILMIND_RESUME_PENDING_TASKS</code> | <code>50</code> | Lower threshold used by bounded intake |
| <code>MAILMIND_MODEL_PATH</code> | optional | Explicit local classifier path |
| <code>MAILMIND_SHADOW_MODEL_PATH</code> | optional | Normal-mode three-class shadow checkpoint |
| <code>MAILMIND_ASSET_MANIFEST</code> | optional | Verified local-only asset manifest |
| <code>MAILMIND_TOKEN_COLLECTION_ENABLED</code> | <code>true</code> | Record privacy-safe token events |
| <code>MAILMIND_TOKEN_ANALYTICS_VISIBLE</code> | <code>false</code> | Expose usage analytics after token collection is observed |
| <code>MAILMIND_EXPLANATIONS_VISIBLE</code> | <code>false</code> | Expose source-labelled explanations |
| <code>MAILMIND_ACTION_EXTRACTION_ENABLED</code> | <code>false</code> | Persist action candidates and enable explicit saved-mail backfill |
| <code>MAILMIND_ACTION_REMINDERS_ENABLED</code> | <code>false</code> | Enable dashboard action reminders |
| <code>MAILMIND_TELEGRAM_ACTION_REMINDERS_ENABLED</code> | <code>false</code> | Separately opt in to Telegram action reminders |

Provider variables are listed in <code>.env.example</code>. Never place provider keys in frontend source or documentation.

### Intelligence rollout order

Keep schema migrations and token collection first. Observe synthetic token events, then enable explanations, validate controlled action types with reminders off, enable Action Center controls, enable dashboard reminders, and only then opt in to Telegram action reminders. Saved-mail backfill is the final, explicit step. Restart API and worker together after flag changes.

The dashboard backfill button requests 20 eligible saved messages at a time. The API hard limit is 100, and admission also obeys the global active-task cap. Backfill never performs historical Telegram notification, Gmail mark-read, or automatic-reminder side effects.

## Google OAuth

1. Create or select a Google Cloud project.
2. Enable the Gmail API.
3. Configure the OAuth consent screen.
4. Add the intended account as a test user when required.
5. Create a **Desktop app** OAuth client.
6. Save the downloaded JSON as ignored root <code>credentials.json</code>.
7. Start MailMind and choose **Connect Google**.

MailMind requests Gmail modify scope because optional read-state changes are supported. Automatic mark-as-read still defaults to off.

The OAuth flow is designed for an interactive local desktop. It is not a hosted callback architecture.

## Frontend build

The native launcher serves <code>frontend/dist</code>, not Vite hot reload.

After frontend changes:

~~~powershell
Set-Location frontend
npm test -- --run
npm run lint
npm run build
Set-Location ..
~~~

For development only:

~~~powershell
Set-Location frontend
npm run dev
~~~

## Native startup

~~~powershell
.\start_all.bat
~~~

Equivalent direct command:

~~~powershell
.\venv\Scripts\python.exe -m scripts.launch start
~~~

The supervisor starts the semantic indexer, API, Gmail/classification worker, and built frontend. The indexer readiness marker means the process is ready to wait; it does not mean mail has been accessed. Mail and embedding initialization begin only after Google connection.

## Status and diagnostics

~~~powershell
.\venv\Scripts\python.exe -m scripts.launch status
.\venv\Scripts\python.exe -m scripts.runtime_diagnostics
~~~

Diagnostics report counts, timestamps, safe status codes, configuration presence, schema version, and semantic-index progress. They do not print Gmail content, account identifiers, OAuth tokens, or provider keys.

Owned logs are stored under <code>.run</code>:

- <code>indexer.log</code>
- <code>api.log</code>
- <code>worker.log</code>
- <code>frontend.log</code>

## Stop

~~~powershell
.\venv\Scripts\python.exe -m scripts.launch stop
~~~

The authenticated loopback stop request applies only to the recorded supervisor and its exact owned children. The supervisor never searches for arbitrary processes by executable name or port.

## Restart behavior

The supervisor can restart the indexer, worker, or frontend at most three times inside a 60-second window. An API exit or exhausted restart budget shuts down the owned service set.

Graceful shutdown uses child stdin. A bounded terminate/kill fallback applies only to the exact owned child objects.

## Local-only preparation

Local-only mode requires pre-provisioned classifier and embedding assets.

~~~powershell
.\venv\Scripts\python.exe -m scripts.prepare_offline_assets --model <classifier-directory> --embedding-cache <cached-embedding-directory> --output models/offline-demo-v1
~~~

Set local-only mode in the process environment before startup for the strongest documented boundary. Verified manifests check presence and SHA-256 integrity; they do not establish publisher identity, licensing, or model quality.

## Docker Compose demonstration

The current Compose stack is local-only and loopback-published:

~~~powershell
.\venv\Scripts\python.exe -m scripts.write_container_manifest
docker compose up --build --wait
docker compose down
~~~

It runs API, worker, and frontend services. It does not currently run the native semantic-indexer service, so semantic-search indexing parity with the native runtime is not claimed.

The stack is CPU-only, nonroot, and persists data in a named volume. Running <code>docker compose down --volumes</code> deletes that demo volume and must be deliberate.

## Verification

Backend:

~~~powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
.\venv\Scripts\python.exe -m scripts.audit_dependencies
~~~

Frontend:

~~~powershell
Set-Location frontend
npm test -- --run
npm run lint
npm run build
npm run test:browser
Set-Location ..
~~~

Integration helpers:

~~~powershell
.\venv\Scripts\python.exe -m scripts.verify_launcher
.\venv\Scripts\python.exe -m scripts.verify_local_only --model <classifier> --embedding-cache <cache>
.\venv\Scripts\python.exe -m scripts.verify_containers
.\venv\Scripts\python.exe -m scripts.check_clean_release
~~~

At the 30 September 2026 automated verification, all 526 backend tests, 48 frontend unit tests, 25 browser scenarios, frontend lint, and the production build passed.

## Release checklist

- [ ] Resolve every unexpected test failure.
- [ ] Run <code>pip check</code>.
- [ ] Run the complete backend suite.
- [ ] Run frontend unit, lint, build, and browser suites.
- [ ] Run dependency and clean-release checks.
- [ ] Confirm secrets, private data, logs, databases, and model assets remain excluded.
- [ ] Use only synthetic screenshots and demonstrations.
- [ ] Confirm the launcher starts four native services and stops only its owned set.
- [ ] Confirm semantic indexing waits for Google connection.
- [ ] Confirm account switching cannot expose the previous account.
- [ ] Follow the staged intelligence rollout order; keep Telegram action reminders opt-in.
- [ ] Confirm explicit backfill remains bounded and lower priority than live/ordinary backlog work.
- [ ] Complete the controlled live validation checklist before claiming live OAuth, Gmail, provider, or Telegram validation.
- [ ] Keep current limitations visible in public documentation.
- [ ] Do not claim live-provider reliability or production model accuracy without deliberate evidence.

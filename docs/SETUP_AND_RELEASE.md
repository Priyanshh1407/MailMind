# Setup, assets and release guide

## Installation and ownership

Use the README's x64 Python 3.14 CPU and Node 22.17.1 steps. The dependency lock
is an exact version closure, not a wheel-hash lock or guarantee of byte-identical
builds. CPU wheels differ by OS. Public images/action major tags can change within
their version; package updates must be followed by release checks. Do not silently
install unrelated packages from an old pip freeze.

The supervisor starts from the project root, checks ports 8000/5173, writes a
private state/control token under `.run`, starts API, waits for readiness, then
starts worker and built UI. It retains Popen objects for those exact children.
Stop validates supervisor PID creation time, cwd and module identity, authenticates
its control request, and drains owned services over stdin. A reused PID is never
signalled; stale state alone can be removed. An unresponsive owned child gets a
reported forced fallback after the grace deadline. Persisted ambiguous notification
state still requires careful recovery. Abrupt supervisor exit closes stdin pipes;
service wrappers stop on EOF. Do not edit launcher state to manage other processes.

The API does not need the standalone `setup_db` command first; its lifespan
initializes/migrates the database. Directory creation already happens in schema,
export, dataset conversion and asset/report scripts. Synthetic checks use temporary
databases. Your real `.env`, Google JSON, tokens and existing databases/models
were not used for release validation.

## Google and optional providers

Gmail is a desktop flow. Follow [Google's quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python)
for an enabled API, consent screen and Desktop OAuth JSON. Put it in ignored
root `credentials.json`. Do not put provider keys in frontend source. Leave optional
Gemini/Groq/Telegram variables empty to show unconfigured state. Missing cloud
classification does not silently consume mail. Gmail modify permission is requested;
auto-mark-read remains opt-in and alerts must succeed first in normal mode.

Native desktop sign-in is not a remote-server/container callback design. The
Docker demo disables Google/cloud/Telegram instead of suggesting hosted OAuth
works. Google consent restrictions and provider account access must be checked
for a real demonstration by the developer, not through a synthetic test.

## Model acquisition and release strategy

There is no trustworthy public MailMind production checkpoint to download in this
repository. The old private checkpoints remain local/ignored. Do not publish
personalization weights trained on private mail just because raw examples are absent;
models can still retain sensitive information.

Tracked acquisition is **rebuild from the original CC0 synthetic fixture**:

```powershell
venv/Scripts/python.exe -m scripts.evaluate_priority --output models/new-demo --report-dir docs/evaluation/new-demo
```

This trains new explicitly three-category tiny models, not the original pretrained
weights. The scope stays synthetic-only. Model versions, split manifests, predictions,
metrics, training histories and safetensors hashes are recorded. Base/personal
quality is insufficient for promotion, particularly urgent regressions. Existing
outputs are not overwritten. For a future approved release, publish a model card,
verified source/license, independent evaluation, mappings, immutable version and
weight checksum; require explicit selection. No future production download is
invented here.

The default runtime path remains the old `models/MailMind-Final`; missing or binary
checkpoints show unavailable/abstain. Rebuilding a demo does not change that path.
The local-only example explicitly selects a separate synthetic base model and data
folder. It is an opt-in demonstration, not default promotion.

## Embedding provisioning and packaging

If Chroma's default public cache is missing, provision it deliberately while online
with synthetic text only:

```powershell
venv/Scripts/python.exe -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()(['Public synthetic asset check'])"
```

Normal Chroma embedding behavior can download public assets; see its
[documentation](https://docs.trychroma.com/docs/embeddings/embedding-functions).
Do not run this as an offline availability test. The local-only adapter disables
downloads and requires a verified cache instead.

Choose a fresh package destination:

```powershell
venv/Scripts/python.exe -m scripts.prepare_offline_assets --model models/new-demo/base --embedding-cache "$env:USERPROFILE/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx" --output models/offline-demo-v1
venv/Scripts/python.exe -m scripts.write_container_manifest
```

On Linux the public cache is normally under `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx`;
pass it explicitly. A package has classifier/embedding folders, required files
and SHA-256 manifest entries. The native manifest records absolute local paths;
rebuild it after relocation. `manifest.container.json` verifies those same files
then rewrites roots to `/app/models/classifier` and `/app/models/embedding` for the
fixed read-only mount. Models are ignored by Git and excluded from image build
context. The package inventory proves local integrity, not independent authenticity.

## Docker and persistence

The Dockerfile has a CPU Python backend and a Node build/runtime UI. Backend
UID/GID 10001 owns its data directory; UI uses node. Compose mounts a named demo
volume and read-only assets, enables init, waits on healthy API and uses loopback
host ports. See [Docker's dependency/health documentation](https://docs.docker.com/compose/how-tos/startup-order/).
`docker compose down` retains data. Removing volumes is an explicit data deletion,
not a normal shutdown. Build context is allowlisted; private project files, user
models and venvs cannot enter it through the configured COPY operations.

Local-only is an application routing boundary, not an OS firewall. Containers
need internal/loopback networking for readiness and the browser. Worker on a fresh
demo volume is paused; a local prediction requires pairing but not Google. No
inbox-import feature is claimed. Native `/predict`/worker checks and the synthetic
engineering rehearsal are separate from production ML validation.

## Release checks and evidence

Read `docs/SECURITY_MAINTENANCE_REPORT.md` for the latest launcher, container, clean-environment, dependency, and security results. The clean helper copies allowed source roots
from the current working tree, excludes private roots, installs a fresh venv and
locked frontend, and runs checks.

The launcher smoke refuses occupied ports and uses temporary local-only data.
The container smoke uses an unpredictable unique Compose project, checks nonroot
UID, actual model inference, blocked external routes and saved mail after API restart and required browser re-pairing, then cleans only its synthetic project resources. Do not substitute
those helpers for a live Gmail or notification test.

GitHub CI was removed at the developer's request. Run the README's local
verification commands before sharing changes. No hosted checks, push, deployment
or live messaging were performed.

## Interview presentation

Start with `python -m scripts.demo_interview`. Explain that provider replies and
similarity are mocked while APIs, revisions, recovery and account guards are real.
Show failures before recovery, retained history, three current corrections and
account separation. Then present the measured negative promotion decision in the root README. Describe what you measured instead of promising that feedback
always improves a user's inbox. Explain the tradeoff between local-only privacy,
missing assets and inability to fetch Gmail or deliver alerts offline.

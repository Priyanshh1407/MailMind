# Test Guide

All automated tests use synthetic or temporary data. They do not read a real inbox, private model checkpoint, OAuth token, or provider credential.

## Backend

Run the complete strict suite:

```powershell
venv/Scripts/python.exe -m tests.run_baseline --strict
```

The current suite contains 345 tests. It covers account isolation, sessions and CSRF, MIME ingestion, durable jobs, provider failure handling, feedback and undo, privacy, local-only behavior, launch/release safety, prompt injection, and RAG poisoning boundaries.

Run the focused security tests:

```powershell
venv/Scripts/python.exe -m unittest tests.test_security_hardening -v
venv/Scripts/python.exe -m scripts.audit_dependencies
```

The dependency audit sends only package names and versions to OSV. Reviewed deployment-specific exceptions live in `config/security-advisory-policy.json`.

## Frontend

```powershell
cd frontend
npm test
npm run lint
npm run build
npm run test:browser
```

The frontend has 23 Node unit tests and 16 Playwright browser scenarios. If Playwright's bundled Chromium is not installed, either run `npx playwright install chromium` or set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to a compatible installed Chrome/Chromium executable.

## Integration checks

```powershell
venv/Scripts/python.exe -m scripts.verify_launcher
venv/Scripts/python.exe -m scripts.verify_local_only --model <classifier> --embedding-cache <cache>
venv/Scripts/python.exe -m scripts.verify_containers
venv/Scripts/python.exe -m scripts.check_clean_release
```

The asset check must use an explicitly public or synthetic package. The container verifier uses a unique Compose project and removes only its own resources. The clean-release verifier builds an allowlisted snapshot without private roots and installs it in a new temporary environment.

GitHub automation is intentionally absent. Run the checks locally before sharing a release.

## Validation limits

Passing these tests does not validate real-inbox model quality, live Google OAuth/Gmail behavior, cloud-provider retention, or Telegram delivery. Those require deliberate operator-owned validation.

# MailMind documentation

This directory contains the current operational and technical documentation for MailMind.

## Start here

| Document | Audience | Purpose |
| --- | --- | --- |
| [Project README](../README.md) | Everyone | Product overview, quick start, architecture summary, and verified limits |
| [User guide](USER_GUIDE.md) | Operators and demonstrators | Dashboard behavior, Gmail intake, search, feedback, recovery, and account actions |
| [Architecture](ARCHITECTURE.md) | Developers and reviewers | Process ownership, persistence, data flow, failure isolation, and security boundaries |
| [Setup and release](SETUP_AND_RELEASE.md) | Developers | Installation, configuration, startup, diagnostics, testing, and release checklist |
| [Privacy and local-only](PRIVACY_AND_LOCAL_ONLY.md) | Operators and security reviewers | Data destinations, local retention, deletion, and offline routing guarantees |
| [Security policy](../SECURITY.md) | Security reviewers | Supported threat model, implemented controls, known limits, and reporting guidance |
| [Security showcase](SECURITY_FEATURES_SHOWCASE.md) | Interviewers and developers | Source-backed explanation of the security engineering |
| [Frontend guide](../frontend/README.md) | Frontend developers | UI architecture, component responsibilities, testing, and accessibility |
| [Test guide](../tests/README.md) | Developers | Current suites, commands, coverage, and validation limits |
| [Controlled intelligence validation](CONTROLLED_INTELLIGENCE_VALIDATION.md) | Release operators | Privacy-safe live OAuth, Gmail, action, token, reminder, and backfill checklist |
| [Intelligence feature contract](INTELLIGENCE_FEATURE_CONTRACT.md) | Developers and reviewers | Stable action, explanation, reminder, token, and rollout behavior |
| [Contributing](../CONTRIBUTING.md) | Contributors | Required tests, privacy gate, documentation rules, and pull-request expectations |

## Documentation principles

- Current source behavior takes precedence over prose.
- Security, privacy, accuracy, and live-provider claims are deliberately bounded.
- Synthetic tests are never presented as proof of real-inbox model quality.
- Secrets, account identifiers, tokens, provider keys, and Gmail content must not appear in documentation or screenshots.
- Commands are written for the supported Windows PowerShell workflow unless stated otherwise.

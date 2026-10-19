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
| [Manual testing guide](MANUAL_TESTING.md) | Owner | Step-by-step checks of every new feature on your own Gmail, with the emails to send and the expected results |
| [Controlled intelligence validation](CONTROLLED_INTELLIGENCE_VALIDATION.md) | Release operators | Privacy-safe live OAuth, Gmail, action, token, reminder, and backfill checklist |
| [Intelligence feature contract](INTELLIGENCE_FEATURE_CONTRACT.md) | Developers and reviewers | Stable action, explanation, reminder, token, and rollout behavior |
| [Classifier evaluation](EVALUATION.md) | Reviewers | Recorded, replayable route comparison on the CC0 benchmark |
| [Scam and phishing evaluation](RISK_EVALUATION.md) | Reviewers | Reproducible precision, recall and ablation of the local scam checks |
| [Money extraction evaluation](LEDGER_EVALUATION.md) | Reviewers | Rules vs provider-assisted extraction, with the no-amounts privacy contract |
| [Ask your inbox evaluation](AGENT_EVALUATION.md) | Reviewers | The cited-answer agent on a synthetic mailbox (recording pending) |
| [Follow-up evaluation](FOLLOW_UP_EVALUATION.md) | Reviewers | Draft checks, facts and privacy (recording pending) |
| [Daily brief evaluation](BRIEF_EVALUATION.md) | Reviewers | Three agents vs one (recording pending) |
| [Red-team](REDTEAM.md) | Security reviewers | What a fully hijacked model could still get past MailMind's checks |
| [Self-recovery](SELF_RECOVERY.md) | Developers, reviewers and interviewers | How MailMind recovers from outages, crashes and restarts, and what still needs a human |
| [Provider resilience](RESILIENCE.md) | Developers and reviewers | Tested outcome for every provider fault type, generated from tests |
| [Performance](PERFORMANCE.md) | Developers | Measured list/search latency and bundle size, with decisions |
| [Contributing](../CONTRIBUTING.md) | Contributors | Required tests, privacy gate, documentation rules, and pull-request expectations |

## Documentation principles

- Current source behavior takes precedence over prose.
- Security, privacy, accuracy, and live-provider claims are deliberately bounded.
- Synthetic tests are never presented as proof of real-inbox model quality.
- Secrets, account identifiers, tokens, provider keys, and Gmail content must not appear in documentation or screenshots.
- Commands are written for the supported Windows PowerShell workflow unless stated otherwise.

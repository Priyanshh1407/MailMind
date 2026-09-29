# MailMind user guide

## Start MailMind

From the project root:

```powershell
.\start_all.bat
```

Open <http://127.0.0.1:5173>. Keep the supervisor terminal running.

Check status without exposing mail or credentials:

```powershell
.\venv\Scripts\python.exe -m scripts.launch status
.\venv\Scripts\python.exe -m scripts.runtime_diagnostics
```

Stop only MailMind-owned services:

```powershell
.\venv\Scripts\python.exe -m scripts.launch stop
```

## Connect Google

1. Open the dashboard.
2. Choose **Connect Google**.
3. Complete the Google OAuth consent flow.
4. The completion page attempts to close itself. If the browser prevents that, it redirects back to the dashboard.
5. Wait for the dashboard to show the connected account and active processing state.

MailMind does not read or process account mail before the Google connection succeeds. The local dashboard session opens automatically; there is no pairing code.

## Understand the dashboard

### Header

- **Systems healthy** opens current model, provider, worker, classification, and refresh information.
- The account menu provides connect, switch, disconnect, deletion, and stop-processing controls.

### KPIs

- **Saved emails** counts locally stored mail for the selected account.
- **Processed** counts messages that completed the workflow.
- **Feedback** separates confirmations and corrections.
- **Average classification** uses measured samples; it does not invent a value when none exist.

### Inbox work progress

The progress panel distinguishes saved work, waiting tasks, active classification, completed work, and items requiring review. It also shows the current historical batch and whether more old pages remain.

### Inbox intake

- **Sync new messages** queues a durable, rate-limited live Gmail check.
- **Fetch next 100** authorizes another historical page only after the current older batch is eligible.
- **Analyze up to 20 saved emails** explicitly enriches eligible saved mail after Action Center extraction is enabled.
- Active, live-pending, historical-pending, and last-live-sync values are separate.

These controls are not equivalent. Sync never grants more historical backlog. Saved-mail analysis never sends historical alerts, changes Gmail read state, or creates automatic reminders. Live mail remains ahead of backfill work.

## Intelligence tabs

### Inbox explanations

Expanded cards can show a bounded explanation, evidence signals, model/source label, and retrieval-use indicator. Explanations summarize decision signals; they are not hidden chain-of-thought. A correction changes the effective category but preserves the original analysis for audit.

### Action Center

The Action Center lists extracted replies, tasks, approvals, payments, meetings, and deadlines. Filters include open, due soon, overdue, snoozed, and completed. You can complete, dismiss, reopen, or snooze an action, and navigate to its account-owned source email.

Automatic dashboard reminders require explicit configuration. Telegram action reminders require a separate opt-in. Ambiguous, past-due, low-confidence, or date-unknown candidates do not silently create automatic reminders.

### Usage

Usage shows day, week, and month token totals with Recharts trends and an accessible data table. Provider-billed tokens and locally processed tokens are separate. Counts can be provider-reported, tokenizer-counted, estimated, or unavailable; they are usage measurements, not prices.

## Categories

| Lane | Meaning |
| --- | --- |
| Important | Personal, direct, urgent, security-sensitive, or action-requiring mail |
| Updates | Legitimate information that is useful but not urgent |
| Spam | Unwanted, promotional, deceptive, or junk mail |
| Review | Processing is incomplete, unavailable, ambiguous, or requires a human decision |

Review is a system state, not a trained model category.

## Search

Type in **Search sender, subject, body, or meaning**. Results update after a short pause.

- Short queries use text matching.
- Queries with three or more characters can use hybrid text and semantic search.
- Exact words remain ranked ahead of merely related meaning.
- Use the category dropdown to narrow results.
- **Search now** immediately submits the current text.
- The search panel scrolls normally with the page; it is not sticky.
- The dashboard reports semantic-index progress.

If semantic search is temporarily unavailable, MailMind continues with lexical results.

## Email cards

Each card provides:

- sender and received time;
- effective category;
- subject and saved snippet;
- review or processing reason when applicable;
- expandable classification details;
- feedback actions; and
- durable processing/feedback history.

### Confirm

Confirms that the displayed category is correct.

### Set label / Edit label

Selects a corrected category. The change is saved to SQLite immediately and becomes the effective dashboard category. Derived feedback indexing continues asynchronously.

### Undo

Withdraws the latest feedback revision and restores the latest valid prediction without deleting history.

### History

Shows saved feedback and processing events. The dashboard does not fabricate provider or delivery history.

## Recovery controls

A failed classification stage may expose **Retry unfinished step**.

An ambiguous Telegram state may expose:

- **Retry alert** after an explicit duplicate-risk confirmation; or
- **Confirm alert arrived** after the user verifies delivery.

MailMind does not silently retry an ambiguous external send.

## Account actions

### Switch Google account

Starts a new OAuth selection. Old-account work is fenced before new credentials can commit. After success, the dashboard loads only the selected account’s data.

### Stop processing

Ends the local session and pauses processing. Saved mail and credentials remain.

### Disconnect Google

Stops Gmail access and removes the selected account’s locally stored OAuth credential. Saved account data remains until separately deleted. Google-side permission revocation is a separate action in the Google account.

### Delete this account data

Removes the selected account’s managed local mail, jobs, feedback, derived vectors, and OAuth credential. Other accounts are preserved. This operation is intentionally explicit and cannot retract data previously sent to external providers.

### Delete old unassigned data

Removes legacy/unassigned records separately. It is not part of ordinary account deletion.

## Semantic indexing

The semantic-indexer process starts with MailMind but waits for Google connection before accessing account mail or initializing embedding/search storage.

After connection:

- pending rows are processed in batches of ten;
- indexed increases as pending decreases;
- failures remain visible;
- Gmail processing continues independently; and
- disconnect/account transitions stop access through generation fencing.

## Notifications and read state

Telegram is optional. Important-message notification state is durable.

Automatic mark-as-read is off by default. If explicitly enabled, it remains a separate workflow stage and never treats an unavailable classification as success.

## Common states

### Google disconnected

Connect Google to access that account’s saved dashboard mail and resume processing.

### Finish Google sign-in

Complete OAuth in the browser tab. The dashboard polls for completion.

### Monitoring unavailable

Check system health, supervisor status, worker logs, and privacy-safe diagnostics.

### Semantic index incomplete

Lexical search remains available. Keep MailMind connected and running while the isolated indexer drains pending rows.

### Historical backlog remains

Wait for the admitted batch to finish, then choose **Fetch next 100**. Live mail continues to receive priority.

### Saved-mail analysis is unavailable

Enable Action Center extraction through the staged configuration, restart the API and worker together, and reconnect the intended account. The disabled control is deliberate; backfill cannot run before its dependent schema and action pipeline are enabled.

## Logs

Owned service logs are written under `.run`:

- `.run/indexer.log`
- `.run/api.log`
- `.run/worker.log`
- `.run/frontend.log`

Logs are intended to contain safe event types rather than email content or raw provider exceptions. Do not publish logs without reviewing them.

## Privacy reminders

- Do not share screenshots containing real email, account identifiers, or provider details.
- Local storage is not encrypted by MailMind.
- Cloud classification receives minimized, masked content in normal mode.
- Explicit saved-mail backfill can send the same minimized, masked saved content to configured cloud classifiers; it does not reread Gmail.
- Token events store counts and safe metadata, not prompts, email text, or provider responses.
- Masking reduces exposure but is not anonymization.
- Local-only mode blocks cloud/Gmail/Telegram routing but is not an operating-system firewall.


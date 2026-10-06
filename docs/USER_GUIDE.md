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

- **Systems healthy** opens current model, provider, worker, classification, and refresh information. **Local model: Off** means the optional comparison shadow is switched off (the default); set `MAILMIND_SHADOW_MODEL_ENABLED=true` to turn it on.
- The account menu provides connect, switch, disconnect, deletion, and stop-processing controls.

### Navigation and overview

**Inbox**, **Action Center** and **Usage** sit in the top navbar, so you can switch sections from anywhere. On narrow screens they move to their own row under the logo. Opening **Action Center** or **Usage** scrolls the page so that section starts just below the navbar; opening **Inbox** returns to the top, where the overview is.

Below the navbar, one overview of eight cards in the same design covers the whole account:

- **Saved emails** counts locally stored mail for the selected account.
- **Processed** counts messages that completed the workflow.
- **Feedback** separates confirmations and corrections.
- **Average classification** uses measured samples, shown in seconds with one decimal, rounded up; it does not invent a value when none exist.
- **Open actions**, **Due soon** (next 7 days) and **Overdue** summarise the Action Center.
- **Tokens today** counts cloud and local token usage since midnight.

### Inbox work progress

The progress panel distinguishes saved work, waiting tasks, active classification, completed work, and items requiring review. It also shows the current historical batch and whether more old pages remain.

### Inbox intake

- **Sync new messages** queues a durable, rate-limited live Gmail check.
- Older mail: Older mail loads automatically, 20 at a time, when you reach the last two pages of your unfiltered inbox. The next 20 load once the previous 20 have been processed, so the classification queue never floods.
- **Analyze up to 20 saved emails** explicitly enriches eligible saved mail after Action Center extraction is enabled.
- Active, live-pending, historical-pending, and last-live-sync values are separate.

These controls are not equivalent. Sync never grants more historical backlog. Saved-mail analysis never sends historical alerts, changes Gmail read state, or creates automatic reminders. Live mail remains ahead of backfill work.

## Intelligence tabs

### Inbox explanations

Each email card has a **Why this category?** section under the email text (it needs `MAILMIND_EXPLANATIONS_VISIBLE=true`). It shows a short reason, the signals behind it, each with the quoted words from the email, and then the whole decision:

- **Decision:** which model chose the category and how long it took, including when the main model was unavailable and a backup model or provider answered;
- **What the category means:** the definition the classifier works from;
- **Your past corrections:** how many similar emails you corrected were shown to the model as examples, or that none were close enough;
- **Second opinion:** whether the local model agreed (shown only when the optional local shadow is switched on);
- **Your label:** when you confirmed or changed the category, your label is the one the dashboard uses.

This is built from what was saved when the email was classified, so it costs no extra model calls. Explanations summarize decision signals; they are not hidden chain-of-thought. A quote that can't be matched to the email is omitted, but the rest of the explanation is kept. A correction changes the effective category but preserves the original analysis for audit.

Mail classified before explanations were enabled says *No explanation was recorded for this email*. Use **Analyze up to 20 saved emails** (Inbox tab) to generate explanations for it. Each email is a real provider request.

### Action Center

The Action Center lists extracted replies, tasks, approvals, payments, meetings, and deadlines. Filters include open, due soon, overdue, snoozed, and completed, plus a **Type** filter that lists only the action types this account actually has, with counts (for example *Payment · 3*). You can complete, dismiss, reopen, or snooze an action. **Open source** shows the originating email in a pop-up without leaving the Action Center: sender, date, category and the full text, with the sentence the action was based on highlighted. Close it with **Esc**, the close button, or a click outside it. **Open in inbox** jumps to the email in the Inbox instead; **Clear source email** beside the search hint then returns to the full inbox.

Deadlines are shown in your local time. A time written in an email without a time zone ("by 5 pm") is read as your local time; if the email states a zone (for example "5 pm EDT"), that zone is respected.

Cards don't show a confidence label: the AI rates its own certainty "high" for almost every task, so the label told you nothing. Instead, the rare task it was unsure about carries an amber **Double-check this** badge, so check that one against its source email. Tasks it was very unsure about are never saved, and only confident ones get automatic reminders.

Automatic dashboard reminders require explicit configuration. Telegram action reminders require a separate opt-in. Ambiguous, past-due, low-confidence, or date-unknown candidates do not silently create automatic reminders.

### Usage

Usage shows day, week, and month token totals with a daily bar chart, an accessible data table, and ranked per-provider and per-operation breakdowns with share bars. Rows whose events have no reported token count read **Not measured** rather than zero. Provider-billed tokens and locally processed tokens are separate. Counts can be provider-reported, tokenizer-counted, estimated, or unavailable; they are usage measurements, not prices.

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

Your correction also applies to **similar future mail**: a new email from the same sender with similar content, or a near-identical email from anyone, gets your category automatically. Its "Why this category?" panel says *Matches your earlier correction*. If you gave similar emails different labels, the closest one decides. Corrections apply to mail that arrives after you make them; they don't relabel older emails.

### Undo

Withdraws the latest feedback revision and restores the latest valid prediction without deleting history.

### History

Shows saved feedback and processing events. The dashboard does not fabricate provider or delivery history.

## Connection problems

MailMind tells you when something is wrong instead of going blank:

- **You're offline:** your computer lost its network. Your saved mail stays on screen and you stay signed in; there is no need to reconnect Google.
- **Can't reach Gmail:** your computer is online but MailMind's Gmail checks are failing. The alert shows a countdown: MailMind retries after 5 s, then 10 s, 20 s, 40 s, and every 60 s after that.
- **You're back online:** shown for a few seconds when the connection returns; new mail syncs on the next check.

- **Restarting a MailMind service:** one part of MailMind stopped unexpectedly and is being restarted automatically; your saved mail stays on screen.
- **MailMind may need to stop:** a service keeps failing right after it starts. MailMind is still retrying and shows a countdown; if the service recovers, the shutdown is cancelled and you see **Everything is running again**. If it doesn't, MailMind stops cleanly; the alert names the log file to check.

Temporary problems (Gemini or Groq outages and quota limits, Gmail or Telegram errors) never send an email to Review: it waits in Processing and retries, at most 5 minutes apart, until it succeeds. Review is only for permanent problems, such as a request the AI rejects or an answer it can't give in the expected form.

**Reconnecting within 24 hours is silent.** After **Disconnect Google** or **Stop processing**, MailMind keeps your Google sign-in unused for 24 hours. Clicking **Connect Google** in that time reconnects straight away, without Google's page, as long as Google still accepts the saved sign-in and it is the same account. After 24 hours it is deleted and Google's page opens as usual. **Switch Google account** always opens Google's page.

Only a real login problem (for example, access revoked in your Google account) asks you to connect Google again. Restarting MailMind doesn't. In that case the dashboard says **Google sign-in needs renewing** instead of the plain "Google is disconnected": click **Connect Google** and sign in; your saved mail and settings are kept. Until you do, the dashboard stays in **read-only** mode: you can browse, search and page through saved mail, open history and source emails, and view actions and usage, but labels, action changes, retries and syncing are disabled. If MailMind's own local service stops responding, the dashboard keeps the last data, pauses email actions, and keeps retrying with the same growing wait.

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

Telegram is optional. Important-message notification state is durable. An alert is marked **unknown** only when it may really have been delivered (for example, the request timed out after reaching Telegram). Unknown alerts are never resent automatically: use **Confirm alert arrived** if you received it, or **Retry alert** if you didn't. A send that failed before reaching Telegram is simply retried.

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

Page towards the end of your inbox (with no search or filter). When the previous older batch has been processed, the next 20 load automatically and the pagination line shows "Loading older emails from Gmail…". Live mail continues to receive priority.

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


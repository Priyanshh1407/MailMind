import { AccountControls } from './AccountControls';
import { HealthPopover } from './HealthPopover';

export function AppHeader({ snapshot, pending, mutate, loading, error, nav = null }) {
  const connected = Boolean(snapshot?.session.connected);
  const ingestion = snapshot?.status?.ingestion;
  const workPending = connected && ((snapshot?.status?.active_pending_tasks ?? 0) > 0 || Boolean(ingestion?.backlog_authorized));
  const tone = error ? 'is-error' : workPending ? 'is-working' : connected ? 'is-connected' : 'is-paused';
  const label = pending.account ? 'Account action pending'
    : error ? 'Refresh needs attention'
      : loading ? 'Refreshing...'
        : snapshot?.status.auth_in_progress ? 'Signing in...'
          : connected ? workPending ? 'Processing inbox' : 'Google connected'
            : 'Processing paused';

  return <header className="dashboard-header">
    <a className="skip-link" href="#emails">Skip to emails</a>
    <div className="brand-lockup">
      <img src="/screen.png" alt="" className="brand-mark" />
      <div className="brand-copy">
        <p className="eyebrow">MAILMIND / INTELLIGENT TRIAGE</p>
        <div className="brand-title-row"><h1>MailMind</h1><p>A calm, private workspace for a better inbox.</p></div>
      </div>
    </div>
    {/* Inbox / Action Center / Usage: always one click away. */}
    {nav && <div className="header-nav">{nav}</div>}
    <div className="header-actions">
      <span className={'connection-state ' + tone}>{label}</span>
      <HealthPopover snapshot={snapshot} stale={Boolean(error)} />
      <AccountControls snapshot={snapshot} pending={pending} mutate={mutate} />
    </div>
  </header>;
}

import { useEffect, useRef, useState } from 'react';
import { ChevronDown, LockKeyhole, UserRound } from 'lucide-react';

export function AccountControls({ snapshot, pending, mutate }) {
  const [open, setOpen] = useState(false);
  const container = useRef(null);
  const trigger = useRef(null);
  const menu = useRef(null);
  const connected = Boolean(snapshot?.session.connected);
  const offline = Boolean(snapshot?.telemetry?.mode?.local_only);
  const email = snapshot?.session.email;
  const busy = Boolean(pending.account);
  const auth = Boolean(snapshot?.status.auth_in_progress);

  function close(restore = true) {
    setOpen(false);
    if (restore) trigger.current?.focus();
  }

  useEffect(() => {
    if (!open) return undefined;
    menu.current?.querySelector('button:not(:disabled)')?.focus();
    const outside = event => {
      if (!container.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);

  async function account(path, options) {
    close();
    await mutate('account', path, options, true);
  }

  function keys(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const buttons = [...menu.current.querySelectorAll('button:not(:disabled)')];
    if (!buttons.length) return;
    event.preventDefault();
    const index = buttons.indexOf(document.activeElement);
    const target = event.key === 'Home' ? 0 : event.key === 'End'
      ? buttons.length - 1
      : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[target].focus();
  }

  if (!snapshot) {
    return <div className="header-account-loading"><span className="status-dot neutral" />Opening MailMind</div>;
  }

  return <div className="header-account-actions">
    {!connected && !auth && <button className="button primary compact connect-header" disabled={busy || offline || snapshot.status.purge_pending} onClick={() => account('/authenticate')}>Connect Google</button>}
    {auth && <button className="button secondary compact" disabled={busy} onClick={() => account('/logout')}>Cancel sign-in</button>}
    <div ref={container} className="account-menu-container">
      <button ref={trigger} type="button" className="header-pill account-trigger" aria-label="Account options" aria-expanded={open} aria-controls="account-options" onClick={() => setOpen(value => !value)}>
        <span className="header-pill-icon"><UserRound size={15} /></span>
        <span className="account-trigger-copy">
          <strong>{email || (offline ? 'Local-only mode' : 'Local session')}</strong>
          <small>{busy ? 'Account action pending' : auth ? 'Finish Google sign-in' : connected ? 'Google connected' : 'Processing paused'}</small>
        </span>
        <ChevronDown size={14} className={open ? 'rotate-icon' : ''} />
      </button>
      {open && <div ref={menu} id="account-options" role="menu" className="account-popover" onKeyDown={keys} onBlur={event => {
        if (!event.currentTarget.contains(event.relatedTarget)) close(false);
      }}>
        <div className="popover-account-head">
          <span className="account-avatar"><UserRound size={17} /></span>
          <div>
            <strong>{email || 'Local session'}</strong>
            <p><span className={'status-dot ' + (connected ? 'success' : 'neutral')} />{offline ? 'Google networking disabled' : auth ? 'Sign-in is in progress' : connected ? 'Google connected - processing active' : 'Google disconnected - processing paused'}</p>
          </div>
        </div>
        <div className="account-menu-actions">
          <button disabled={busy || offline || auth || snapshot.status.purge_pending} onClick={() => account('/authenticate')}>{connected ? 'Switch Google account' : 'Connect Google'}</button>
          <button disabled={busy || auth || !email} onClick={() => account('/disconnect')}>Disconnect Google</button>
          <button disabled={busy || !email} className="danger-item" onClick={() => {
            if (window.confirm('Delete saved mail, feedback and local Google credentials for ' + email + '? Other accounts are kept. This cannot be undone.')) account('/account-data', { method: 'DELETE' });
          }}>{snapshot.status.purge_pending ? 'Retry account deletion' : 'Delete this account data'}</button>
          <button disabled={busy} className="danger-item" onClick={() => {
            if (window.confirm('Delete old unassigned mail, archive, vectors and shared token files? This cannot be undone.')) account('/legacy-data', { method: 'DELETE', body: { confirmation: 'DELETE_UNASSIGNED_DATA' } });
          }}>Delete old unassigned data</button>
          <button disabled={busy} onClick={() => account('/logout')}>Stop processing</button>
        </div>
        {snapshot.status.purge_pending && <p className="purge-notice" role="status">Deletion is pending. Processing stays paused. Retry account deletion.</p>}
        <p className="account-privacy"><LockKeyhole size={13} /> Stop processing and Disconnect keep saved mail. Your Google sign-in is kept unused for 24 hours so Connect Google can reconnect without Google's page, then deleted. Delete account data removes it at once. Revoke Google permission separately in your Google account.</p>
      </div>}
    </div>
  </div>;
}

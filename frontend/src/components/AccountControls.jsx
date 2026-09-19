import { useEffect, useRef, useState } from 'react';
export function AccountControls({ snapshot, pending, mutate }) {
  const [code, setCode] = useState(''), [open, setOpen] = useState(false);
  const container = useRef(null), trigger = useRef(null), menu = useRef(null);
  const paired = Boolean(snapshot), connected = snapshot?.session.connected, offline = snapshot?.telemetry?.mode?.local_only;
  const email = snapshot?.session.email, busy = Boolean(pending.account), auth = snapshot?.status.auth_in_progress;
  function close(restore = true) { setOpen(false); if (restore) trigger.current?.focus(); }
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector('button:not(:disabled)')?.focus();
    const outside = event => { if (!container.current?.contains(event.target)) setOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  async function account(path, options) { close(); await mutate('account', path, options, true); }
  function keys(event) {
    if (event.key === 'Escape') { event.preventDefault(); close(); return; }
    if (!['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
    const buttons = [...menu.current.querySelectorAll('button:not(:disabled)')];
    if (!buttons.length) return;
    event.preventDefault();
    const index = buttons.indexOf(document.activeElement);
    const target = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[target].focus();
  }
  return <section aria-label="Account controls" className="glass-card account-panel">
    {!paired ? <form onSubmit={async event => {
      event.preventDefault();
      if (await mutate('account', '/session', { body: { code } }, true)) setCode('');
    }} className="pair-form">
      <div><h2>Open your local session</h2><p>Copy the pairing code from the API terminal, then connect Google.</p></div>
      <label htmlFor="pair-code">Pairing code</label>
      <input id="pair-code" type="password" autoComplete="off" required maxLength={256} value={code} onChange={event => setCode(event.target.value)} disabled={busy} />
      <button type="submit" disabled={busy}>{busy ? 'Opening…' : 'Open MailMind'}</button>
    </form> : <>
      <div className="account-row"><div><h2>{email || 'Local session'}</h2><p>{offline ? 'Local-only mode; Google networking disabled' : auth ? 'Finish Google sign-in in the opened tab.' : connected ? 'Google connected' : 'Google disconnected — processing paused'}</p></div>
        <div ref={container} className="menu-container"><button ref={trigger} type="button" aria-expanded={open} aria-controls="account-options" onClick={() => setOpen(value => !value)}>Account options</button>
          {open && <div ref={menu} id="account-options" className="account-menu" onKeyDown={keys} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) close(false); }}>
            <button disabled={busy || offline || auth || snapshot.status.purge_pending} onClick={() => account('/authenticate')}>{connected ? 'Switch Google account' : 'Connect Google'}</button>
            <button disabled={busy || auth || !email} onClick={() => account('/disconnect')}>Disconnect Google</button>
            <button disabled={busy || !email} onClick={() => { if (window.confirm(`Delete saved mail, feedback and local Google credentials for ${email}? Other accounts are kept. This cannot be undone.`)) account('/account-data', { method: 'DELETE' }); }}>{snapshot.status.purge_pending ? 'Retry account deletion' : 'Delete this account data'}</button>
            <button disabled={busy} onClick={() => { if (window.confirm('Delete old unassigned mail, archive, vectors and shared token files? This cannot be undone.')) account('/legacy-data', { method: 'DELETE', body: { confirmation: 'DELETE_UNASSIGNED_DATA' } }); }}>Delete old unassigned data</button>
            <button disabled={busy} onClick={() => account('/logout')}>Logout</button>
          </div>}
        </div>
      </div>
      {!connected && !auth && <button disabled={busy || offline || snapshot.status.purge_pending} onClick={() => account('/authenticate')}>Connect Google</button>}
      {auth && <button disabled={busy} onClick={() => account('/logout')}>Cancel sign-in and logout</button>}
      {snapshot.status.purge_pending && <p role="status">Deletion is pending. Processing stays paused. Retry account deletion.</p>}
      <p className="muted">Logout keeps saved mail and credentials. Disconnect removes local Google credentials. Google permission can be removed separately in your Google account.</p>
    </>}
  </section>;
}

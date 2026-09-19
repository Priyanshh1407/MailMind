import { useState } from 'react';
import { createApi } from './api';
import { useDashboard } from './hooks/useDashboard';
import { AccountControls } from './components/AccountControls';
import { DashboardStats } from './components/DashboardStats';
import { EmailBoard } from './components/EmailBoard';
import { CATEGORIES, categoryName, formatTime } from './dashboard';
const api = createApi(`http://${window.location.hostname}:8000`);
function App() {
  const [query, setQuery] = useState({ offset: 0, search: '', category: '' }), [search, setSearch] = useState('');
  const { snapshot, loading, error, notice, pending, refresh, mutate } = useDashboard(api, query);
  const page = snapshot?.page, connected = Boolean(snapshot?.session.connected), disabled = Boolean(pending.account) || Boolean(error), ingestion = snapshot?.status.ingestion;
  const connectionTone = error ? 'is-error' : snapshot?.status.is_polling ? 'is-working' : connected ? 'is-connected' : 'is-paused';
  return <div className="grid-texture dashboard"><a className="skip-link" href="#main">Skip to emails</a>
    <header className="dashboard-header"><div className="brand-lockup"><span aria-hidden="true" className="brand-mark">M</span><div><p className="eyebrow">MAILMIND / INTELLIGENT TRIAGE</p><h1>MailMind</h1><p className="header-subtitle">A calm, private workspace for a better inbox.</p></div></div><span className={`connection-state ${connectionTone}`}>{pending.account ? 'Account action pending' : error ? 'Refresh needs attention' : loading ? 'Refreshing…' : snapshot?.status.auth_in_progress ? 'Signing in…' : connected ? snapshot.status.is_polling ? 'Processing mail' : 'Google connected' : 'Processing paused'}</span></header>
    <main id="main" className="dashboard-main">
      {error && <div role="alert" className="message error"><p>{error.message}</p><button onClick={refresh} disabled={Boolean(pending.account)}>Retry refresh</button>{snapshot && <p>Showing last known data. Email actions are paused until refresh succeeds.</p>}</div>}
      {notice && <p role="status" className="message">{notice}</p>}
      <AccountControls snapshot={snapshot} pending={pending} mutate={async (...args) => { const result = await mutate(...args); if (result && args[3]) { setQuery({ offset: 0, search: '', category: '' }); setSearch(''); } return result; }} />
      <DashboardStats snapshot={snapshot} stale={Boolean(error)} />
      {connected && ingestion && <p className="message" role="status">{ingestion.status === 'error' ? 'The last inbox check failed; this does not mean it was empty.' : ingestion.status === 'partial' ? 'The last inbox check had some failures; good emails were kept.' : ingestion.status === 'empty' ? 'The last inbox check found no new unread emails.' : ingestion.status === 'deferred' ? 'Inbox checking was deferred; another check is needed.' : 'The last inbox check fetched emails.'}{' '}Fetched: {ingestion.fetched_count}. Failed: {ingestion.failed_count}. Checked: {formatTime(ingestion.last_checked_at)}.{ingestion.has_more ? ' More pages remain.' : ''}</p>}
      {connected && snapshot.status.ingestion_failures.length > 0 && <details className="message"><summary>Emails needing fetch or parsing recovery ({snapshot.status.ingestion_failures.length} recent)</summary>{snapshot.status.ingestion_failures.map(row => <div className="failure-row" key={row.email_id}><p>Message {row.email_id}: {row.status} · {row.error_code} · Attempts: {row.attempt_count}</p><button disabled={disabled || pending[row.email_id]} onClick={() => mutate(row.email_id, `/tasks/${encodeURIComponent(row.email_id)}/retry`)}>Retry fetching message</button></div>)}</details>}
      {snapshot && !connected && !snapshot.status.auth_in_progress && <p className="message">Connect Google to view this account's saved emails and resume processing.</p>}
      {connected && <>
        <section className="glass-card filters" aria-label="Search saved emails"><form onSubmit={event => { event.preventDefault(); setQuery(previous => ({ ...previous, offset: 0, search: search.trim() })); }}><label htmlFor="email-search">Search sender, subject or saved body</label><div className="search-row"><input id="email-search" type="search" maxLength={200} value={search} onChange={event => setSearch(event.target.value)} /><button type="submit" disabled={loading}>Search</button><button type="button" onClick={() => { setSearch(''); setQuery({ offset: 0, search: '', category: '' }); }}>Clear filters</button></div></form><label htmlFor="category-filter">Category<select id="category-filter" value={query.category} onChange={event => setQuery(previous => ({ ...previous, offset: 0, category: event.target.value }))}><option value="">All categories</option>{CATEGORIES.map(item => <option key={item} value={item}>{categoryName(item)}</option>)}</select></label><button disabled={loading || disabled || pending.process || snapshot.status.purge_pending || snapshot.telemetry?.mode?.local_only} onClick={() => mutate('process', '/process')}>{pending.process ? 'Queuing…' : 'Queue inbox check'}</button></section>
        {loading && <p role="status">Loading the selected email page…</p>}
        {page && <><nav className="pagination" aria-label="Email pages"><p>{page.total === 0 ? 'No matching saved emails' : `${page.offset + 1}–${Math.min(page.offset + page.emails.length, page.total)} of ${page.total} matching saved emails`}</p><div className="actions"><button disabled={loading || query.offset === 0} onClick={() => setQuery(previous => ({ ...previous, offset: Math.max(0, previous.offset - 20) }))}>Previous page</button><button disabled={loading || !page.has_more} onClick={() => setQuery(previous => ({ ...previous, offset: previous.offset + 20 }))}>Next page</button></div></nav>
          {!page.emails.length && <p className="message">{page.total === 0 ? query.search || query.category ? 'No emails match these filters. Clear them or try a different search.' : 'No saved emails yet. Queue an inbox check or wait for the worker.' : 'This page is now empty. Go to the previous page.'}</p>}
          <EmailBoard page={page} pending={pending} disabled={disabled} mutate={mutate} api={api} generation={snapshot.session.generation} />
        </>}
      </>}
    </main>
  </div>;
}
export default App;

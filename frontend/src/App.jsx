import { useEffect, useState } from 'react';
import { createApi } from './api';
import { useDashboard } from './hooks/useDashboard';
import { AppFeedback } from './components/AppFeedback';
import { AppFooter } from './components/AppFooter';
import { AppHeader } from './components/AppHeader';
import { DashboardStats } from './components/DashboardStats';
import { EmailBoard } from './components/EmailBoard';
import { InboxIntake } from './components/InboxIntake';
import { InboxProgress } from './components/InboxProgress';
import { IngestionStatus } from './components/IngestionStatus';
import { Pagination } from './components/Pagination';
import { SearchFilters } from './components/SearchFilters';

const api = createApi('http://' + window.location.hostname + ':8000');

function App() {
  const [query, setQuery] = useState({ offset: 0, search: '', category: '' });
  const [search, setSearch] = useState('');
  const {
    snapshot, loading, error, actionError, notice, pending, refresh, mutate,
    dismissNotice, dismissActionError,
  } = useDashboard(api, query);

  useEffect(() => {
    const normalized = search.trim();
    if (normalized === query.search) return undefined;
    const timer = window.setTimeout(() => setQuery(previous => ({ ...previous, offset: 0, search: normalized })), 300);
    return () => window.clearTimeout(timer);
  }, [search, query.search]);

  const accountMutate = async (...args) => {
    const result = await mutate(...args);
    if (result && args[3]) {
      setQuery({ offset: 0, search: '', category: '' });
      setSearch('');
    }
    return result;
  };
  const page = snapshot?.page;
  const connected = Boolean(snapshot?.session.connected);
  const disabled = Boolean(pending.account) || Boolean(error);

  return <div className="dashboard">
    <AppHeader snapshot={snapshot} pending={pending} mutate={accountMutate} loading={loading} error={error} />
    <AppFeedback error={error} actionError={actionError} notice={notice} snapshot={snapshot} pending={pending} refresh={refresh} dismissActionError={dismissActionError} dismissNotice={dismissNotice} />
    <main className="dashboard-main">
      <DashboardStats snapshot={snapshot} />
      {connected && <div className="work-grid"><InboxProgress snapshot={snapshot} /><InboxIntake snapshot={snapshot} loading={loading} disabled={disabled} pending={pending} mutate={mutate} /></div>}
      {connected && <IngestionStatus snapshot={snapshot} disabled={disabled} pending={pending} mutate={mutate} />}
      {snapshot && !connected && !snapshot.status.auth_in_progress && <div className="disconnected-callout"><strong>Google is disconnected</strong><p>Connect Google to view this account's saved emails and resume processing. MailMind will not access or process mail before you connect.</p></div>}
      {snapshot?.status.auth_in_progress && <div className="disconnected-callout"><strong>Finish Google sign-in</strong><p>Complete OAuth in the opened tab. This dashboard will update automatically after the connection succeeds.</p></div>}
      {connected && <>
        <SearchFilters search={search} setSearch={setSearch} query={query} setQuery={setQuery} loading={loading} page={page} />
        {loading && page && <p className="refresh-status" role="status">Refreshing saved emails...</p>}
        {page && <div className="email-results" aria-busy={loading}>
          <Pagination page={page} query={query} setQuery={setQuery} loading={loading} />
          {!page.emails.length && <p className="empty-state">{page.total === 0 ? query.search || query.category ? 'No emails match these filters. Clear them or try a different search.' : 'No saved emails yet. Sync new messages or wait for live monitoring.' : 'This page is now empty. Go to the previous page.'}</p>}
          <EmailBoard page={page} pending={pending} disabled={disabled} mutate={mutate} api={api} generation={snapshot.session.generation} />
        </div>}
      </>}
      <AppFooter autoMarkRead={snapshot?.status.auto_mark_read} />
    </main>
  </div>;
}

export default App;

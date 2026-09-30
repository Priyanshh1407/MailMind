import { useEffect, useRef, useState } from 'react';
import { createApi } from './api';
import { useDashboard } from './hooks/useDashboard';
import { ActionCenter } from './components/ActionCenter';
import { ActionSummary } from './components/ActionSummary';
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
import { TokenUsagePanel } from './components/TokenUsagePanel';

const api = createApi('http://' + window.location.hostname + ':8000');
const INITIAL_QUERY = {
  offset: 0, search: '', category: '', emailId: '',
  actionOffset: 0, actionStatus: 'open', actionFilter: 'open',
  actionDueFrom: '', actionDueTo: '', tokenWindow: 'day',
};
const TABS = [
  { key: 'inbox', label: 'Inbox' },
  { key: 'actions', label: 'Action Center' },
  { key: 'usage', label: 'Usage' },
];

function App() {
  const [query, setQuery] = useState(INITIAL_QUERY);
  const [search, setSearch] = useState('');
  const [activeTab, setActiveTab] = useState('inbox');
  // Mount the Usage panel on first visit so its chart bundle loads on demand.
  const [usageOpened, setUsageOpened] = useState(false);
  useEffect(() => { if (activeTab === 'usage') setUsageOpened(true); }, [activeTab]);
  const tabRefs = useRef([]);
  const {
    snapshot, loading, error, actionError, notice, pending, refresh, mutate,
    dismissNotice, dismissActionError,
  } = useDashboard(api, query);

  useEffect(() => {
    const normalized = search.trim();
    if (normalized === query.search) return undefined;
    const timer = window.setTimeout(() => setQuery(previous => ({
      ...previous, offset: 0, search: normalized, emailId: '',
    })), 300);
    return () => window.clearTimeout(timer);
  }, [search, query.search]);

  const accountMutate = async (...args) => {
    const result = await mutate(...args);
    if (result && args[3]) {
      setQuery(INITIAL_QUERY);
      setSearch('');
      setActiveTab('inbox');
    }
    return result;
  };
  const selectTab = key => setActiveTab(key);
  const tabKeys = event => {
    const index = TABS.findIndex(tab => tab.key === activeTab);
    let next = index;
    if (event.key === 'ArrowRight') next = (index + 1) % TABS.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = TABS.length - 1;
    else return;
    event.preventDefault();
    setActiveTab(TABS[next].key);
    tabRefs.current[next]?.focus();
  };
  const openSource = emailId => {
    setSearch('');
    setQuery(previous => ({
      ...previous, offset: 0, search: '', category: '', emailId,
    }));
    setActiveTab('inbox');
    window.requestAnimationFrame(() => document.getElementById('emails')?.scrollIntoView({ block: 'start' }));
  };

  const page = snapshot?.page;
  const connected = Boolean(snapshot?.session.connected);
  const disabled = Boolean(pending.account) || Boolean(error);

  return <div className='dashboard'>
    <AppHeader snapshot={snapshot} pending={pending} mutate={accountMutate} loading={loading} error={error} />
    <AppFeedback error={error} actionError={actionError} notice={notice} snapshot={snapshot} pending={pending} refresh={refresh} dismissActionError={dismissActionError} dismissNotice={dismissNotice} />
    <main className='dashboard-main'>
      {snapshot && !connected && !snapshot.status.auth_in_progress && <div className='disconnected-callout'><strong>Google is disconnected</strong><p>Connect Google to view this account&apos;s saved emails and resume processing. MailMind will not access or process mail before you connect.</p></div>}
      {snapshot?.status.auth_in_progress && <div className='disconnected-callout'><strong>Finish Google sign-in</strong><p>Complete OAuth in the opened tab. This dashboard will update automatically after the connection succeeds.</p></div>}
      {connected && <>
        <ActionSummary snapshot={snapshot} />
        <div className='dashboard-tabs' role='tablist' aria-label='Dashboard sections' onKeyDown={tabKeys}>
          {TABS.map((tab, index) => <button key={tab.key} ref={node => { tabRefs.current[index] = node; }} id={'tab-' + tab.key} role='tab' aria-selected={activeTab === tab.key} aria-controls={'panel-' + tab.key} tabIndex={activeTab === tab.key ? 0 : -1} onClick={() => selectTab(tab.key)}>{tab.label}</button>)}
        </div>
        <section id='panel-inbox' role='tabpanel' aria-labelledby='tab-inbox' hidden={activeTab !== 'inbox'}>
          <DashboardStats snapshot={snapshot} />
          <div className='work-grid'><InboxProgress snapshot={snapshot} /><InboxIntake snapshot={snapshot} loading={loading} disabled={disabled} pending={pending} mutate={mutate} /></div>
          <IngestionStatus snapshot={snapshot} disabled={disabled} pending={pending} mutate={mutate} />
          <SearchFilters search={search} setSearch={setSearch} query={query} setQuery={setQuery} loading={loading} page={page} />
          {loading && page && <p className='refresh-status' role='status'>Refreshing saved emails...</p>}
          {page && <div className='email-results' aria-busy={loading}>
            <Pagination page={page} query={query} setQuery={setQuery} loading={loading} />
            {!page.emails.length && <p className='empty-state'>{page.total === 0 ? query.emailId ? 'The source email is not available for this connected account.' : query.search || query.category ? 'No emails match these filters. Clear them or try a different search.' : 'No saved emails yet. Sync new messages or wait for live monitoring.' : 'This page is now empty. Go to the previous page.'}</p>}
            <EmailBoard page={page} pending={pending} disabled={disabled} mutate={mutate} api={api} generation={snapshot.session.generation} />
          </div>}
        </section>
        <section id='panel-actions' role='tabpanel' aria-labelledby='tab-actions' hidden={activeTab !== 'actions'}>
          <ActionCenter snapshot={snapshot} query={query} setQuery={setQuery} loading={loading} disabled={disabled} pending={pending} mutate={mutate} openSource={openSource} />
        </section>
        <section id='panel-usage' role='tabpanel' aria-labelledby='tab-usage' hidden={activeTab !== 'usage'}>
          {usageOpened && <TokenUsagePanel snapshot={snapshot} setQuery={setQuery} />}
        </section>
      </>}
      <AppFooter autoMarkRead={snapshot?.status.auto_mark_read} />
    </main>
  </div>;
}

export default App;

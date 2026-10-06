import { useEffect, useRef, useState } from 'react';
import { m, useReducedMotion } from 'motion/react';
import { createApi } from './api';
import { OLDER_MAIL_BATCH, disconnectReason, shouldLoadOlderMail } from './dashboard';
import { useDashboard } from './hooks/useDashboard';
import { ActionCenter } from './components/ActionCenter';
import { AppFeedback } from './components/AppFeedback';
import { AppFooter } from './components/AppFooter';
import { ConnectivityBanner } from './components/ConnectivityBanner';
import { AppHeader } from './components/AppHeader';
import { DashboardStats } from './components/DashboardStats';
import { EmailBoard } from './components/EmailBoard';
import { InboxIntake } from './components/InboxIntake';
import { InboxProgress } from './components/InboxProgress';
import { IngestionStatus } from './components/IngestionStatus';
import { Pagination } from './components/Pagination';
import { SectionTabs } from './components/SectionTabs';
import { SearchFilters } from './components/SearchFilters';
import { TokenUsagePanel } from './components/TokenUsagePanel';
import { EASE_OUT } from './motion';
import { BootContext, useBootSequence } from './boot';

const api = createApi('http://' + window.location.hostname + ':8000');
const INITIAL_QUERY = {
  offset: 0, search: '', category: '', emailId: '',
  actionOffset: 0, actionStatus: 'open', actionFilter: 'open', actionType: '',
  actionDueFrom: '', actionDueTo: '', tokenWindow: 'day',
};
// Panels stay mounted (hidden) so state survives a tab switch; the content
// fades up each time its panel becomes the visible one.
const PANEL_MOTION = {
  hidden: { opacity: 0, y: 8 },
  shown: { opacity: 1, y: 0, transition: { duration: 0.28, ease: EASE_OUT } },
};

function TabPanel({ id, active, children }) {
  return <m.section id={'panel-' + id} role='tabpanel' aria-labelledby={'tab-' + id} hidden={!active} initial={false} variants={PANEL_MOTION} animate={active ? 'shown' : 'hidden'}>
    {children}
  </m.section>;
}

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
  const reduceMotion = useReducedMotion();
  // Switching sections: Action Center and Usage scroll to where they start
  // (just below the navbar); Inbox returns to the top, where the overview is.
  const showSection = key => {
    setActiveTab(key);
    window.requestAnimationFrame(() => {
      const behavior = reduceMotion ? 'auto' : 'smooth';
      if (key === 'inbox') window.scrollTo({ top: 0, behavior });
      else document.getElementById('panel-' + key)?.scrollIntoView({ block: 'start', behavior });
    });
  };
  const selectTab = key => showSection(key);
  const tabKeys = event => {
    const index = TABS.findIndex(tab => tab.key === activeTab);
    let next = index;
    if (event.key === 'ArrowRight') next = (index + 1) % TABS.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = TABS.length - 1;
    else return;
    event.preventDefault();
    showSection(TABS[next].key);
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
  // Google sign-in needs renewing: saved mail stays visible, but nothing can change.
  const readOnly = !connected && snapshot?.session.read_only === true;
  const showData = connected || readOnly;
  const disabled = Boolean(pending.account) || Boolean(error);
  // Power-on sequence each time a connected account's dashboard comes up.
  const booting = useBootSequence(connected ? snapshot.session.generation : null, !reduceMotion);
  // Load older mail in the background as the user pages towards the end.
  // It re-arms each time the previous small batch has been processed.
  const loadOlder = connected && !disabled && shouldLoadOlderMail(page, query, snapshot.status);
  useEffect(() => {
    if (loadOlder) mutate('extract', '/ingestion/fetch-next', { body: { limit: OLDER_MAIL_BATCH }, silent: true });
  }, [loadOlder, mutate]);

  return <BootContext.Provider value={booting}><div className={booting ? 'dashboard booting' : 'dashboard'}>
    <AppHeader snapshot={snapshot} pending={pending} mutate={accountMutate} loading={loading} error={error}
      nav={showData ? <SectionTabs tabs={TABS} activeTab={activeTab} onSelect={selectTab} onKeyDown={tabKeys} tabRefs={tabRefs} /> : null} />
    <ConnectivityBanner snapshot={snapshot} refresh={refresh} />
    <AppFeedback error={error} actionError={actionError} notice={notice} snapshot={snapshot} pending={pending} refresh={refresh} dismissActionError={dismissActionError} dismissNotice={dismissNotice} />
    <main className='dashboard-main'>
      {snapshot && !connected && !snapshot.status.auth_in_progress && (disconnectReason(snapshot) === 'login_rejected'
        ? <div className='disconnected-callout' role='alert'><strong>Google sign-in needs renewing</strong><p>Google stopped accepting MailMind&apos;s saved sign-in (it may have expired or been revoked in your Google account). Click Connect Google to sign in again; your saved mail and settings are kept. Until then you can still browse your saved mail and actions (read-only).</p></div>
        : <div className='disconnected-callout'><strong>Google is disconnected</strong><p>Connect Google to view this account&apos;s saved emails and resume processing. MailMind will not access or process mail before you connect.</p></div>)}
      {snapshot?.status.auth_in_progress && <div className='disconnected-callout'><strong>Finish Google sign-in</strong><p>Complete OAuth in the opened tab. This dashboard will update automatically after the connection succeeds.</p></div>}
      {showData && <>
        <DashboardStats snapshot={snapshot} />
        <TabPanel id='inbox' active={activeTab === 'inbox'}>
          <div className='work-grid'><InboxProgress snapshot={snapshot} /><InboxIntake snapshot={snapshot} loading={loading} disabled={disabled || readOnly} pending={pending} mutate={mutate} /></div>
          <IngestionStatus snapshot={snapshot} disabled={disabled || readOnly} pending={pending} mutate={mutate} />
          <SearchFilters search={search} setSearch={setSearch} query={query} setQuery={setQuery} loading={loading} page={page} />
          {loading && page && <p className='refresh-status' role='status'>Refreshing saved emails...</p>}
          {page && <div className='email-results' aria-busy={loading}>
            <Pagination page={page} query={query} setQuery={setQuery} loading={loading} status={snapshot.status} />
            {!page.emails.length && <p className='empty-state'>{page.total === 0 ? query.emailId ? 'The source email is not available for this connected account.' : query.search || query.category ? 'No emails match these filters. Clear them or try a different search.' : 'No saved emails yet. Sync new messages or wait for live monitoring.' : 'This page is now empty. Go to the previous page.'}</p>}
            <EmailBoard page={page} pending={pending} disabled={disabled || readOnly} readDisabled={disabled} mutate={mutate} api={api} generation={snapshot.session.generation} />
          </div>}
        </TabPanel>
        <TabPanel id='actions' active={activeTab === 'actions'}>
          <ActionCenter snapshot={snapshot} query={query} setQuery={setQuery} loading={loading} disabled={disabled || readOnly} readDisabled={disabled} pending={pending} mutate={mutate} openSource={openSource} api={api} />
        </TabPanel>
        <TabPanel id='usage' active={activeTab === 'usage'}>
          {usageOpened && <TokenUsagePanel snapshot={snapshot} setQuery={setQuery} />}
        </TabPanel>
      </>}
      <AppFooter autoMarkRead={snapshot?.status.auto_mark_read} />
    </main>
  </div></BootContext.Provider>;
}

export default App;

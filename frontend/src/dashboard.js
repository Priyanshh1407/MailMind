import { ACTION_TYPES } from './api.js';
export const CATEGORIES = ['IMPORTANT', 'UPDATES', 'SPAM', 'NEEDS_REVIEW'];
export const ACTION_PAGE_LIMIT = 50;
export const EMAIL_PAGE_LIMIT = 20;
export const MAX_PAGE_OFFSET = 1000000;
export const MAX_ACTION_RANGE_DAYS = 366;
const ACTION_STATUSES = ['open', 'completed', 'dismissed', 'snoozed'];
const TOKEN_WINDOWS = ['day', 'week', 'month'];
export const categoryName = category => ({ IMPORTANT: 'Important', UPDATES: 'Updates', SPAM: 'Spam', NEEDS_REVIEW: 'Needs Review' })[category] || category;
export const effectiveCategory = email => CATEGORIES.includes(email.effective_category) ? email.effective_category : 'NEEDS_REVIEW';
export function assertAccount(session, ...responses) {
  if (responses.some(data => data.generation !== session.generation || data.account_id !== session.email)) {
    const error = new Error('The account changed during refresh. Waiting for its current data.'); error.status = 409; throw error;
  }
}

function pageOffset(value, name) {
  const number = value ?? 0;
  if (!Number.isInteger(number) || number < 0 || number > MAX_PAGE_OFFSET) throw new TypeError(name + ' must be a bounded non-negative integer.');
  return number;
}

function explicitTimestamp(value, name) {
  if (value === undefined || value === null || value === '') return null;
  if (typeof value !== 'string' || value.length > 64 || !/(?:Z|[+-]\d\d:\d\d)$/.test(value) || !Number.isFinite(Date.parse(value))) {
    throw new TypeError(name + ' must be an ISO timestamp with a timezone.');
  }
  return value;
}

export function dashboardQueries(query = {}) {
  const emailOffset = pageOffset(query.offset, 'Email offset');
  const actionOffset = pageOffset(query.actionOffset, 'Action offset');
  const actionStatus = query.actionStatus === undefined ? 'open' : query.actionStatus;
  if (actionStatus !== null && !ACTION_STATUSES.includes(actionStatus)) throw new TypeError('Unsupported action status.');
  const actionType = query.actionType || null;
  if (actionType !== null && !ACTION_TYPES.includes(actionType)) throw new TypeError('Unsupported action type.');
  const tokenWindow = query.tokenWindow ?? 'day';
  if (!TOKEN_WINDOWS.includes(tokenWindow)) throw new TypeError('Unsupported token window.');
  const dueFrom = explicitTimestamp(query.actionDueFrom, 'Action range start');
  const dueTo = explicitTimestamp(query.actionDueTo, 'Action range end');
  if ((dueFrom === null) !== (dueTo === null)) throw new TypeError('Action date ranges require both bounds.');
  if (dueFrom && (Date.parse(dueFrom) >= Date.parse(dueTo) || Date.parse(dueTo) - Date.parse(dueFrom) > MAX_ACTION_RANGE_DAYS * 86400000)) {
    throw new TypeError('Action date range must be ordered and no longer than 366 days.');
  }
  const email = new URLSearchParams({ limit: String(EMAIL_PAGE_LIMIT), offset: String(emailOffset), search: query.search || '' });
  if (query.category) email.set('category', query.category);
  if (query.emailId) email.set('email_id', query.emailId);
  const actions = new URLSearchParams({ limit: String(ACTION_PAGE_LIMIT), offset: String(actionOffset) });
  if (actionStatus) actions.set('status', actionStatus);
  if (actionType) actions.set('action_type', actionType);
  if (dueFrom) {
    actions.set('due_from', dueFrom);
    actions.set('due_to', dueTo);
  }
  return { email, actions, tokenWindow };
}

function emptyActionSummary(session) {
  return {
    account_id: session.email, generation: session.generation, total: 0,
    overdue: 0, due_soon: 0,
    status_counts: { open: 0, completed: 0, dismissed: 0, snoozed: 0 },
    type_counts: {},
    reminder_counts: { scheduled: 0, claimed: 0, delivered: 0, dismissed: 0, retry: 0, dead: 0 },
  };
}

function emptyActionPage(session, offset) {
  return { account_id: session.email, generation: session.generation, actions: [], limit: ACTION_PAGE_LIMIT, offset };
}

function emptyTokenSummary(session, window) {
  return {
    account_id: session.email, generation: session.generation, window, timezone: null, start_at: null, end_at: null,
    totals: { event_count: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, unknown_events: 0 },
    provider_billed_tokens: 0, local_processed_tokens: 0, providers: [],
    operations: [], count_methods: [], outcomes: [], daily: [],
  };
}

const unavailable = data => ({ state: 'unavailable', data, error: null });

async function optionalRead(read, empty) {
  try {
    return { state: 'ready', data: await read(), error: null };
  } catch (error) {
    if (error.name === 'AbortError' || [401, 409].includes(error.status)) throw error;
    if (error.status === 404) return { state: 'disabled', data: empty, error: null };
    return {
      state: 'error', data: empty,
      error: { message: error.message || 'This dashboard section could not be loaded.', status: error.status ?? null },
    };
  }
}

export async function loadDashboard(api, query, signal, jobId = null, clock = () => performance.now()) {
  const group = new AbortController();
  const cancel = () => group.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  if (signal?.aborted) cancel();
  try { return await readDashboard(api, query, group.signal, jobId, clock); }
  catch (error) { group.abort(); throw error; }
  finally { signal?.removeEventListener('abort', cancel); }
}
async function readDashboard(api, query, signal, jobId, clock) {
  const start = clock();
  let session;
  try {
    session = await api.request('/session', { signal });
  } catch (error) {
    if (error.status !== 401) throw error;
    const opened = await api.request('/session', { method: 'POST', signal });
    api.setCsrf(opened.csrf_token);
    session = await api.request('/session', { signal });
  }
  const queries = dashboardQueries(query);
  const scoped = async path => {
    const data = await api.request(path, { signal });
    assertAccount(session, data);
    return data;
  };
  const actionOffset = Number(queries.actions.get('offset'));
  const emptySummary = emptyActionSummary(session);
  const emptyPage = emptyActionPage(session, actionOffset);
  const emptyTokens = emptyTokenSummary(session, queries.tokenWindow);
  const emptyTodayTokens = emptyTokenSummary(session, 'day');
  const selectedTokens = session.connected
    ? optionalRead(
      () => scoped('/analytics/tokens?window=' + encodeURIComponent(queries.tokenWindow)),
      emptyTokens,
    )
    : Promise.resolve(unavailable(emptyTokens));
  const todayTokens = queries.tokenWindow === 'day'
    ? selectedTokens
    : session.connected
      ? optionalRead(
        () => scoped('/analytics/tokens?window=day'),
        emptyTodayTokens,
      )
      : Promise.resolve(unavailable(emptyTodayTokens));
  const [status, telemetry, page, actionSummary, actionPage, tokenUsage, tokenToday] = await Promise.all([
    scoped('/status'), scoped('/telemetry'),
    session.connected ? scoped('/emails?' + queries.email) : Promise.resolve(null),
    session.connected ? optionalRead(() => scoped('/actions/summary'), emptySummary) : Promise.resolve(unavailable(emptySummary)),
    session.connected ? optionalRead(() => scoped('/actions?' + queries.actions), emptyPage) : Promise.resolve(unavailable(emptyPage)),
    selectedTokens,
    todayTokens,
  ]);
  let job = null;
  if (jobId) { try { job = await api.request('/jobs/' + encodeURIComponent(jobId), { signal }); } catch (error) { if (error.status !== 404) throw error; } }
  return {
    session, status, telemetry, page, job,
    actions: {
      summary: actionSummary.data, page: actionPage.data,
      summaryState: actionSummary.state, pageState: actionPage.state,
      summaryError: actionSummary.error, pageError: actionPage.error,
    },
    tokenUsage: {
      summary: tokenUsage.data, state: tokenUsage.state,
      error: tokenUsage.error, today: tokenToday.data,
      todayState: tokenToday.state, todayError: tokenToday.error,
    },
    refreshMs: Math.max(0, clock() - start), checkedAt: new Date().toISOString(),
  };
}
export function workerState(snapshot, now = Date.now()) {
  if (!snapshot?.session.connected) return 'Paused';
  const heartbeat = Date.parse(snapshot.status.worker?.heartbeat_at);
  if (!Number.isFinite(heartbeat)) return 'Not observed yet';
  const limit = (snapshot.status.poll_interval_seconds + (snapshot.status.worker_lease_seconds || 90)) * 1000;
  if (now - heartbeat > limit) return 'Stale heartbeat';
  return snapshot.status.is_polling ? 'Processing' : 'Recently checked';
}
export function formatTime(value) { const date = new Date(value); return value && Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Not recorded'; }
export function formatEmailTime(value) {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return '';
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return 'Yesterday';
  const diffDays = Math.floor((now - date) / 86400000);
  if (diffDays < 7) return date.toLocaleDateString(undefined, { weekday: 'short' });
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export const formatCount = value => value == null ? '-' : Number(value).toLocaleString();

export function senderName(value) {
  const raw = (value || '').trim();
  const match = raw.match(/^"?([^"<]*?)"?\s*<[^>]+>$/);
  const name = (match ? match[1] : raw.replace(/^<|>$/g, '')).trim();
  return name || raw || 'Unknown sender';
}

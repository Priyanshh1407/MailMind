import test from 'node:test';
import assert from 'node:assert/strict';
import { validateResponse } from '../src/api.js';
import {
  ACTION_PAGE_LIMIT,
  EMAIL_PAGE_LIMIT,
  dashboardQueries,
  loadDashboard,
} from '../src/dashboard.js';

const ACCOUNT = 'phase5@example.test';
const identity = { account_id: ACCOUNT, generation: 7 };
const session = { connected: true, email: ACCOUNT, generation: 7, csrf_token: 'synthetic' };
const actionSummary = {
  ...identity,
  total: 1,
  overdue: 0,
  due_soon: 1,
  status_counts: { open: 1, completed: 0, dismissed: 0, snoozed: 0 },
  reminder_counts: { scheduled: 0, claimed: 0, delivered: 0, dismissed: 0, retry: 0, dead: 0 },
};
const action = {
  action_id: 1,
  account_id: ACCOUNT,
  email_id: 'synthetic-email',
  fingerprint: 'a'.repeat(64),
  action_type: 'reply_required',
  title: 'Reply to the synthetic sender',
  description: 'Send a short confirmation.',
  evidence: 'Please reply before Friday.',
  due_at: '2026-09-30T11:30:00.000000Z',
  due_precision: 'exact_time',
  confidence: 'high',
  extraction_source: 'gemini',
  status: 'open',
  snoozed_until: null,
  revision: 0,
  created_at: '2026-09-28T10:00:00.000000Z',
  updated_at: '2026-09-28T10:00:00.000000Z',
  completed_at: null,
  analysis_revision: '2026-09-28T09:59:00.000000Z',
  next_reminder_at: null,
  delivered_reminder_count: 0,
};
const actionPage = { ...identity, actions: [action], limit: ACTION_PAGE_LIMIT, offset: 0 };
const tokenSummary = {
  ...identity,
  window: 'day',
  timezone: 'Asia/Kolkata',
  start_at: '2026-09-27T18:30:00.000000Z',
  end_at: '2026-09-28T18:30:00.000000Z',
  totals: { event_count: 1, input_tokens: 12, output_tokens: 3, total_tokens: 15, unknown_events: 0 },
  provider_billed_tokens: 15,
  local_processed_tokens: 0,
  providers: [{ key: 'gemini', event_count: 1, input_tokens: 12, output_tokens: 3, total_tokens: 15, unknown_events: 0 }],
  operations: [{ key: 'classification_analysis', event_count: 1, input_tokens: 12, output_tokens: 3, total_tokens: 15, unknown_events: 0 }],
  count_methods: [{ key: 'provider_reported', event_count: 1, input_tokens: 12, output_tokens: 3, total_tokens: 15, unknown_events: 0 }],
  outcomes: [{ key: 'success', event_count: 1, input_tokens: 12, output_tokens: 3, total_tokens: 15, unknown_events: 0 }],
  daily: [{
    date: '2026-09-28',
    start_at: '2026-09-27T18:30:00.000000Z',
    end_at: '2026-09-28T18:30:00.000000Z',
    event_count: 1, input_tokens: 12, output_tokens: 3,
    total_tokens: 15, unknown_events: 0,
    provider_billed_tokens: 15, local_processed_tokens: 0,
  }],
};

function responseFor(path) {
  if (path === '/session') return session;
  if (path === '/actions/summary') return actionSummary;
  if (path.startsWith('/actions?')) return actionPage;
  if (path.startsWith('/analytics/tokens?')) return tokenSummary;
  return identity;
}

function failure(status, message) {
  const error = new Error(message);
  error.status = status;
  return error;
}

test('Phase 5 strictly validates action pages, summaries, and token summaries', () => {
  assert.doesNotThrow(() => validateResponse('/actions?limit=50', 'GET', actionPage));
  assert.doesNotThrow(() => validateResponse('/actions/summary', 'GET', actionSummary));
  assert.doesNotThrow(() => validateResponse('/analytics/tokens?window=day', 'GET', tokenSummary));
  assert.throws(() => validateResponse('/actions', 'GET', { ...actionPage, actions: [{ ...action, action_type: 'task' }] }), /Unexpected/);
  assert.throws(() => validateResponse('/actions/summary', 'GET', { ...actionSummary, total: 2 }), /Unexpected/);
  assert.throws(() => validateResponse('/analytics/tokens', 'GET', {
    ...tokenSummary,
    providers: [{ ...tokenSummary.providers[0], key: 'gmail' }],
  }), /Unexpected/);
});

test('Phase 5 query construction keeps email/action pages and date windows bounded', () => {
  const defaults = dashboardQueries({ offset: 0, search: '' });
  assert.equal(defaults.email.get('limit'), String(EMAIL_PAGE_LIMIT));
  assert.equal(defaults.actions.get('limit'), String(ACTION_PAGE_LIMIT));
  assert.equal(defaults.actions.get('status'), 'open');
  assert.equal(defaults.tokenWindow, 'day');
  const bounded = dashboardQueries({
    actionOffset: 50,
    actionStatus: 'completed',
    tokenWindow: 'month',
    actionDueFrom: '2026-01-01T00:00:00Z',
    actionDueTo: '2026-12-31T00:00:00Z',
  });
  assert.equal(bounded.actions.get('offset'), '50');
  assert.equal(bounded.actions.get('due_to'), '2026-12-31T00:00:00Z');
  assert.equal(bounded.tokenWindow, 'month');
  for (const query of [
    { actionOffset: 1000001 },
    { actionStatus: 'all' },
    { tokenWindow: 'year' },
    { actionDueFrom: '2026-01-01T00:00:00Z' },
    { actionDueFrom: '2026-12-31T00:00:00Z', actionDueTo: '2026-01-01T00:00:00Z' },
    { actionDueFrom: '2026-01-01T00:00:00Z', actionDueTo: '2027-01-03T00:00:00Z' },
  ]) assert.throws(() => dashboardQueries(query), TypeError);
});

test('Phase 5 starts mail, status, telemetry, actions, and usage reads in parallel', async () => {
  const pending = new Map();
  const calls = [];
  const api = {
    request: async (path) => {
      calls.push(path);
      if (path === '/session') return session;
      return new Promise(resolve => pending.set(path, resolve));
    },
  };
  const run = loadDashboard(api, { offset: 0, search: '' }, new AbortController().signal);
  await new Promise(resolve => setImmediate(resolve));
  const reads = calls.filter(path => path !== '/session');
  assert.equal(reads.length, 6);
  assert.ok(reads.includes('/status'));
  assert.ok(reads.includes('/telemetry'));
  assert.ok(reads.some(path => path.startsWith('/emails?limit=20&offset=0')));
  assert.ok(reads.includes('/actions/summary'));
  assert.ok(reads.some(path => path.startsWith('/actions?limit=50&offset=0&status=open')));
  assert.ok(reads.includes('/analytics/tokens?window=day'));
  for (const [path, resolve] of pending) resolve(responseFor(path));
  const snapshot = await run;
  assert.equal(snapshot.actions.page.actions.length, 1);
  assert.equal(snapshot.tokenUsage.summary.totals.total_tokens, 15);
});

test('Phase 5 rejects switched-account intelligence and aborts sibling reads', async () => {
  const signals = [];
  const api = {
    request: async (path, { signal }) => {
      signals.push(signal);
      if (path === '/session') return session;
      if (path === '/actions/summary') return { ...actionSummary, account_id: 'other@example.test' };
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')), { once: true }));
    },
  };
  await assert.rejects(
    loadDashboard(api, { offset: 0, search: '' }, new AbortController().signal),
    error => error.status === 409,
  );
  assert.ok(signals.length >= 7);
  assert.ok(signals.every(signal => signal.aborted));
});

test('Phase 5 analytics failure preserves inbox and action data', async () => {
  const api = {
    request: async path => {
      if (path.startsWith('/analytics/tokens?')) throw failure(503, 'Synthetic analytics outage.');
      return responseFor(path);
    },
  };
  const snapshot = await loadDashboard(api, { offset: 0, search: '' }, new AbortController().signal);
  assert.equal(snapshot.page.account_id, ACCOUNT);
  assert.equal(snapshot.actions.pageState, 'ready');
  assert.equal(snapshot.tokenUsage.state, 'error');
  assert.equal(snapshot.tokenUsage.summary.totals.total_tokens, 0);
  assert.equal(snapshot.tokenUsage.error.message, 'Synthetic analytics outage.');
});

test('Phase 5 action failure stays local so the inbox remains publishable', async () => {
  const api = {
    request: async path => {
      if (path === '/actions/summary' || path.startsWith('/actions?')) throw failure(503, 'Synthetic Action Center outage.');
      return responseFor(path);
    },
  };
  const snapshot = await loadDashboard(api, { offset: 0, search: '' }, new AbortController().signal);
  assert.equal(snapshot.page.account_id, ACCOUNT);
  assert.equal(snapshot.actions.summaryState, 'error');
  assert.equal(snapshot.actions.pageState, 'error');
  assert.deepEqual(snapshot.actions.page.actions, []);
  assert.equal(snapshot.tokenUsage.state, 'ready');
});

test('Phase 5 enabled endpoints preserve truthful empty action and usage states', async () => {
  const emptyActions = {
    ...actionSummary,
    total: 0,
    status_counts: { open: 0, completed: 0, dismissed: 0, snoozed: 0 },
  };
  const emptyTokens = {
    ...tokenSummary,
    totals: { event_count: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, unknown_events: 0 },
    provider_billed_tokens: 0,
    providers: [],
    operations: [],
    count_methods: [],
    outcomes: [],
    daily: [{
      ...tokenSummary.daily[0],
      event_count: 0, input_tokens: 0, output_tokens: 0,
      total_tokens: 0, unknown_events: 0,
      provider_billed_tokens: 0, local_processed_tokens: 0,
    }],
  };
  const api = {
    request: async path => {
      if (path === '/actions/summary') return emptyActions;
      if (path.startsWith('/actions?')) return { ...actionPage, actions: [] };
      if (path.startsWith('/analytics/tokens?')) return emptyTokens;
      return responseFor(path);
    },
  };
  const snapshot = await loadDashboard(api, { offset: 0, search: '' }, new AbortController().signal);
  assert.equal(snapshot.actions.summaryState, 'ready');
  assert.equal(snapshot.actions.summary.total, 0);
  assert.deepEqual(snapshot.actions.page.actions, []);
  assert.equal(snapshot.tokenUsage.state, 'ready');
  assert.equal(snapshot.tokenUsage.summary.totals.event_count, 0);
});

test('Phase 5 disabled features return stable empty states without false errors', async () => {
  const api = {
    request: async path => {
      if (path.startsWith('/actions') || path.startsWith('/analytics/tokens?')) throw failure(404, 'Feature disabled.');
      return responseFor(path);
    },
  };
  const snapshot = await loadDashboard(api, { offset: 0, search: '' }, new AbortController().signal);
  assert.equal(snapshot.actions.summaryState, 'disabled');
  assert.equal(snapshot.actions.pageState, 'disabled');
  assert.equal(snapshot.actions.summary.total, 0);
  assert.deepEqual(snapshot.actions.page.actions, []);
  assert.equal(snapshot.tokenUsage.state, 'disabled');
  assert.equal(snapshot.tokenUsage.summary.totals.event_count, 0);
  assert.equal(snapshot.tokenUsage.error, null);
});

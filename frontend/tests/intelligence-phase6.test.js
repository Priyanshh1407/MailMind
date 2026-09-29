import test from 'node:test';
import assert from 'node:assert/strict';
import { validateResponse } from '../src/api.js';
import { dashboardQueries, loadDashboard } from '../src/dashboard.js';
import {
  actionFilterQuery, hasEstimatedUsage, signalLabel, sourceLabel,
} from '../src/intelligence.js';

const ACCOUNT = 'phase6@example.test';
const identity = { account_id: ACCOUNT, generation: 11 };
const session = {
  connected: true, email: ACCOUNT, generation: 11, csrf_token: 'phase6',
};
const disconnected = {
  connected: false, email: null, generation: 11, csrf_token: 'phase6',
};
const disconnectedIdentity = { account_id: null, generation: 11 };
const status = { ...identity };
const telemetry = { ...identity };
const daily = {
  date: '2026-09-28',
  start_at: '2026-09-27T18:30:00.000000Z',
  end_at: '2026-09-28T18:30:00.000000Z',
  event_count: 2, input_tokens: 13, output_tokens: 5, total_tokens: 18,
  unknown_events: 0, provider_billed_tokens: 15,
  local_processed_tokens: 3,
};
const usage = window => ({
  ...identity, window, timezone: 'Asia/Kolkata',
  start_at: '2026-09-27T18:30:00.000000Z',
  end_at: '2026-09-28T18:30:00.000000Z',
  totals: {
    event_count: 2, input_tokens: 13, output_tokens: 5,
    total_tokens: 18, unknown_events: 0,
  },
  provider_billed_tokens: 15, local_processed_tokens: 3,
  providers: [
    { key: 'gemini', event_count: 1, input_tokens: 10, output_tokens: 5, total_tokens: 15, unknown_events: 0 },
    { key: 'local', event_count: 1, input_tokens: 3, output_tokens: 0, total_tokens: 3, unknown_events: 0 },
  ],
  operations: [{ key: 'classification_analysis', event_count: 2, input_tokens: 13, output_tokens: 5, total_tokens: 18, unknown_events: 0 }],
  count_methods: [{ key: 'provider_reported', event_count: 2, input_tokens: 13, output_tokens: 5, total_tokens: 18, unknown_events: 0 }],
  outcomes: [{ key: 'success', event_count: 2, input_tokens: 13, output_tokens: 5, total_tokens: 18, unknown_events: 0 }],
  daily: [{ ...daily }],
});
const analysis = {
  analysis_version: 'analysis-v1',
  predicted_category: 'IMPORTANT',
  explanation_summary: 'A direct approval is requested before a deadline.',
  signals: [
    { signal: 'approval_request', evidence: 'approve the plan' },
    { signal: 'deadline' },
  ],
  source: 'gemini',
  model_version: 'synthetic-v1',
  retrieval_used: false,
};
const actionMutation = {
  action_id: 7, account_id: ACCOUNT, email_id: 'mail-7',
  status: 'completed', revision: 1, snoozed_until: null,
  completed_at: '2026-09-28T10:00:00.000000Z',
  updated_at: '2026-09-28T10:00:00.000000Z',
};

test('Phase 6 action filters produce bounded server-side windows', () => {
  const now = new Date('2026-09-28T12:00:00.000Z');
  assert.deepEqual(actionFilterQuery('open', now), {
    actionOffset: 0, actionDueFrom: '', actionDueTo: '',
    actionStatus: 'open',
  });
  const soon = actionFilterQuery('due_soon', now);
  assert.equal(soon.actionStatus, 'open');
  assert.equal(soon.actionDueFrom, now.toISOString());
  assert.equal(Date.parse(soon.actionDueTo) - now.getTime(), 7 * 86400000);
  const overdue = actionFilterQuery('overdue', now);
  assert.equal(Date.parse(overdue.actionDueTo), now.getTime());
  assert.equal(now.getTime() - Date.parse(overdue.actionDueFrom), 366 * 86400000);
  assert.throws(() => actionFilterQuery('everything', now), /Unsupported/);
});

test('Phase 6 exact source lookup is encoded as a bounded email query', () => {
  const result = dashboardQueries({
    offset: 0, search: '', emailId: 'mail/with spaces',
  });
  assert.equal(result.email.get('email_id'), 'mail/with spaces');
  assert.match(result.email.toString(), /email_id=mail%2Fwith\+spaces/);
});

test('Phase 6 validates explanations and action mutation acknowledgements', () => {
  const emailPage = {
    ...identity, total: 1, limit: 20, offset: 0, has_more: false,
    status: 'success', search_mode: 'text', semantic_available: false,
    semantic_index: { pending: 0, indexed: 1, failed: 0 },
    emails: [{
      id: 'mail-7', subject: 'Approval', sender: 'Sender',
      created_at: '2026-09-28T08:00:00.000000Z',
      effective_category: 'IMPORTANT', review_reason: null, analysis,
    }],
  };
  assert.doesNotThrow(() => validateResponse('/emails?email_id=mail-7', 'GET', emailPage));
  assert.doesNotThrow(() => validateResponse('/actions/7', 'PATCH', actionMutation));
  assert.doesNotThrow(() => validateResponse('/actions/7/snooze', 'POST', {
    ...actionMutation, status: 'snoozed',
    snoozed_until: '2026-09-29T10:00:00.000000Z', completed_at: null,
  }));
  assert.throws(() => validateResponse('/emails', 'GET', {
    ...emailPage,
    emails: [{ ...emailPage.emails[0], analysis: { ...analysis, source: 'unknown' } }],
  }), /Unexpected/);
  assert.throws(() => validateResponse('/actions/7', 'PATCH', {
    ...actionMutation, revision: -1,
  }), /Unexpected/);
});

test('Phase 6 validates daily token series and identifies estimated usage', () => {
  const summary = usage('month');
  assert.doesNotThrow(() => validateResponse('/analytics/tokens?window=month', 'GET', summary));
  assert.equal(hasEstimatedUsage(summary), false);
  assert.equal(hasEstimatedUsage({
    ...summary,
    count_methods: [{ ...summary.count_methods[0], key: 'estimated' }],
  }), true);
  assert.throws(() => validateResponse('/analytics/tokens', 'GET', {
    ...summary, daily: [{ ...daily, provider_billed_tokens: -1 }],
  }), /Unexpected/);
  assert.equal(signalLabel('approval_request'), 'Approval Request');
  assert.equal(sourceLabel('local_heuristic'), 'Local model');
});

test('Phase 6 disconnected dashboard never requests protected account data', async () => {
  const calls = [];
  const api = {
    request: async path => {
      calls.push(path);
      if (path === '/session') return disconnected;
      if (path === '/status') return disconnectedIdentity;
      if (path === '/telemetry') return disconnectedIdentity;
      throw new Error('Protected route requested while disconnected: ' + path);
    },
  };
  const snapshot = await loadDashboard(
    api, { offset: 0, search: '' }, new AbortController().signal,
  );
  assert.equal(snapshot.page, null);
  assert.deepEqual(calls, ['/session', '/status', '/telemetry']);
  assert.equal(snapshot.actions.pageState, 'unavailable');
  assert.equal(snapshot.tokenUsage.state, 'unavailable');
});

test('Phase 6 non-day usage also fetches the tokens-today KPI once', async () => {
  const calls = [];
  const api = {
    request: async path => {
      calls.push(path);
      if (path === '/session') return session;
      if (path === '/status') return status;
      if (path === '/telemetry') return telemetry;
      if (path.startsWith('/emails?')) return { ...identity };
      if (path === '/actions/summary') return { ...identity };
      if (path.startsWith('/actions?')) return { ...identity };
      if (path === '/analytics/tokens?window=month') return usage('month');
      if (path === '/analytics/tokens?window=day') return usage('day');
      throw new Error('Unexpected path ' + path);
    },
  };
  const snapshot = await loadDashboard(
    api,
    { offset: 0, search: '', tokenWindow: 'month' },
    new AbortController().signal,
  );
  assert.equal(calls.filter(path => path === '/analytics/tokens?window=day').length, 1);
  assert.equal(snapshot.tokenUsage.summary.window, 'month');
  assert.equal(snapshot.tokenUsage.today.window, 'day');
});

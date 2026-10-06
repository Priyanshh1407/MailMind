import test from 'node:test';
import assert from 'node:assert/strict';

import { countLabel, describeDecision, formatDeadline, needsDoubleCheck, signalLabel } from '../src/intelligence.js';

const options = { locale: 'en-GB', timeZone: 'Asia/Kolkata' };

test('date-only deadlines show the calendar day, never the internal reminder hour', () => {
  // Stored at the 09:00 local reminder hour; the email only named a date.
  assert.equal(formatDeadline('2026-10-02T03:30:00.000000Z', 'date_only', options), 'Fri 2 Oct');
});

test('timed deadlines show day and time without seconds', () => {
  assert.equal(
    formatDeadline('2026-10-01T09:30:00.000000Z', 'exact_time', options), 'Thu 1 Oct, 15:00');
  assert.equal(
    formatDeadline('2026-10-01T09:30:00.000000Z', 'relative', options), 'Thu 1 Oct, 15:00');
});

test('missing or invalid deadlines read as not detected', () => {
  assert.equal(formatDeadline(null, 'unknown', options), 'No deadline detected');
  assert.equal(formatDeadline('not-a-date', 'exact_time', options), 'No deadline detected');
});

test('counts use the right plural', () => {
  assert.equal(countLabel(1, 'event'), '1 event');
  assert.equal(countLabel(2, 'event'), '2 events');
  assert.equal(countLabel(0, 'event'), '0 events');
  assert.equal(countLabel(1200, 'event'), (1200).toLocaleString() + ' events');
});

test('action type filter becomes an action_type query parameter', async () => {
  const { dashboardQueries } = await import('../src/dashboard.js');
  const withType = dashboardQueries({ actionType: 'payment_required' });
  assert.equal(withType.actions.get('action_type'), 'payment_required');
  assert.equal(dashboardQueries({}).actions.get('action_type'), null);
  assert.throws(() => dashboardQueries({ actionType: 'urgent' }), /Unsupported action type/);
});

test('summary type counts must be known types with integer counts', async () => {
  const { validateResponse } = await import('../src/api.js');
  const summary = typeCounts => ({
    account_id: 'a@example.test', generation: 1, total: 3, overdue: 0, due_soon: 0,
    status_counts: { open: 3, completed: 0, dismissed: 0, snoozed: 0 },
    reminder_counts: { scheduled: 0, claimed: 0, delivered: 0, dismissed: 0, retry: 0, dead: 0 },
    type_counts: typeCounts,
  });
  const ok = data => { try { validateResponse('/actions/summary', 'GET', data); return true; } catch { return false; } };
  assert.equal(ok(summary({ payment_required: 2, meeting: 1 })), true);
  assert.equal(ok(summary({ urgent: 1 })), false);
  assert.equal(ok(summary({ meeting: 'one' })), false);
});

test('the decision story covers route, meaning, corrections, second opinion and your label', () => {
  const base = { decided_by: 'model', category: 'IMPORTANT', route: 'primary', provider: 'gemini',
    model_version: 'gemini-3.8-flash', precedents: { used: 0, lookup: 'none_close_enough' },
    second_opinion: { category: 'IMPORTANT', agrees: true }, elapsed_ms: 1201, your_label: null };
  const text = decision => Object.fromEntries(describeDecision(decision).map(part => [part.key, part.text]));
  const primary = text(base);
  assert.equal(primary.decision, 'Gemini (gemini-3.8-flash) chose Important in 1.3 s.');
  assert.match(primary.meaning, /needs your action or attention/);
  assert.match(primary.precedents, /judged on its own/);
  assert.equal(primary.second, 'The local model also chose Important.');
  assert.equal(primary.yours, undefined);
  assert.match(text({ ...base, route: 'fallback' }).decision, /backup Gemini model answered/);
  assert.match(text({ ...base, route: 'groq', provider: 'groq' }).decision, /backup provider answered/);
  assert.equal(text({ ...base, precedents: { used: 3, lookup: 'used' } }).precedents,
    '3 similar emails you corrected were shown to the model as examples.');
  assert.match(text({ ...base, second_opinion: { category: 'SPAM', agrees: false } }).second, /would have chosen Spam/);
  assert.match(text({ ...base, decided_by: 'you', your_label: 'UPDATES' }).yours, /changed this to Updates/);
  const local = text({ ...base, route: 'local', provider: 'local', second_opinion: null, precedents: { used: 0, lookup: 'not_applicable' } });
  assert.match(local.decision, /running entirely on this computer/);
  assert.equal(local.precedents, undefined);
  assert.deepEqual(describeDecision(null), []);
});

test('a decision made by your earlier correction is labelled in plain words', () => {
  assert.equal(signalLabel('feedback_precedent'), 'Your earlier correction');
  assert.equal(signalLabel('direct_request'), 'Direct Request');
});

test('only tasks the AI was unsure about ask for a double-check', () => {
  assert.equal(needsDoubleCheck({ confidence: 'high' }), false);
  assert.equal(needsDoubleCheck({ confidence: 'medium' }), true);
  assert.equal(needsDoubleCheck({ confidence: 'low' }), true);
});

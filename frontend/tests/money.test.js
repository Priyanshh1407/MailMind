import test from 'node:test';
import assert from 'node:assert/strict';
import { validateResponse } from '../src/api.js';
import { daysUntil, formatMoney, parseMoney, shiftMonth } from '../src/money.js';

const identity = { account_id: 'synthetic@example.test', generation: 1 };
const entry = {
  entry_id: 3, email_id: 'synthetic-1', kind: 'purchase', amount_minor: 34000, currency: 'INR', merchant: 'Swiggy',
  occurred_at: '2026-09-02T08:00:00.000000Z', renews_on: null, period: null, evidence: 'Order total: Rs 340',
  source: 'gemini', revision: 0, user_corrected: false,
};

test('amounts are formatted from minor units in the browser', () => {
  assert.equal(formatMoney(34000, 'INR'), '₹340');
  assert.equal(formatMoney(12345678, 'INR'), '₹1,23,456.78');
  assert.equal(formatMoney(1234, 'USD'), '$12.34');
  assert.equal(formatMoney(null, 'INR'), '-');
});

test('typed corrections parse to minor units and reject nonsense', () => {
  assert.equal(parseMoney('1,299.50'), 129950);
  assert.equal(parseMoney('₹ 340'), 34000);
  assert.equal(parseMoney('0'), null);
  assert.equal(parseMoney('12.345'), null);
  assert.equal(parseMoney('-5'), null);
  assert.equal(parseMoney('abc'), null);
});

test('months shift across year boundaries and renewal countdowns use calendar days', () => {
  assert.equal(shiftMonth('2026-01', -1), '2025-12');
  assert.equal(shiftMonth('2026-12', 1), '2027-01');
  assert.equal(daysUntil('2026-10-05', new Date(2026, 9, 1, 23, 30)), 4);
});

test('ledger responses are validated before the Money tab renders them', () => {
  const summary = {
    ...identity, month: '2026-09', currency: 'INR',
    totals: { INR: { spent_minor: 34000, refunds_minor: 0, bills_minor: 0, count: 1, net_minor: 34000 } },
    trend: [{ month: '2026-09', spent_minor: 34000, refunds_minor: 0 }],
    merchants: [{ merchant: 'Swiggy', currency: 'INR', count: 1, spent_minor: 34000 }],
    scan: { done: 1, not_transaction: 4, retry: 0, waiting: 0 },
  };
  assert.doesNotThrow(() => validateResponse('/ledger/summary?month=2026-09', 'GET', summary));
  assert.throws(() => validateResponse('/ledger/summary', 'GET', { ...summary, currency: 'BTC' }), /Unexpected/);
  assert.throws(() => validateResponse('/ledger/summary', 'GET', { ...summary, totals: { INR: { spent_minor: 1.5 } } }), /Unexpected/);
  assert.doesNotThrow(() => validateResponse('/ledger/entries', 'GET', { ...identity, entries: [entry], total: 1, limit: 50, offset: 0 }));
  assert.throws(() => validateResponse('/ledger/entries', 'GET', { ...identity, entries: [{ ...entry, kind: 'gift' }], total: 1, limit: 50, offset: 0 }), /Unexpected/);
  assert.doesNotThrow(() => validateResponse('/ledger/entries/3', 'PATCH', { ...identity, entry: { ...entry, user_corrected: true }, message: 'Saved.' }));
  const subscription = { merchant: 'Netflix', currency: 'INR', period: 'monthly', amount_minor: 64900, monthly_minor: 64900,
    last_charged_at: '2026-09-05T08:00:00.000000Z', renews_on: '2026-10-05', charges: 3, category: 'streaming',
    email_id: 'synthetic-2', basis: 'stated' };
  assert.doesNotThrow(() => validateResponse('/ledger/subscriptions', 'GET', { ...identity, subscriptions: [subscription],
    insights: [{ kind: 'overlap', category: 'streaming', currency: 'INR', merchants: ['Netflix', 'Spotify'], monthly_minor: 76800 }] }));
  assert.throws(() => validateResponse('/ledger/subscriptions', 'GET', { ...identity, subscriptions: [{ ...subscription, period: 'daily' }], insights: [] }), /Unexpected/);
});

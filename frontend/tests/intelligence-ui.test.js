import test from 'node:test';
import assert from 'node:assert/strict';

import { countLabel, formatDeadline } from '../src/intelligence.js';

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

import test from 'node:test';
import assert from 'node:assert/strict';

import { validateResponse } from '../src/api.js';


const status = {
  account_id: 'phase8@example.test',
  generation: 14,
  connected: true,
  is_polling: false,
  auth_in_progress: false,
  purge_pending: false,
  auto_mark_read: false,
  ingestion_paused: false,
  fetch_next_available: false,
  live_monitoring: true,
  active_pending_tasks: 2,
  live_pending_tasks: 0,
  backlog_pending_tasks: 2,
  max_pending_tasks: 100,
  resume_pending_tasks: 50,
  current_batch_target_tasks: 100,
  current_batch_admitted_tasks: 0,
  workflow_total_tasks: 2,
  workflow_finished_tasks: 0,
  processing_counts: { queued: 2 },
  notification_counts: {},
  ingestion_failures: [],
  semantic_search_index: { pending: 0, indexed: 2, failed: 0 },
  intelligence_backfill: {
    enabled: true,
    eligible: 3,
    queued: 2,
    running: 0,
    retry: 0,
    complete: 0,
    dead: 0,
  },
};


test('Phase 8 strictly validates bounded backfill progress', () => {
  assert.doesNotThrow(() => validateResponse('/status', 'GET', status));
  assert.throws(() => validateResponse('/status', 'GET', {
    ...status,
    intelligence_backfill: {
      ...status.intelligence_backfill,
      eligible: -1,
    },
  }), /Unexpected/);
  assert.throws(() => validateResponse('/status', 'GET', {
    ...status,
    intelligence_backfill: {
      ...status.intelligence_backfill,
      enabled: 'yes',
    },
  }), /Unexpected/);
  assert.throws(() => validateResponse('/status', 'GET', {
    ...status,
    intelligence_backfill: undefined,
  }), /Unexpected/);
});


test('Phase 8 backfill mutation requires a bounded durable acknowledgement', () => {
  const response = {
    job_id: 45,
    status: 'queued',
    requested: 20,
    admitted: 2,
    eligible: 2,
    capacity: 99,
    message: 'Queued 2 saved emails for analysis.',
  };
  assert.doesNotThrow(() => validateResponse(
    '/intelligence/backfill', 'POST', response));
  for (const invalid of [
    { ...response, job_id: undefined },
    { ...response, requested: 101 },
    { ...response, admitted: 0 },
    { ...response, admitted: 21 },
    { ...response, message: undefined },
  ]) {
    assert.throws(() => validateResponse(
      '/intelligence/backfill', 'POST', invalid), /Unexpected/);
  }
});

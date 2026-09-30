// The loopback session stays in an HttpOnly cookie and opens automatically.
export function createApi(baseUrl, transport = fetch) {
  let csrf = '';
  return {
    setCsrf(value) { csrf = value || ''; },
    async request(path, { method = 'GET', body, signal, timeout = 15000 } = {}) {
      const controller = new AbortController();
      let timedOut = false;
      const abort = () => controller.abort();
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort();
      const timer = setTimeout(() => { timedOut = true; abort(); }, timeout);
      try {
        const response = await transport(baseUrl + path, {
          method, credentials: 'include', signal: controller.signal,
          headers: { 'Content-Type': 'application/json', ...(method === 'GET' ? {} : { 'X-CSRF-Token': csrf }) },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        let data;
        try { data = await response.json(); } catch { data = null; }
        if (controller.signal.aborted) throw new DOMException('Request cancelled', 'AbortError');
        if (!response.ok) {
          const error = new Error(typeof data?.detail === 'string' ? data.detail : response.status === 422 ? 'Please check your input and try again.' : 'The server could not finish this request. Refresh and try again.');
          error.status = response.status;
          throw error;
        }
        validateResponse(path, method, data);
        return data;
      } catch (error) {
        if (timedOut) throw new Error('The request took too long. Refresh to check whether your action was saved before trying again.');
        if (error.name === 'AbortError' || error.status || error.message.startsWith('Unexpected')) throw error;
        throw new Error('Cannot reach MailMind. Check that the local API is running, then retry.');
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
      }
    },
  };
}
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const integer = value => Number.isInteger(value) && value >= 0;
const positiveInteger = value => Number.isInteger(value) && value > 0;
const identity = value => record(value) && integer(value.generation) && (value.account_id === null || typeof value.account_id === 'string');
const timestamp = value => typeof value === 'string' && value.length <= 64 && /(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));
const nullableTimestamp = value => value === null || timestamp(value);
const keysAreIntegers = (value, keys) => record(value) && keys.every(key => integer(value[key]));
const ACTION_STATUSES = ['open', 'completed', 'dismissed', 'snoozed'];
export const ACTION_TYPES = ['reply_required', 'approval_required', 'payment_required', 'document_required', 'meeting', 'review_required', 'follow_up_required', 'general_task'];
const REMINDER_STATUSES = ['scheduled', 'claimed', 'delivered', 'dismissed', 'retry', 'dead'];
const TOKEN_PROVIDERS = ['gemini', 'groq', 'local', 'embedding'];
const TOKEN_OPERATIONS = ['classification_analysis', 'local_shadow', 'document_embedding', 'query_embedding', 'manual_prediction', 'action_reanalysis'];
const TOKEN_COUNT_METHODS = ['provider_reported', 'tokenizer_counted', 'estimated', 'unavailable'];
const TOKEN_OUTCOMES = ['success', 'failed', 'timeout', 'cancelled'];
const ANALYSIS_SOURCES = ['gemini', 'groq', 'local_heuristic', 'system'];
const EXPLANATION_SIGNALS = [
  'direct_request', 'deadline', 'approval_request', 'payment_request',
  'document_request', 'meeting_request', 'follow_up_request',
  'review_request', 'transactional_update', 'delivery_update',
  'no_response_requested', 'promotional_content', 'unsolicited_claim',
  'credential_request', 'feedback_precedent', 'local_model_signal',
];

function validAnalysis(analysis) {
  return analysis === null || (record(analysis)
    && typeof analysis.analysis_version === 'string'
    && [null, 'IMPORTANT', 'UPDATES', 'SPAM'].includes(analysis.predicted_category)
    && typeof analysis.explanation_summary === 'string'
    && analysis.explanation_summary.length > 0
    && ANALYSIS_SOURCES.includes(analysis.source)
    && (analysis.model_version === null || typeof analysis.model_version === 'string')
    && typeof analysis.retrieval_used === 'boolean'
    && Array.isArray(analysis.signals) && analysis.signals.length <= 3
    && analysis.signals.every(item => record(item)
      && EXPLANATION_SIGNALS.includes(item.signal)
      && (item.evidence === undefined || typeof item.evidence === 'string')));
}

function validAction(action) {
  return record(action) && positiveInteger(action.action_id) && typeof action.account_id === 'string' && typeof action.email_id === 'string' && typeof action.fingerprint === 'string'
    && ACTION_TYPES.includes(action.action_type) && typeof action.title === 'string' && typeof action.description === 'string' && typeof action.evidence === 'string'
    && nullableTimestamp(action.due_at) && ['exact_time', 'date_only', 'relative', 'unknown'].includes(action.due_precision)
    && ['low', 'medium', 'high'].includes(action.confidence) && ['gemini', 'groq', 'local_heuristic', 'system'].includes(action.extraction_source)
    && ACTION_STATUSES.includes(action.status) && nullableTimestamp(action.snoozed_until) && integer(action.revision)
    && timestamp(action.created_at) && timestamp(action.updated_at) && nullableTimestamp(action.completed_at)
    && nullableTimestamp(action.analysis_revision) && nullableTimestamp(action.next_reminder_at) && integer(action.delivered_reminder_count);
}

function validActionMutation(action) {
  return record(action) && positiveInteger(action.action_id)
    && typeof action.account_id === 'string' && typeof action.email_id === 'string'
    && ACTION_STATUSES.includes(action.status) && integer(action.revision)
    && nullableTimestamp(action.snoozed_until)
    && nullableTimestamp(action.completed_at) && timestamp(action.updated_at);
}

function validTokenBreakdown(rows, allowedKeys) {
  return Array.isArray(rows) && rows.every(row => record(row) && allowedKeys.includes(row.key)
    && keysAreIntegers(row, ['event_count', 'input_tokens', 'output_tokens', 'total_tokens', 'unknown_events']));
}

function validDailyTrend(rows) {
  return Array.isArray(rows) && rows.length >= 1 && rows.length <= 31
    && rows.every(row => record(row)
      && /^\d{4}-\d{2}-\d{2}$/.test(row.date)
      && timestamp(row.start_at) && timestamp(row.end_at)
      && Date.parse(row.start_at) < Date.parse(row.end_at)
      && keysAreIntegers(row, [
        'event_count', 'input_tokens', 'output_tokens', 'total_tokens',
        'unknown_events', 'provider_billed_tokens', 'local_processed_tokens',
      ]));
}

export function validateResponse(path, method, data) {
  const route = path.split('?')[0];
  let valid = record(data);
  if (valid && route === '/session') valid = typeof data.csrf_token === 'string' && (method !== 'GET' || (integer(data.generation) && typeof data.connected === 'boolean' && (data.email === null || typeof data.email === 'string')));
  if (valid && method === 'GET' && route === '/status') valid = identity(data) && ['connected','is_polling','auth_in_progress','purge_pending','auto_mark_read'].every(key => typeof data[key] === 'boolean') && record(data.processing_counts) && record(data.notification_counts) && Array.isArray(data.ingestion_failures);
  if (valid && method === 'GET' && route === '/status') valid = ['ingestion_paused','fetch_next_available','live_monitoring'].every(key => typeof data[key] === 'boolean') && ['active_pending_tasks','live_pending_tasks','backlog_pending_tasks','max_pending_tasks','resume_pending_tasks','current_batch_target_tasks','current_batch_admitted_tasks','workflow_total_tasks','workflow_finished_tasks'].every(key => integer(data[key]));
  if (valid && method === 'GET' && route === '/status') valid = record(data.intelligence_backfill) && typeof data.intelligence_backfill.enabled === 'boolean' && ['eligible','queued','running','retry','complete','dead'].every(key => integer(data.intelligence_backfill[key]));
  if (valid && method === 'GET' && route === '/emails') valid = identity(data) && Array.isArray(data.emails) && data.emails.every(email => record(email) && typeof email.id === 'string' && typeof email.subject === 'string' && typeof email.sender === 'string' && typeof email.created_at === 'string' && [null,'IMPORTANT','UPDATES','SPAM'].includes(email.effective_category) && (email.review_reason === null || (record(email.review_reason) && typeof email.review_reason.code === 'string' && typeof email.review_reason.message === 'string')) && (email.analysis === undefined || validAnalysis(email.analysis))) && ['total','limit','offset'].every(key => integer(data[key])) && typeof data.has_more === 'boolean' && ['text','hybrid'].includes(data.search_mode) && typeof data.semantic_available === 'boolean' && record(data.semantic_index) && ['pending','indexed','failed'].every(key => integer(data.semantic_index[key]));
  if (valid && method === 'GET' && route === '/telemetry') valid = identity(data) && record(data.totals) && ['saved','completed','labelled','corrected','confirmed','feedback_events','prediction_attempts'].every(key => integer(data.totals[key])) && record(data.local_model) && typeof data.local_model.ready === 'boolean' && record(data.providers) && record(data.classification_timing) && integer(data.classification_timing.sample_count) && (data.classification_timing.mean_ms === null || (typeof data.classification_timing.mean_ms === 'number' && Number.isFinite(data.classification_timing.mean_ms) && data.classification_timing.mean_ms >= 0));
  if (valid && method === 'GET' && route.endsWith('/history')) valid = identity(data) && Array.isArray(data.feedback) && Array.isArray(data.processing);
  if (valid && ((method === 'POST' && ['/authenticate','/inbox/sync','/ingestion/fetch-next','/intelligence/backfill'].includes(route)) || route.startsWith('/jobs/'))) valid = ['string','number'].includes(typeof data.job_id) && typeof data.status === 'string';
  if (valid && method === 'POST' && route === '/intelligence/backfill') valid = typeof data.message === 'string' && ['requested','admitted','eligible','capacity'].every(key => integer(data[key])) && data.requested >= 1 && data.requested <= 100 && data.admitted >= 1 && data.admitted <= data.requested && data.capacity <= 1000;
  const actionMutation = /^\/actions\/\d+(?:\/snooze)?$/.test(route);
  if (valid && actionMutation && ['PATCH', 'POST'].includes(method)) {
    valid = validActionMutation(data);
  }
  if (valid && method !== 'GET' && !actionMutation && !['/session','/authenticate','/inbox/sync','/ingestion/fetch-next','/intelligence/backfill','/feedback/reconcile'].includes(route)) valid = typeof data.message === 'string';
  if (valid && route.startsWith('/jobs/')) valid = ['queued','running','complete','failed','cancelled'].includes(data.status);
  if (valid && method === 'GET' && route === '/telemetry') valid = ['gemini','groq'].every(key => ['unconfigured','configured_unverified','disabled_local_only'].includes(data.providers[key]));
  if (valid && method === 'GET' && route === '/actions') valid = identity(data) && Array.isArray(data.actions) && data.actions.every(validAction) && integer(data.limit) && data.limit >= 1 && data.limit <= 200 && integer(data.offset) && data.offset <= 1000000;
  if (valid && method === 'GET' && route === '/actions/summary') valid = identity(data) && integer(data.total) && integer(data.overdue) && integer(data.due_soon) && keysAreIntegers(data.status_counts, ACTION_STATUSES) && keysAreIntegers(data.reminder_counts, REMINDER_STATUSES) && data.total === ACTION_STATUSES.reduce((sum, key) => sum + data.status_counts[key], 0);
  // Only action types the account has; each a known type with an integer count.
  if (valid && method === 'GET' && route === '/actions/summary' && data.type_counts !== undefined) valid = record(data.type_counts) && Object.entries(data.type_counts).every(([key, value]) => ACTION_TYPES.includes(key) && integer(value));
  if (valid && method === 'GET' && route === '/analytics/tokens') valid = identity(data) && ['day', 'week', 'month'].includes(data.window) && typeof data.timezone === 'string' && timestamp(data.start_at) && timestamp(data.end_at) && Date.parse(data.start_at) < Date.parse(data.end_at) && keysAreIntegers(data.totals, ['event_count', 'input_tokens', 'output_tokens', 'total_tokens', 'unknown_events']) && integer(data.provider_billed_tokens) && integer(data.local_processed_tokens) && validTokenBreakdown(data.providers, TOKEN_PROVIDERS) && validTokenBreakdown(data.operations, TOKEN_OPERATIONS) && validTokenBreakdown(data.count_methods, TOKEN_COUNT_METHODS) && validTokenBreakdown(data.outcomes, TOKEN_OUTCOMES) && validDailyTrend(data.daily);
  if (valid && method === 'GET' && route === '/status') valid = [data.processing_counts,data.notification_counts].every(counts => Object.values(counts).every(integer)) && data.ingestion_failures.every(row => record(row) && typeof row.email_id === 'string' && typeof row.error_code === 'string' && integer(row.attempt_count) && ['retry','dead'].includes(row.status));
  if (valid && method === 'GET' && route === '/status') valid = record(data.semantic_search_index) && ['pending','indexed','failed'].every(key => integer(data.semantic_search_index[key]));
  if (valid && method === 'GET' && route.endsWith('/history')) valid = data.feedback.every(row => record(row) && integer(row.revision_id) && [null,'IMPORTANT','UPDATES','SPAM'].includes(row.label) && typeof row.created_at === 'string' && ['pending','indexed','failed'].includes(row.indexing_state)) && data.processing.every(row => record(row) && typeof row.stage === 'string' && typeof row.outcome === 'string' && typeof row.created_at === 'string');
  if (valid && method === 'GET' && route === '/telemetry' && data.mode !== undefined) valid = record(data.mode) && typeof data.mode.local_only === 'boolean';
  if (valid && method === 'GET' && route === '/telemetry') valid = ['gemini','groq'].every(key => data.providers[key] === 'disabled_local_only' ? data.mode?.local_only === true : data.mode?.local_only !== true);
  if (!valid) throw new Error('Unexpected server response. Refresh the page; the frontend and API may need restarting together.');
}

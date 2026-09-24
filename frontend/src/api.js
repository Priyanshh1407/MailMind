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
const identity = value => record(value) && integer(value.generation) && (value.account_id === null || typeof value.account_id === 'string');
export function validateResponse(path, method, data) {
  const route = path.split('?')[0];
  let valid = record(data);
  if (valid && route === '/session') valid = typeof data.csrf_token === 'string' && (method !== 'GET' || (integer(data.generation) && typeof data.connected === 'boolean' && (data.email === null || typeof data.email === 'string')));
  if (valid && method === 'GET' && route === '/status') valid = identity(data) && ['connected','is_polling','auth_in_progress','purge_pending','auto_mark_read'].every(key => typeof data[key] === 'boolean') && record(data.processing_counts) && record(data.notification_counts) && Array.isArray(data.ingestion_failures);
  if (valid && method === 'GET' && route === '/status') valid = ['ingestion_paused','fetch_next_available','live_monitoring'].every(key => typeof data[key] === 'boolean') && ['active_pending_tasks','live_pending_tasks','backlog_pending_tasks','max_pending_tasks','resume_pending_tasks','current_batch_target_tasks','current_batch_admitted_tasks','workflow_total_tasks','workflow_finished_tasks'].every(key => integer(data[key]));
  if (valid && method === 'GET' && route === '/emails') valid = identity(data) && Array.isArray(data.emails) && data.emails.every(email => record(email) && typeof email.id === 'string' && typeof email.subject === 'string' && typeof email.sender === 'string' && typeof email.created_at === 'string' && [null,'IMPORTANT','UPDATES','SPAM'].includes(email.effective_category) && (email.review_reason === null || (record(email.review_reason) && typeof email.review_reason.code === 'string' && typeof email.review_reason.message === 'string'))) && ['total','limit','offset'].every(key => integer(data[key])) && typeof data.has_more === 'boolean' && ['text','hybrid'].includes(data.search_mode) && typeof data.semantic_available === 'boolean' && record(data.semantic_index) && ['pending','indexed','failed'].every(key => integer(data.semantic_index[key]));
  if (valid && method === 'GET' && route === '/telemetry') valid = identity(data) && record(data.totals) && ['saved','completed','labelled','corrected','confirmed','feedback_events','prediction_attempts'].every(key => integer(data.totals[key])) && record(data.local_model) && typeof data.local_model.ready === 'boolean' && record(data.providers) && record(data.classification_timing) && integer(data.classification_timing.sample_count) && (data.classification_timing.mean_ms === null || (typeof data.classification_timing.mean_ms === 'number' && Number.isFinite(data.classification_timing.mean_ms) && data.classification_timing.mean_ms >= 0));
  if (valid && method === 'GET' && route.endsWith('/history')) valid = identity(data) && Array.isArray(data.feedback) && Array.isArray(data.processing);
  if (valid && ((method === 'POST' && ['/authenticate','/inbox/sync','/ingestion/fetch-next','/ingestion/resume'].includes(route)) || route.startsWith('/jobs/'))) valid = ['string','number'].includes(typeof data.job_id) && typeof data.status === 'string';
  if (valid && method !== 'GET' && !['/session','/authenticate','/inbox/sync','/ingestion/fetch-next','/ingestion/resume','/feedback/reconcile'].includes(route)) valid = typeof data.message === 'string';
  if (valid && route.startsWith('/jobs/')) valid = ['queued','running','complete','failed','cancelled'].includes(data.status);
  if (valid && method === 'GET' && route === '/telemetry') valid = ['gemini','groq'].every(key => ['unconfigured','configured_unverified','disabled_local_only'].includes(data.providers[key]));
  if (valid && method === 'GET' && route === '/status') valid = [data.processing_counts,data.notification_counts].every(counts => Object.values(counts).every(integer)) && data.ingestion_failures.every(row => record(row) && typeof row.email_id === 'string' && typeof row.error_code === 'string' && integer(row.attempt_count) && ['retry','dead'].includes(row.status));
  if (valid && method === 'GET' && route === '/status') valid = record(data.semantic_search_index) && ['pending','indexed','failed'].every(key => integer(data.semantic_search_index[key]));
  if (valid && method === 'GET' && route.endsWith('/history')) valid = data.feedback.every(row => record(row) && integer(row.revision_id) && [null,'IMPORTANT','UPDATES','SPAM'].includes(row.label) && typeof row.created_at === 'string' && ['pending','indexed','failed'].includes(row.indexing_state)) && data.processing.every(row => record(row) && typeof row.stage === 'string' && typeof row.outcome === 'string' && typeof row.created_at === 'string');
  if (valid && method === 'GET' && route === '/telemetry' && data.mode !== undefined) valid = record(data.mode) && typeof data.mode.local_only === 'boolean';
  if (valid && method === 'GET' && route === '/telemetry') valid = ['gemini','groq'].every(key => data.providers[key] === 'disabled_local_only' ? data.mode?.local_only === true : data.mode?.local_only !== true);
  if (!valid) throw new Error('Unexpected server response. Refresh the page; the frontend and API may need restarting together.');
}

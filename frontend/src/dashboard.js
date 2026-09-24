export const CATEGORIES = ['IMPORTANT', 'UPDATES', 'SPAM', 'NEEDS_REVIEW'];
export const categoryName = category => ({ IMPORTANT: 'Important', UPDATES: 'Updates', SPAM: 'Spam', NEEDS_REVIEW: 'Needs Review' })[category] || category;
export const effectiveCategory = email => CATEGORIES.includes(email.effective_category) ? email.effective_category : 'NEEDS_REVIEW';
export function assertAccount(session, ...responses) {
  if (responses.some(data => data.generation !== session.generation || data.account_id !== session.email)) {
    const error = new Error('The account changed during refresh. Waiting for its current data.'); error.status = 409; throw error;
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
  const params = new URLSearchParams({ limit: '20', offset: String(query.offset), search: query.search });
  if (query.category) params.set('category', query.category);
  const [status, telemetry, page] = await Promise.all([
    api.request('/status', { signal }), api.request('/telemetry', { signal }),
    session.connected ? api.request('/emails?' + params, { signal }) : Promise.resolve(null),
  ]);
  assertAccount(session, status, telemetry, ...(page ? [page] : []));
  let job = null;
  if (jobId) { try { job = await api.request('/jobs/' + encodeURIComponent(jobId), { signal }); } catch (error) { if (error.status !== 404) throw error; } }
  return { session, status, telemetry, page, job, refreshMs: Math.max(0, clock() - start), checkedAt: new Date().toISOString() };
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
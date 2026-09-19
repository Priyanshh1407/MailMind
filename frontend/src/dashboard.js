export const CATEGORIES = ['IMPORTANT', 'UPDATES', 'SPAM', 'NEEDS_REVIEW'];
export const categoryName = category => category === 'NEEDS_REVIEW' ? 'Needs Review' : category;
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
  const session = await api.request('/session', { signal });
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

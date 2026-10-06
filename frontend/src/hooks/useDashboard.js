import { useCallback, useEffect, useRef, useState } from 'react';
import { createPoller } from '../polling';
import { loadDashboard } from '../dashboard';
export function useDashboard(api, query) {
  const [snapshot, setSnapshot] = useState(null), [loading, setLoading] = useState(true), [error, setError] = useState(null), [notice, setNotice] = useState(''), [actionError, setActionError] = useState(null), [pending, setPending] = useState({});
  const epoch = useRef(0), locks = useRef(new Set()), mutations = useRef(new Set()), poller = useRef(null), authJob = useRef(null), mounted = useRef(false);
  const queryRef = useRef(query); queryRef.current = query;
  const reset = useCallback(() => {
    epoch.current += 1; poller.current?.stop();
    for (const controller of mutations.current) controller.abort();
    mutations.current.clear(); locks.current.clear();
    setPending({}); setSnapshot(null); setError(null); setActionError(null); setLoading(true);
  }, []);
  useEffect(() => {
    mounted.current = true;
    poller.current = createPoller(signal => loadDashboard(api, queryRef.current, signal, authJob.current), {
      onData(data) {
        api.setCsrf(data.session.csrf_token); setSnapshot(data); setError(null); setLoading(false);
        if (data.job?.status === 'failed') { setNotice('Google sign-in failed. Connect again to retry.'); authJob.current = null; }
        if (data.job?.status === 'complete') { setNotice('Google connected. Inbox work is queued.'); authJob.current = null; }
        if (!data.status.auth_in_progress && !data.job) authJob.current = null;
      },
      onError(failure) {
        setError(failure); setLoading(false);
        if ([401,409].includes(failure.status)) {
          setSnapshot(null);
          if (failure.status === 401) { api.setCsrf(''); authJob.current = null; }
        }
      },
    });
    const currentPoller = poller.current;
    const currentMutations = mutations.current;
    currentPoller.start();
    return () => { mounted.current = false; epoch.current += 1; currentPoller.stop(); for (const controller of currentMutations) controller.abort(); };
  }, [api]);
  useEffect(() => {
    poller.current?.stop(); setLoading(true);
    if (!locks.current.has('account')) poller.current?.start();
  }, [
    query.offset, query.search, query.category, query.emailId,
    query.actionOffset, query.actionStatus, query.actionType, query.actionDueFrom,
    query.actionDueTo, query.tokenWindow,
  ]);
  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(''), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (!actionError) return undefined;
    const timer = window.setTimeout(() => setActionError(null), 10000);
    return () => window.clearTimeout(timer);
  }, [actionError]);
  const refresh = useCallback(() => { setActionError(null); return poller.current?.refresh(); }, []);
  // silent: background work the user didn't click (e.g. loading older mail).
  // It shows no notice and no error toast; the next poll shows the result.
  const mutate = useCallback(async (key, path, { silent = false, ...options } = {}, account = false) => {
    if (locks.current.has(key) || (!account && locks.current.has('account'))) return false;
    if (account) reset();
    locks.current.add(key); setPending(previous => ({ ...previous, [key]: true }));
    if (!silent) { setNotice(''); setActionError(null); }
    const version = epoch.current;
    const controller = new AbortController(); mutations.current.add(controller);
    try {
      const result = await api.request(path, { method: 'POST', ...options, signal: controller.signal, timeout: 60000 });
      if (version !== epoch.current || !mounted.current) return false;
      if (!silent) setNotice(result.message || 'Saved.');
      if (path === '/authenticate') authJob.current = result.job_id;
      if (path === '/logout') { api.setCsrf(''); authJob.current = null; }
      return true;
    } catch (failure) {
      if (version === epoch.current && mounted.current && failure.name !== 'AbortError' && !silent) {
        setActionError(failure);
        if (failure.status === 401) { setSnapshot(null); api.setCsrf(''); }
      }
      return false;
    } finally {
      mutations.current.delete(controller);
      if (version === epoch.current && mounted.current) {
        locks.current.delete(key); setPending(previous => ({ ...previous, [key]: false }));
        if (account) { setLoading(false); poller.current?.start(); } else poller.current?.refresh();
      }
    }
  }, [api, reset]);
  return { snapshot, loading, error, actionError, notice, pending, refresh, mutate,
    dismissNotice: () => setNotice(''), dismissActionError: () => setActionError(null) };
}

import { useEffect, useRef, useState } from 'react';
import { CATEGORIES, categoryName, formatTime } from '../dashboard';
export function EmailCard({ email, style, pending, disabled, mutate, api, generation }) {
  const [editing, setEditing] = useState(false), [label, setLabel] = useState(email.human_label || email.effective_category || 'IMPORTANT');
  const [history, setHistory] = useState(null), [historyError, setHistoryError] = useState(''), [historyBusy, setHistoryBusy] = useState(false);
  const editor = useRef(null), editButton = useRef(null), historyRequest = useRef(null);
  useEffect(() => { if (editing) editor.current?.querySelector('select')?.focus(); }, [editing]);
  useEffect(() => () => historyRequest.current?.abort(), []);
  function close() { setEditing(false); editButton.current?.focus(); }
  async function feedback(value) {
    if (await mutate(email.id, '/feedback', { body: { email_id: email.id, label: value, expected_revision_id: email.feedback?.revision_id || 0 } })) { setHistory(null); close(); }
  }
  async function recover(action) {
    if (action === 'retry' && email.notification?.status === 'unknown' && !window.confirm('This alert may already have arrived. Retrying could send a duplicate. Continue?')) return;
    if (action === 'confirmed_sent' && !window.confirm('Have you checked Telegram and confirmed this alert arrived?')) return;
    const id = encodeURIComponent(email.id);
    await mutate(email.id, action === 'task' ? '/tasks/' + id + '/retry' : '/notifications/' + id + '/resolve', action === 'task' ? {} : { body: { action } });
  }
  async function showHistory() {
    if (history) { setHistory(null); return; }
    if (historyBusy) return;
    const controller = new AbortController(); historyRequest.current = controller; setHistoryBusy(true); setHistoryError('');
    try {
      const result = await api.request('/emails/' + encodeURIComponent(email.id) + '/history', { signal: controller.signal });
      if (!controller.signal.aborted && result.generation === generation && result.account_id === email.account_id) setHistory(result);
    } catch (failure) { if (!controller.signal.aborted) setHistoryError(failure.message); }
    finally { if (!controller.signal.aborted) setHistoryBusy(false); }
  }
  const blocked = pending || disabled, original = email.prediction || 'No category', latest = email.latest_prediction;
  const effective = email.effective_category || (email.processing_state === 'pending' ? 'Processing' : 'Needs Review');
  return <article className="glass-card email-card group" aria-label={email.subject || 'Untitled email'} aria-busy={pending}>
    <header className="email-card-header"><div><p className="sender">{email.sender || 'Unknown sender'}</p><time dateTime={email.created_at}>{formatTime(email.created_at)}</time></div>
      <span className={'category-badge border ' + style.badge}>{effective}</span>
    </header>
    <h3 className={style.hover}>{email.subject || '(No subject)'}</h3>
    <p className="snippet">{email.body_snippet}</p>
    {email.processing && email.processing.status !== 'complete' && <p className="processing-line">{email.processing.status === 'running' ? 'Classifying now' : email.processing.status === 'queued' ? 'Waiting to process' : email.processing.status + ' · ' + email.processing.stage}{email.processing.error_code ? ' · ' + email.processing.error_code : ''}</p>}
    {email.notification && ['unknown','blocked','dead','retry'].includes(email.notification.status) && <div className="actions email-actions"><button disabled={blocked} onClick={() => recover('retry')}>Retry alert</button>{email.notification.status === 'unknown' && <button disabled={blocked} onClick={() => recover('confirmed_sent')}>Confirm alert arrived</button>}</div>}
    {['retry','dead'].includes(email.processing?.status) && email.processing.stage !== 'notify' && <div className="email-actions"><button disabled={blocked} onClick={() => recover('task')}>Retry unfinished step</button></div>}
    <details className="technical-details"><summary>Classification details</summary><div className="technical-content">
      <p>Primary: {latest?.category || latest?.outcome || original}{latest?.source ? ' (' + latest.source + ')' : ''} · Local: {latest?.local?.category || latest?.local?.outcome || email.local_prediction || 'No category'}</p>
      <p className="muted">Original primary: {original} · Original local: {email.local_prediction || 'No category'}</p>
      {Boolean(email.body_truncated) && <p>The saved body was shortened to its text limit.</p>}
      {email.parse_warnings?.length > 0 && <p>Some message parts required fallback decoding.</p>}
      {email.processing && <p>Workflow: {email.processing.status} · {email.processing.stage} · {email.processing.attempt_count} attempts</p>}
      {email.notification && <p>Alert: {email.notification.status}</p>}
      {email.human_label && <p>Your feedback: {email.human_label} · {email.feedback?.indexing_state === 'indexed' ? 'indexed' : 'index update pending'}</p>}
      {email.feedback?.label === null && email.feedback.indexing_state !== 'indexed' && <p>Feedback withdrawn; index cleanup is pending.</p>}
    </div></details>
    <div className="actions email-actions">
      {!email.human_label && email.effective_category && <button disabled={blocked} onClick={() => feedback(email.effective_category)}>Confirm</button>}
      <button ref={editButton} disabled={blocked} aria-expanded={editing} onClick={() => { setLabel(email.human_label || email.effective_category || 'IMPORTANT'); setEditing(value => !value); }}>{email.human_label ? 'Edit label' : 'Set label'}</button>
      {email.human_label && <button disabled={blocked} onClick={async () => { if (await mutate(email.id, '/feedback/' + encodeURIComponent(email.id) + '?expected_revision_id=' + email.feedback.revision_id, { method: 'DELETE' })) setHistory(null); }}>Undo</button>}
      <button disabled={historyBusy || disabled} aria-expanded={Boolean(history)} onClick={showHistory}>{historyBusy ? 'Loading…' : history ? 'Hide history' : 'History'}</button>
    </div>
    {editing && <form ref={editor} className="feedback-editor" onSubmit={event => { event.preventDefault(); feedback(label); }} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); close(); } }}><label>Correct category<select value={label} onChange={event => setLabel(event.target.value)} disabled={blocked}>{CATEGORIES.filter(item => item !== 'NEEDS_REVIEW').map(item => <option key={item} value={item}>{categoryName(item)}</option>)}</select></label><div className="actions"><button type="submit" disabled={blocked}>{pending ? 'Saving…' : 'Save category'}</button><button type="button" onClick={close}>Cancel</button></div></form>}
    {pending && <p role="status">Saving this action…</p>}{historyError && <p role="alert">{historyError}</p>}
    {history && <div className="history"><h4>Recent history</h4><h5>Feedback</h5>{history.feedback.length ? <ol>{history.feedback.map(row => <li key={row.revision_id}>{formatTime(row.created_at)}: {row.label || 'Feedback withdrawn'} · {row.indexing_state}</li>)}</ol> : <p>No feedback yet.</p>}<h5>Processing</h5>{history.processing.length ? <ol>{history.processing.map((row,index) => <li key={index}>{formatTime(row.created_at)}: {row.stage} · {row.outcome}{row.error_code ? ' · ' + row.error_code : ''}</li>)}</ol> : <p>No saved processing steps.</p>}</div>}
  </article>;
}
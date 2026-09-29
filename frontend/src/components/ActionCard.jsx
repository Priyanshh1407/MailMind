import { Check, ExternalLink, RotateCcw, Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';
import { actionTypeLabel, sourceLabel } from '../intelligence';
import { formatTime } from '../dashboard';

function defaultSnoozeTime() {
  const date = new Date(Date.now() + 86400000);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

export function ActionCard({ action, busy, disabled, mutate, openSource }) {
  const [snoozedUntil, setSnoozedUntil] = useState(defaultSnoozeTime);
  const mutationKey = 'action:' + action.action_id;
  const snoozeValid = useMemo(() => {
    const value = Date.parse(snoozedUntil);
    return Number.isFinite(value) && value > Date.now();
  }, [snoozedUntil]);

  const changeStatus = status => mutate(
    mutationKey,
    '/actions/' + action.action_id,
    {
      method: 'PATCH',
      body: { status, expected_revision: action.revision },
    },
  );
  const snooze = () => mutate(
    mutationKey,
    '/actions/' + action.action_id + '/snooze',
    {
      body: {
        snoozed_until: new Date(snoozedUntil).toISOString(),
        expected_revision: action.revision,
      },
    },
  );

  return <article className='action-card' aria-busy={busy}>
    <div className='action-card-heading'>
      <div>
        <span className='action-type'>{actionTypeLabel(action.action_type)}</span>
        <h3>{action.title}</h3>
      </div>
      <span className={'action-status status-' + action.status}>{action.status}</span>
    </div>
    <p>{action.description}</p>
    <blockquote>{action.evidence}</blockquote>
    <dl className='action-metadata'>
      <div><dt>Deadline</dt><dd>{action.due_at ? formatTime(action.due_at) : 'No deadline detected'}</dd></div>
      <div><dt>Confidence</dt><dd>{action.confidence}</dd></div>
      <div><dt>Source</dt><dd>{sourceLabel(action.extraction_source)}</dd></div>
    </dl>
    {action.status === 'snoozed' && <p className='action-snoozed'>Snoozed until {formatTime(action.snoozed_until)}</p>}
    <div className='action-controls'>
      {['open', 'snoozed'].includes(action.status) && <>
        <button className='button primary compact' disabled={busy || disabled} onClick={() => changeStatus('completed')}><Check size={14} />Complete</button>
        <button className='button ghost compact' disabled={busy || disabled} onClick={() => changeStatus('dismissed')}><Trash2 size={14} />Dismiss</button>
      </>}
      {['completed', 'dismissed', 'snoozed'].includes(action.status) && <button className='button secondary compact' disabled={busy || disabled} onClick={() => changeStatus('open')}><RotateCcw size={14} />Reopen</button>}
      <button className='button ghost compact' disabled={busy || disabled} onClick={() => openSource(action.email_id)}><ExternalLink size={14} />Open source</button>
    </div>
    {action.status === 'open' && <div className='snooze-control'>
      <label htmlFor={'snooze-' + action.action_id}>Snooze until</label>
      <input id={'snooze-' + action.action_id} type='datetime-local' value={snoozedUntil} onChange={event => setSnoozedUntil(event.target.value)} />
      <button className='button secondary compact' disabled={busy || disabled || !snoozeValid} onClick={snooze}>Snooze</button>
    </div>}
    {busy && <p className='pending-line' role='status'>Saving action...</p>}
  </article>;
}

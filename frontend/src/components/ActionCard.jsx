import { Check, ExternalLink, RotateCcw, Trash2 } from 'lucide-react';
import { useCallback, useMemo, useRef, useState } from 'react';
import { actionTypeLabel, formatDeadline, needsDoubleCheck, sourceLabel } from '../intelligence';
import { SourceEmailDialog } from './SourceEmailDialog';

function defaultSnoozeTime() {
  const date = new Date(Date.now() + 86400000);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

export function ActionCard({ action, busy, disabled, readDisabled = disabled, mutate, openSource, api, generation }) {
  const [snoozedUntil, setSnoozedUntil] = useState(defaultSnoozeTime);
  // The source email opens in a pop-up; focus returns to this button on close.
  const [showSource, setShowSource] = useState(false);
  const sourceButton = useRef(null);
  const closeSource = useCallback(() => {
    setShowSource(false);
    window.requestAnimationFrame(() => sourceButton.current?.focus());
  }, []);
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
        {needsDoubleCheck(action) && <span className='action-check' title='The AI was not fully sure about this task. Check it against the source email.'>Double-check this</span>}
      </div>
      <span className={'action-status status-' + action.status}>{action.status}</span>
    </div>
    {action.description && <p className='action-description'>{action.description}</p>}
    <blockquote className='action-evidence' aria-label='Evidence from the email'>{action.evidence}</blockquote>
    <dl className='action-metadata'>
      <div><dt>Deadline</dt><dd>{formatDeadline(action.due_at, action.due_precision)}</dd></div>
      <div><dt>Source</dt><dd>{sourceLabel(action.extraction_source)}</dd></div>
    </dl>
    {action.status === 'snoozed' && <p className='action-snoozed'>Snoozed until {formatDeadline(action.snoozed_until, 'exact_time')}</p>}
    <div className='action-controls'>
      {['open', 'snoozed'].includes(action.status) && <>
        <button className='button primary compact' disabled={busy || disabled} onClick={() => changeStatus('completed')}><Check size={14} />Complete</button>
        <button className='button ghost compact' disabled={busy || disabled} onClick={() => changeStatus('dismissed')}><Trash2 size={14} />Dismiss</button>
      </>}
      {['completed', 'dismissed', 'snoozed'].includes(action.status) && <button className='button secondary compact' disabled={busy || disabled} onClick={() => changeStatus('open')}><RotateCcw size={14} />Reopen</button>}
      <button ref={sourceButton} className='button ghost compact' disabled={busy || readDisabled} aria-haspopup='dialog' onClick={() => setShowSource(true)}><ExternalLink size={14} />Open source</button>
    </div>
    {action.status === 'open' && <div className='snooze-control'>
      <label htmlFor={'snooze-' + action.action_id}>Snooze until</label>
      <input id={'snooze-' + action.action_id} type='datetime-local' disabled={disabled} value={snoozedUntil} onChange={event => setSnoozedUntil(event.target.value)} />
      <button className='button secondary compact' disabled={busy || disabled || !snoozeValid} onClick={snooze}>Snooze</button>
    </div>}
    {busy && <p className='pending-line' role='status'>Saving action...</p>}
    {showSource && <SourceEmailDialog api={api} emailId={action.email_id} evidence={action.evidence} generation={generation}
      onClose={closeSource} onOpenInInbox={() => { setShowSource(false); openSource(action.email_id); }} />}
  </article>;
}

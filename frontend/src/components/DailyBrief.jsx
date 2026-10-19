import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Newspaper, RefreshCw, X } from 'lucide-react';
import { Surface, SectionTitle } from './ui/Surface';
import { SourceEmailDialog } from './SourceEmailDialog';
import { formatTime } from '../dashboard';

const POLL_MS = 1500;
const PROPOSAL_LABELS = { complete_action: 'Mark done', snooze_action: 'Snooze a day', draft_follow_up: 'Draft follow-up', mark_risky_reviewed: 'Mark checked' };
const AGENTS = { triage: 'Triage', research: 'Research', writer: 'Writer' };

function describe(step) {
  if (step.agent === 'triage') return `Triage chose ${step.chosen} of ${step.candidates} items`;
  if (step.agent === 'research') return step.stopped ? 'Research stopped at the time budget' : `Research found ${step.facts} verified fact${step.facts === 1 ? '' : 's'}`;
  if (step.agent === 'writer') return `Writer kept ${step.sentences} sentence${step.sentences === 1 ? '' : 's'}${step.dropped ? `, removed ${step.dropped} without verified facts` : ''}`;
  return AGENTS[step.agent] || step.agent;
}

export function DailyBrief({ snapshot, disabled, api, onOpenFollowUp }) {
  const generation = snapshot.session.generation;
  const [state, setState] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState({});
  const [source, setSource] = useState(null);
  const timer = useRef(null);
  const live = useRef(true);

  const load = useCallback(async () => {
    try {
      const result = await api.request('/briefs/today');
      if (!live.current || result.generation !== generation) return;
      setState(result);
      setError('');
      if (result.brief?.status === 'running') timer.current = window.setTimeout(load, POLL_MS);
    } catch (failure) {
      if (live.current) setError(failure.message);
    }
  }, [api, generation]);

  useEffect(() => {
    live.current = true;
    load();
    return () => { live.current = false; window.clearTimeout(timer.current); };
  }, [load]);

  const regenerate = async () => {
    setError('');
    try {
      await api.request('/briefs', { method: 'POST' });
      timer.current = window.setTimeout(load, 300);
    } catch (failure) {
      setError(failure.message);
    }
  };

  const decide = async (index, decision, proposal) => {
    setBusy(previous => ({ ...previous, [index]: true }));
    try {
      const result = await api.request(`/briefs/${state.brief.brief_id}/proposals/${index}/${decision}`, { method: 'POST' });
      if (result.open === 'follow-ups') onOpenFollowUp?.(proposal.message_id);
      await load();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(previous => ({ ...previous, [index]: false }));
    }
  };

  const row = state?.brief;
  const brief = row?.brief;
  const running = row?.status === 'running';
  let reference = 0;
  return <Surface className='daily-brief' aria-label='Daily brief'>
    <SectionTitle icon={<Newspaper size={18} />} eyebrow={row ? 'Brief for ' + new Date(row.brief_date + 'T12:00:00').toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' }) : 'Daily brief'} title='Your day in mail'
      aside={<button className='button ghost compact' disabled={disabled || running} onClick={regenerate}><RefreshCw size={13} className={running ? 'spin-icon' : ''} />{running ? 'Writing...' : 'Refresh brief'}</button>} />
    {!row && !error && <p className='empty-state'>Your brief is written each morning after {state?.scheduled_time || '08:30'}. Refresh to write one now.</p>}
    {running && <p className='refresh-status' role='status'>Three agents are reading today&apos;s mail: triage, research, then writing...</p>}
    {row?.status === 'failed' && <div className='section-error' role='alert'><strong>The brief could not be written.</strong><p>Try Refresh brief ({row.error_code}).</p></div>}
    {brief && !running && <>
      {brief.mode === 'fallback' && <p className='usage-note'>AI providers were unavailable, so this is a plain summary from your saved mail.</p>}
      {brief.mode === 'empty' && <p className='empty-state'>Nothing needs you today.</p>}
      {brief.sections.map(section => <section key={section.title} className='brief-section'>
        <h3>{section.title}</h3>
        <ul>{section.sentences.map((sentence, index) => <li key={index}>
          {sentence.text}
          {sentence.citations.map(citation => {
            reference += 1;
            const number = reference;
            return <button key={number} className='citation' aria-label={'Source ' + number + (citation.subject ? ': ' + citation.subject : '')} onClick={() => setSource(citation)}>{number}</button>;
          })}
        </li>)}</ul>
      </section>)}
      {brief.proposals.length > 0 && <section className='brief-proposals' aria-label='Suggested actions'>
        <h3>Suggested actions</h3>
        <ul>{brief.proposals.map((proposal, index) => <li key={index} className={proposal.state}>
          <span>{proposal.label}</span>
          {proposal.state === 'proposed' ? <span className='actions'>
            <button className='button primary compact' disabled={disabled || busy[index]} onClick={() => decide(index, 'approve', proposal)}><Check size={13} />{PROPOSAL_LABELS[proposal.type]}</button>
            <button className='button ghost compact' disabled={disabled || busy[index]} aria-label={'Dismiss: ' + proposal.label} onClick={() => decide(index, 'dismiss', proposal)}><X size={13} /></button>
          </span> : <span className='proposal-state'>{proposal.state === 'approved' ? 'Done' : 'Dismissed'}</span>}
        </li>)}</ul>
        <p className='usage-note'>Nothing happens until you approve it.</p>
      </section>}
      {row.trace.length > 0 && <details className='technical-details'>
        <summary>How this brief was made</summary>
        <div className='technical-content'>
          <ol>{row.trace.map((step, index) => <li key={index}>{describe(step)}{Number.isInteger(step.elapsed_ms) ? ` · ${(step.elapsed_ms / 1000).toFixed(1)} s` : ''}</li>)}</ol>
          <p>{row.latency_ms != null ? (row.latency_ms / 1000).toFixed(1) + ' s' : ''}{row.total_tokens ? ' · ' + row.total_tokens.toLocaleString() + ' tokens' : ''} · written {formatTime(row.updated_at)}. Every sentence is built only from facts whose quotes were verified in your email.</p>
        </div>
      </details>}
    </>}
    {error && <p className='card-error' role='alert'>{error}</p>}
    {source && <SourceEmailDialog api={api} emailId={source.email_id} evidence={source.quote} generation={generation}
      onClose={() => setSource(null)} onOpenInInbox={() => setSource(null)} />}
  </Surface>;
}

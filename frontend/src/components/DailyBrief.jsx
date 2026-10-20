import { useCallback, useEffect, useRef, useState } from 'react';
import { BadgeCheck, CalendarClock, Check, CircleAlert, Hourglass, Inbox, Info, ListChecks, Newspaper, RefreshCw, Repeat, ShieldAlert, Sparkles, TriangleAlert, X } from 'lucide-react';
import { Surface, SectionTitle } from './ui/Surface';
import { SourceEmailDialog } from './SourceEmailDialog';
import { formatTime } from '../dashboard';

const POLL_MS = 1500;
const PROPOSAL_LABELS = { complete_action: 'Mark done', snooze_action: 'Snooze a day', draft_follow_up: 'Draft follow-up', mark_risky_reviewed: 'Mark checked' };
const AGENTS = { triage: 'Triage', research: 'Research', writer: 'Writer' };
// Section icons by meaning; the agents choose their own titles, so match words.
const SECTION_ICONS = [
  [/overdue/i, TriangleAlert, 'overdue'],
  [/scam|risk|suspicious/i, ShieldAlert, 'risk'],
  [/renew|subscription|bill|money|payment/i, Repeat, 'money'],
  [/wait|repl|follow/i, Hourglass, 'waiting'],
  [/due|week|deadline|today|needs you/i, CalendarClock, 'due'],
  [/new|since|mail|inbox/i, Inbox, 'new'],
];

function sectionStyle(title) {
  const match = SECTION_ICONS.find(([pattern]) => pattern.test(title));
  return match ? { Icon: match[1], tone: match[2] } : { Icon: Sparkles, tone: 'new' };
}

function describe(step) {
  if (step.agent === 'triage') return `Triage chose ${step.chosen} of ${step.candidates} items`;
  if (step.agent === 'research') return step.stopped ? 'Research stopped at the time budget' : `Research found ${step.facts} verified fact${step.facts === 1 ? '' : 's'}`;
  if (step.agent === 'writer') return `Writer kept ${step.sentences} sentence${step.sentences === 1 ? '' : 's'}${step.dropped ? `, removed ${step.dropped} without verified facts` : ''}`;
  return AGENTS[step.agent] || step.agent;
}

function briefDate(row) {
  return new Date(row.brief_date + 'T12:00:00').toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
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
  const fallback = brief?.mode === 'fallback';
  const shown = brief && !running;
  let reference = 0;

  const aside = <div className='brief-aside'>
    {shown && brief.sections.length > 0 && (fallback
      ? <span className='brief-mode plain'><Info size={12} aria-hidden='true' />Plain summary</span>
      : <span className='assurance-badge'><BadgeCheck size={12} aria-hidden='true' />Every sentence cites your email</span>)}
    <button className='button ghost compact' disabled={disabled || running} onClick={regenerate}>
      <RefreshCw size={13} className={running ? 'spin-icon' : ''} />{running ? 'Writing...' : 'Refresh brief'}
    </button>
  </div>;

  return <Surface className='daily-brief' aria-label='Daily brief'>
    <SectionTitle icon={<Newspaper size={18} />} eyebrow={row ? 'Brief for ' + briefDate(row) : 'Daily brief'} title='Your day in mail' aside={aside} />

    {!row && !error && <div className='brief-empty'>
      <Newspaper size={20} aria-hidden='true' />
      <div><strong>No brief yet today</strong><p>It is written each morning after {state?.scheduled_time || '08:30'}. Refresh brief to write one now.</p></div>
    </div>}

    {running && <div className='brief-progress' role='status'>
      <span className='brief-steps' aria-hidden='true'><span>Triage</span><span>Research</span><span>Writer</span></span>
      <p>Reading today&apos;s mail: triage picks what matters, research checks each item against its email, then the writer drafts the brief. This usually takes under a minute.</p>
    </div>}

    {row?.status === 'failed' && <div className='section-error' role='alert'><strong>The brief could not be written.</strong><p>Try Refresh brief ({row.error_code}).</p></div>}

    {shown && <>
      {fallback && <p className='brief-note'><Info size={14} aria-hidden='true' />The AI providers were unavailable, so this is a plain summary from your saved mail. Refresh brief later for a written one.</p>}
      {(brief.mode === 'empty' || brief.sections.length === 0) && <div className='brief-empty'>
        <Check size={20} aria-hidden='true' />
        <div><strong>Nothing needs you today</strong><p>No overdue tasks, deadlines, scam warnings, renewals or unanswered threads.</p></div>
      </div>}

      {brief.sections.length > 0 && <div className='brief-sections'>
        {brief.sections.map(section => {
          const { Icon, tone } = sectionStyle(section.title);
          return <section key={section.title} className={'brief-section ' + tone}>
            <header>
              <span className='brief-section-icon'><Icon size={14} aria-hidden='true' /></span>
              <h3>{section.title}</h3>
              <span className='brief-count'>{section.sentences.length}</span>
            </header>
            <ul>{section.sentences.map((sentence, index) => {
              if (fallback && sentence.title) {
                return <li key={index} className='brief-item'>
                  <div><strong>{sentence.title}</strong><span>{[sentence.detail, sentence.when].filter(Boolean).join(' · ')}</span></div>
                  {sentence.email_id && <button className='button ghost compact' onClick={() => setSource({ email_id: sentence.email_id, quote: '' })}>Open</button>}
                </li>;
              }
              return <li key={index} className='brief-sentence'>
                <span>{sentence.text}</span>
                {sentence.citations.map(citation => {
                  reference += 1;
                  const number = reference;
                  return <button key={number} className='citation' aria-label={'Source ' + number + (citation.subject ? ': ' + citation.subject : '')} onClick={() => setSource(citation)}>{number}</button>;
                })}
              </li>;
            })}</ul>
          </section>;
        })}
      </div>}

      {brief.proposals.length > 0 && <section className='brief-proposals' aria-label='Suggested actions'>
        <header><ListChecks size={14} aria-hidden='true' /><h3>Suggested actions</h3><span>Nothing happens until you approve it.</span></header>
        <ul>{brief.proposals.map((proposal, index) => <li key={index} className={proposal.state}>
          <span className='proposal-label'>{proposal.label}</span>
          {proposal.state === 'proposed' ? <span className='actions'>
            <button className='button primary compact' disabled={disabled || busy[index]} onClick={() => decide(index, 'approve', proposal)}><Check size={13} />{PROPOSAL_LABELS[proposal.type]}</button>
            <button className='button ghost compact' disabled={disabled || busy[index]} aria-label={'Dismiss: ' + proposal.label} onClick={() => decide(index, 'dismiss', proposal)}><X size={13} />Dismiss</button>
          </span> : <span className={'proposal-state ' + proposal.state}>{proposal.state === 'approved' ? <><Check size={12} />Done</> : 'Dismissed'}</span>}
        </li>)}</ul>
      </section>}

      {row.trace.length > 0 && <details className='technical-details'>
        <summary>How this brief was made</summary>
        <div className='technical-content'>
          <ol>{row.trace.map((step, index) => <li key={index}>{describe(step)}{Number.isInteger(step.elapsed_ms) ? ` · ${(step.elapsed_ms / 1000).toFixed(1)} s` : ''}</li>)}</ol>
          <p>{row.latency_ms != null ? (row.latency_ms / 1000).toFixed(1) + ' s' : ''}{row.total_tokens ? ' · ' + row.total_tokens.toLocaleString() + ' tokens' : ''} · written {formatTime(row.updated_at)}. Every sentence is built only from facts whose quotes were verified in your email.</p>
        </div>
      </details>}
    </>}

    {error && <p className='card-error' role='alert'><CircleAlert size={13} />{error}</p>}
    {source && <SourceEmailDialog api={api} emailId={source.email_id} evidence={source.quote} generation={generation}
      onClose={() => setSource(null)} onOpenInInbox={() => setSource(null)} />}
  </Surface>;
}

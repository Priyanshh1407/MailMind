import { useCallback, useEffect, useRef, useState } from 'react';
import { BadgeCheck, CircleHelp, History, MessageSquareText, Search, Send, ShieldCheck } from 'lucide-react';
import { Surface, SectionTitle } from './ui/Surface';
import { SourceEmailDialog } from './SourceEmailDialog';
import { describeStep, numberCitations, questionOutcome } from '../ask';
import { formatCount, formatTime } from '../dashboard';

const EXAMPLES = [
  'Which emails still need a reply from me?',
  'How much did I spend on Swiggy this month?',
  'When does my next subscription renew?',
  'Did anyone send me a suspicious link this week?',
];
const POLL_MS = 900;
const POLL_LIMIT_MS = 75000;

function Answer({ item, onSource }) {
  const { sentences, sources } = numberCitations(item.answer);
  const outcome = questionOutcome(item);
  return <div className={'ask-answer ' + item.status}>
    {outcome && <p className='ask-outcome'><CircleHelp size={14} aria-hidden='true' />{outcome}</p>}
    {sentences.length > 0 && <p className='ask-text'>{sentences.map((sentence, index) => <span key={index}>
      {sentence.text}{sentence.refs.map(ref => <button key={ref} className='citation' aria-label={'Source ' + ref + ': ' + sources[ref - 1].subject} onClick={() => onSource(sources[ref - 1])}>{ref}</button>)}{' '}
    </span>)}</p>}
    {sources.length > 0 && <ol className='ask-sources' aria-label='Sources'>{sources.map((source, index) => <li key={index}>
      <button className='link-button' onClick={() => onSource(source)}>{source.subject || '(No subject)'}</button>
      <span>{source.date}</span>
      <q className='signal-evidence'>{source.quote}</q>
    </li>)}</ol>}
  </div>;
}

function Trace({ item }) {
  if (!item.trace.length) return null;
  const answer = item.answer;
  return <details className='technical-details ask-trace'>
    <summary>How this was answered ({item.trace.length} step{item.trace.length === 1 ? '' : 's'})</summary>
    <div className='technical-content'>
      <ol>{item.trace.map(step => <li key={step.step}>{describeStep(step)}{Number.isInteger(step.elapsed_ms) ? ` · ${(step.elapsed_ms / 1000).toFixed(1)} s` : ''}</li>)}</ol>
      <p>{item.latency_ms != null ? `${(item.latency_ms / 1000).toFixed(1)} s total` : 'Still running'}{item.total_tokens ? ` · ${formatCount(item.total_tokens)} tokens` : ''}
        {answer ? ` · ${answer.citations_verified}/${answer.citations_total} citations verified` : ''}
        {answer?.dropped_sentences ? ` · ${answer.dropped_sentences} unverified sentence${answer.dropped_sentences === 1 ? '' : 's'} removed` : ''}</p>
    </div>
  </details>;
}

export function AskPanel({ snapshot, disabled, api, openSource }) {
  const generation = snapshot.session.generation;
  const [question, setQuestion] = useState('');
  const [current, setCurrent] = useState(null);
  const [history, setHistory] = useState([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState(null);
  const poll = useRef(null);
  const live = useRef(true);

  const loadHistory = useCallback(async () => {
    try {
      const result = await api.request('/questions?limit=10');
      if (live.current && result.generation === generation) setHistory(result.questions);
    } catch { /* history is optional */ }
  }, [api, generation]);

  useEffect(() => {
    live.current = true;
    loadHistory();
    return () => { live.current = false; window.clearTimeout(poll.current); };
  }, [loadHistory]);

  const follow = (questionId, started) => {
    poll.current = window.setTimeout(async () => {
      try {
        const result = await api.request('/questions/' + questionId);
        if (!live.current || result.generation !== generation) return;
        setCurrent(result);
        if (result.status === 'running' && Date.now() - started < POLL_LIMIT_MS) follow(questionId, started);
        else { setBusy(false); loadHistory(); }
      } catch (failure) {
        if (live.current) { setError(failure.message); setBusy(false); }
      }
    }, POLL_MS);
  };

  const submit = async text => {
    const value = (text ?? question).trim();
    if (value.length < 3 || busy) return;
    setQuestion(value);
    setError('');
    setBusy(true);
    setCurrent({ question: value, status: 'running', trace: [], answer: null });
    try {
      const result = await api.request('/questions', { method: 'POST', body: { question: value } });
      follow(result.question_id, Date.now());
    } catch (failure) {
      setError(failure.message);
      setBusy(false);
      setCurrent(null);
    }
  };

  return <Surface className='ask-panel'>
    <SectionTitle icon={<MessageSquareText size={18} />} eyebrow='Questions about your saved mail' title='Ask your inbox' aside={<span className='estimated-badge'><BadgeCheck size={12} aria-hidden='true' /> Every sentence cites your email</span>} />
    <form className='ask-form' onSubmit={event => { event.preventDefault(); submit(); }}>
      <label htmlFor='ask-question' className='visually-hidden'>Your question</label>
      <Search size={16} aria-hidden='true' />
      <input id='ask-question' value={question} maxLength={300} placeholder='Ask about your email, e.g. "When is my electricity bill due?"' disabled={disabled} onChange={event => setQuestion(event.target.value)} />
      <button type='submit' className='button primary' disabled={disabled || busy || question.trim().length < 3}><Send size={14} />{busy ? 'Thinking...' : 'Ask'}</button>
    </form>
    {!current && <div className='ask-examples' aria-label='Example questions'>{EXAMPLES.map(item => <button key={item} className='filter-chip' disabled={disabled || busy} onClick={() => submit(item)}>{item}</button>)}</div>}
    {error && <p className='card-error' role='alert'>{error}</p>}
    {current && <section className='ask-result' aria-live='polite' aria-busy={current.status === 'running'}>
      <h3>{current.question}</h3>
      {current.status === 'running' && <p className='refresh-status' role='status'>{current.trace.length ? describeStep(current.trace[current.trace.length - 1]) + '...' : 'Searching your saved mail...'}</p>}
      {current.status !== 'running' && <Answer item={current} onSource={setSource} />}
      <Trace item={current} />
    </section>}
    <p className='usage-note'><ShieldCheck size={14} aria-hidden='true' />The AI reads only the emails it chooses to open, minimised the same way as for classification; senders are shown to it as domains and amounts as placeholders filled in on this computer. Claims it cannot back with a quote from your email are removed.</p>
    {history.length > 0 && <details className='ask-history'>
      <summary><History size={14} aria-hidden='true' />Recent questions</summary>
      <ul>{history.map(item => <li key={item.question_id}>
        <button className='link-button' onClick={() => { setCurrent(item); setQuestion(item.question); }}>{item.question}</button>
        <span>{formatTime(item.created_at)} · {item.status}</span>
      </li>)}</ul>
    </details>}
    {source && <SourceEmailDialog api={api} emailId={source.email_id} evidence={source.quote} generation={generation}
      onClose={() => setSource(null)} onOpenInInbox={() => { setSource(null); openSource(source.email_id); }} />}
  </Surface>;
}

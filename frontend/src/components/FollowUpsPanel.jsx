import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, CircleAlert, Clipboard, ClipboardCheck, ExternalLink, Mail, RefreshCw, Search, Send } from 'lucide-react';
import { Surface, SectionTitle } from './ui/Surface';
import { formatEmailTime, formatTime } from '../dashboard';

const POLL_MS = 1000;
const POLL_LIMIT_MS = 90000;
const gmailThreadUrl = threadId => 'https://mail.google.com/mail/u/0/#all/' + encodeURIComponent(threadId);

function recipientName(to) {
  const match = /^\s*"?([^"<]+?)"?\s*</.exec(to || '');
  return match ? match[1].trim() : (to || '').split(',')[0].trim() || 'Unknown recipient';
}

function SentRow({ item, onPick, disabled }) {
  return <li>
    <button className='sent-row' disabled={disabled} onClick={() => onPick(item)}>
      <span className='sent-row-top'>
        <strong>{recipientName(item.to)}</strong>
        <time>{formatEmailTime(new Date(item.sent_at).toISOString())}</time>
      </span>
      <span className='sent-row-subject'>{item.subject || '(No subject)'}</span>
      <span className='sent-row-snippet'>{item.snippet}</span>
      {item.reply_state === 'waiting' && <span className='waiting-badge'>No reply · {item.waiting_days} day{item.waiting_days === 1 ? '' : 's'}</span>}
      {item.reply_state === 'replied' && <span className='replied-badge'>Replied</span>}
    </button>
  </li>;
}

export function FollowUpsPanel({ snapshot, disabled, api }) {
  const generation = snapshot.session.generation;
  const [sent, setSent] = useState(null);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [picked, setPicked] = useState(null);
  const [result, setResult] = useState(null);
  const [text, setText] = useState('');
  const [length, setLength] = useState('standard');
  const [copied, setCopied] = useState(false);
  const live = useRef(true);
  const timer = useRef(null);

  const load = useCallback(async query => {
    try {
      const page = await api.request('/follow-ups/sent?limit=25&search=' + encodeURIComponent(query || ''));
      if (!live.current || page.generation !== generation) return null;
      setSent(page);
      return page;
    } catch (failure) {
      if (live.current) setError(failure.message);
      return null;
    }
  }, [api, generation]);

  const refresh = useCallback(async more => {
    setError('');
    try {
      await api.request('/follow-ups/sent/refresh', { method: 'POST', body: { more: Boolean(more) } });
    } catch (failure) {
      if (live.current) setError(failure.message);
    }
    const started = Date.now();
    const tick = async () => {
      const page = await load(search);
      if (page?.refreshing && Date.now() - started < 30000 && live.current) timer.current = window.setTimeout(tick, POLL_MS);
    };
    tick();
  }, [api, load, search]);

  // Opening the tab shows what is saved at once, then fetches the latest sent
  // mail, once per account session (the ref survives a remount).
  const fetchedFor = useRef(null);
  useEffect(() => {
    live.current = true;
    if (fetchedFor.current !== generation) {
      fetchedFor.current = generation;
      load('').then(() => refresh(false));
    } else load('');
    return () => { live.current = false; window.clearTimeout(timer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generation]);

  useEffect(() => {
    const handle = window.setTimeout(() => load(search), 300);
    return () => window.clearTimeout(handle);
  }, [search, load]);

  const write = async (item, size) => {
    setPicked(item);
    setResult({ status: 'running', summary: [] });
    setText('');
    setCopied(false);
    setError('');
    try {
      const created = await api.request('/follow-ups/drafts', { method: 'POST', body: { message_id: item.message_id, length: size } });
      const started = Date.now();
      const follow = async () => {
        try {
          const draft = await api.request('/follow-ups/drafts/' + created.follow_up_id);
          if (!live.current || draft.generation !== generation) return;
          setResult(draft);
          if (draft.status === 'running' && Date.now() - started < POLL_LIMIT_MS) timer.current = window.setTimeout(follow, POLL_MS);
          else if (draft.text) setText(draft.text);
        } catch (failure) {
          if (live.current) setError(failure.message);
        }
      };
      timer.current = window.setTimeout(follow, POLL_MS);
    } catch (failure) {
      setError(failure.message);
      setResult(null);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (result?.follow_up_id) api.request('/follow-ups/drafts/' + result.follow_up_id, { method: 'PATCH', body: { status: 'copied' } }).catch(() => {});
    } catch {
      setError('Copying was blocked by the browser. Select the text and copy it yourself.');
    }
  };

  if (picked) {
    const running = result?.status === 'running';
    return <Surface className='follow-ups-panel'>
      <button className='button ghost compact back-button' onClick={() => { setPicked(null); setResult(null); }}><ArrowLeft size={13} />Back to sent mail</button>
      <SectionTitle icon={<Send size={18} />} eyebrow={'To ' + recipientName(picked.to)} title={picked.subject || '(No subject)'} />
      {result?.summary?.length > 0 && <section className='thread-summary' aria-label='Thread summary'>
        <h3>Thread summary</h3>
        <ol>{result.summary.map((item, index) => <li key={index}><strong>{item.from}</strong> <span>{item.date}</span><p>{item.preview}</p></li>)}</ol>
      </section>}
      {running && <p className='refresh-status' role='status'>{result.summary?.length ? 'Writing the follow-up and checking its tone...' : 'Reading the thread...'}</p>}
      {result?.status === 'failed' && <div className='section-error' role='alert'><strong>The follow-up could not be written.</strong><p>{result.error_code === 'gmail_unavailable' ? 'Gmail could not be reached. Check the connection and try again.' : result.error_code === 'cloud_not_configured_or_unavailable' ? 'No AI provider is configured.' : 'Try again in a moment (' + result.error_code + ').'}</p></div>}
      {result?.text != null && !running && <section className='follow-up-draft' aria-label='Follow-up draft'>
        <label htmlFor='follow-up-text'>Your follow-up (edit anything before copying)</label>
        <textarea id='follow-up-text' value={text} rows={12} onChange={event => { setText(event.target.value); setCopied(false); }} />
        {result.issues.length > 0 && <div className='check-before-sending' role='note'>
          <strong><CircleAlert size={14} aria-hidden='true' />Check before sending</strong>
          <ul>{result.issues.map(issue => <li key={issue}>{issue}</li>)}</ul>
        </div>}
        {result.issues.length === 0 && <p className='usage-note'>Passed every check: your greeting and sign-off, no stock AI phrases, no facts that are not in the thread{result.revised ? ', after one revision' : ''}.</p>}
        <div className='actions follow-up-actions'>
          <button className='button primary' onClick={copy} disabled={!text.trim()}>{copied ? <ClipboardCheck size={14} /> : <Clipboard size={14} />}{copied ? 'Copied' : 'Copy'}</button>
          <a className='button secondary' href={gmailThreadUrl(picked.thread_id)} target='_blank' rel='noreferrer'><ExternalLink size={14} />Open in Gmail</a>
          <div className='window-selector' role='group' aria-label='Length'>
            {['short', 'standard'].map(size => <button key={size} className={'filter-chip ' + (length === size ? 'active' : '')} aria-pressed={length === size} onClick={() => setLength(size)}>{size === 'short' ? 'Short' : 'Standard'}</button>)}
          </div>
          <button className='button ghost' disabled={disabled || running} onClick={() => write(picked, length)}><RefreshCw size={14} />Regenerate</button>
        </div>
        <p className='usage-note'>Paste it as a reply in the Gmail thread and send it yourself. MailMind never sends mail.</p>
      </section>}
      {error && <p className='card-error' role='alert'>{error}</p>}
    </Surface>;
  }

  return <Surface className='follow-ups-panel'>
    <SectionTitle icon={<Mail size={18} />} eyebrow='From your sent mail' title='Follow-ups' aside={<button className='button ghost compact' disabled={disabled || sent?.refreshing} onClick={() => refresh(false)}><RefreshCw size={13} className={sent?.refreshing ? 'spin-icon' : ''} />{sent?.refreshing ? 'Fetching...' : 'Refresh'}</button>} />
    <p className='follow-ups-question'>Which email do you want to follow up on?</p>
    <div className='ask-form'>
      <Search size={16} aria-hidden='true' />
      <label htmlFor='sent-search' className='visually-hidden'>Search sent mail</label>
      <input id='sent-search' value={search} placeholder='Search by recipient, subject or text' onChange={event => setSearch(event.target.value)} />
    </div>
    {sent?.refreshing && <p className='refresh-status' role='status'>Fetching your sent mail...</p>}
    {sent?.error_code && <p className='card-error' role='alert'>Your sent mail could not be fetched{sent.error_code === 'gmail_unavailable' ? ': Gmail could not be reached.' : '.'} Showing what was saved.</p>}
    {sent && sent.messages.length > 0 && <ul className='sent-list' aria-label='Sent emails'>{sent.messages.map(item => <SentRow key={item.message_id} item={item} disabled={disabled} onPick={chosen => write(chosen, length)} />)}</ul>}
    {sent && !sent.messages.length && !sent.refreshing && <p className='empty-state'>{search ? 'No sent emails match this search.' : 'No sent emails saved yet. Refresh to fetch the last 90 days.'}</p>}
    {sent?.has_more && !search && <button className='button ghost compact' disabled={sent.refreshing} onClick={() => refresh(true)}>Load more</button>}
    {sent?.refreshed_at && <p className='usage-note'>Fetched {formatTime(sent.refreshed_at)}. Only headers and a short preview are read until you pick an email.</p>}
    {error && <p className='card-error' role='alert'>{error}</p>}
  </Surface>;
}

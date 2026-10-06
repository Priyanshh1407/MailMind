import { useEffect, useRef, useState } from 'react';
import { ExternalLink, Mail, X } from 'lucide-react';
import { categoryName, formatEmailTime, senderName } from '../dashboard';

// Split text around the first case-insensitive match of the action's quote,
// so the sentence that produced the action is highlighted in the email.
function highlight(text, quote) {
  const needle = (quote || '').trim();
  const at = needle ? text.toLowerCase().indexOf(needle.toLowerCase()) : -1;
  if (at < 0) return [text];
  return [text.slice(0, at), <mark key='quote'>{text.slice(at, at + needle.length)}</mark>, text.slice(at + needle.length)];
}

// The email an action came from, shown in place: no jump to the Inbox.
// Esc, the close button, or a click outside the panel closes it.
export function SourceEmailDialog({ api, emailId, evidence, generation, onClose, onOpenInInbox }) {
  const dialog = useRef(null);
  const [state, setState] = useState({ status: 'loading', email: null, error: '' });

  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal?.();
    const onKey = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    const controller = new AbortController();
    api.request('/emails?limit=1&email_id=' + encodeURIComponent(emailId), { signal: controller.signal })
      .then(page => {
        const email = page.emails[0];
        if (page.generation !== generation) throw new Error('The account changed. Close this and refresh.');
        setState(email ? { status: 'ready', email, error: '' }
          : { status: 'error', email: null, error: 'This email is no longer saved for this account.' });
      })
      .catch(error => {
        if (error.name !== 'AbortError') setState({ status: 'error', email: null, error: error.message });
      });
    return () => controller.abort();
  }, [api, emailId, generation]);

  const email = state.email;
  const text = email ? (email.body || email.body_snippet || '') : '';
  return <dialog ref={dialog} className='source-dialog' aria-labelledby='source-dialog-title'
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => { if (event.target === dialog.current) onClose(); }}>
    <div className='source-dialog-panel'>
      <header className='source-dialog-header'>
        <div className='source-dialog-title'>
          <Mail size={16} aria-hidden='true' />
          <div>
            <p className='section-eyebrow'>Source email</p>
            <h2 id='source-dialog-title'>{email ? (email.subject || '(No subject)') : 'Loading the email…'}</h2>
          </div>
        </div>
        <button type='button' className='source-dialog-close' aria-label='Close source email' onClick={onClose}><X size={16} /></button>
      </header>
      {state.status === 'loading' && <p className='source-dialog-status' role='status'>Loading the email…</p>}
      {state.status === 'error' && <p className='source-dialog-status card-error' role='alert'>{state.error}</p>}
      {email && <>
        <dl className='source-dialog-meta'>
          <div><dt>From</dt><dd title={email.sender}>{senderName(email.sender)}</dd></div>
          <div><dt>Received</dt><dd>{formatEmailTime(email.created_at)}</dd></div>
          <div><dt>Category</dt><dd>{email.effective_category ? categoryName(email.effective_category) : 'Needs Review'}</dd></div>
        </dl>
        {evidence && <p className='source-dialog-quote'><span>This action was based on:</span> <q>{evidence}</q></p>}
        <div className='source-dialog-body' tabIndex={0} aria-label='Email text'>{highlight(text, evidence)}</div>
      </>}
      <footer className='source-dialog-actions'>
        {email && <button type='button' className='button ghost compact' onClick={onOpenInInbox}><ExternalLink size={14} />Open in inbox</button>}
        <button type='button' className='button secondary compact' onClick={onClose}>Close</button>
      </footer>
    </div>
  </dialog>;
}

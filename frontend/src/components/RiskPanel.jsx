import { useState } from 'react';
import { ChevronDown, ShieldAlert, ShieldCheck } from 'lucide-react';
import { AnimatePresence, m } from 'motion/react';
import { EASE_OUT } from '../motion';
import { riskBandLabel, riskSignalLabel } from '../intelligence';

// The badge shown next to the category; nothing for low risk.
export function RiskBadge({ risk }) {
  if (!risk || risk.band === 'low') return null;
  return <span className={'risk-badge ' + risk.band} title='Scam check score (0-100)'>
    <ShieldAlert size={12} aria-hidden='true' />{riskBandLabel(risk.band)} · {risk.score}
  </span>;
}

// "Scam check": every rule that fired, with the evidence it quotes.
export function RiskPanel({ risk, emailId, disabled, pending, mutate }) {
  const [open, setOpen] = useState(false);
  if (!risk) return null;
  if (!risk.findings.length) return <p className='risk-clear'><ShieldCheck size={13} aria-hidden='true' />Scam check: no warning signs{risk.evidence_level === 'content_only' ? ' in the text (sender checks pending)' : ''}.</p>;
  const reviewable = risk.band !== 'low' && !risk.reviewed_at;
  return <details className={'risk-panel ' + risk.band} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><ShieldAlert size={14} aria-hidden='true' />Scam check: {riskBandLabel(risk.band).toLowerCase()} ({risk.score}/100)<m.span className='explanation-chevron' animate={{ rotate: open ? 180 : 0 }} transition={{ duration: 0.22, ease: EASE_OUT }}><ChevronDown size={14} aria-hidden='true' /></m.span></summary>
    <AnimatePresence initial={false}>{open && <m.div key='content' className='explanation-reveal' initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.26, ease: EASE_OUT }}>
      <div className='explanation-content'>
        <div className='risk-meter' role='meter' aria-valuemin={0} aria-valuemax={100} aria-valuenow={risk.score} aria-label='Scam risk score'><span style={{ width: risk.score + '%' }} /></div>
        <ul className='signal-list' aria-label='Scam warning signs'>
          {risk.findings.map(item => <li key={item.signal}>
            <span className='signal-chip'>{riskSignalLabel(item.signal)}</span>
            {item.evidence && <q className='signal-evidence'>{item.evidence}</q>}
            {item.detail && <span className='risk-detail'>{item.detail}</span>}
          </li>)}
        </ul>
        <p className='explanation-source'>{risk.evidence_level === 'content_only' ? 'Based on the email text and sender address; sender authentication and links are checked once this email’s security signals are captured.' : 'Based on Gmail’s sender checks, reply paths, links and the email text.'} Local rules only; nothing was sent to an AI provider. This is a warning, not a verdict: MailMind never hides or deletes mail because of it.</p>
        {risk.reviewed_at && <p className='risk-reviewed'>You marked this as reviewed.</p>}
        {reviewable && <button className='button secondary compact' disabled={disabled || pending} onClick={() => mutate(emailId, '/emails/' + encodeURIComponent(emailId) + '/risk/review')}>I&apos;ve checked this email</button>}
      </div>
    </m.div>}</AnimatePresence>
  </details>;
}

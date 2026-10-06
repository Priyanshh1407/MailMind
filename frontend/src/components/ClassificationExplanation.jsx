import { useState } from 'react';
import { ChevronDown, Sparkles } from 'lucide-react';
import { AnimatePresence, m } from 'motion/react';
import { EASE_OUT } from '../motion';
import { describeDecision, signalLabel, sourceLabel } from '../intelligence';

export function ClassificationExplanation({ analysis, decision = null }) {
  const [open, setOpen] = useState(false);
  if (!analysis) return null;
  // <details> keeps native keyboard and screen-reader behaviour; the content is
  // rendered only while open so it can grow in instead of snapping.
  return <details className='classification-explanation' onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><Sparkles size={14} aria-hidden='true' />Why this category?<m.span className='explanation-chevron' animate={{ rotate: open ? 180 : 0 }} transition={{ duration: 0.22, ease: EASE_OUT }}><ChevronDown size={14} aria-hidden='true' /></m.span></summary>
    <AnimatePresence initial={false}>{open && <m.div key='content' className='explanation-reveal' initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.26, ease: EASE_OUT }}>
    <div className='explanation-content'>
      <p className='explanation-summary'>{analysis.explanation_summary}</p>
      {analysis.signals.length > 0 && <ul className='signal-list' aria-label='Classification signals'>
        {analysis.signals.map(item => <li key={item.signal}>
          <span className='signal-chip'>{signalLabel(item.signal)}</span>
          {item.evidence && <q className='signal-evidence'>{item.evidence}</q>}
        </li>)}
      </ul>}
      {/* The whole decision, from what was saved with it. */}
      {decision && <dl className='decision-story' aria-label='How this category was chosen'>
        {describeDecision(decision).map(part => <div key={part.key} className={'decision-part ' + part.key}>
          <dt>{part.title}</dt><dd>{part.text}</dd>
        </div>)}
      </dl>}
      {/* The decision story already names the model and your corrections. */}
      {!decision && <p className='explanation-source'>
        Source: {sourceLabel(analysis.source)}
        {analysis.model_version ? ' - ' + analysis.model_version : ''}
        {analysis.retrieval_used ? ' - used your feedback precedent' : ''}
      </p>}
      {analysis.source === 'system' && <p className='system-explanation'>This is a system state explanation, not a model classification.</p>}
    </div>
    </m.div>}</AnimatePresence>
  </details>;
}

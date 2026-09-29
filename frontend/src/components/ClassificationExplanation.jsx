import { ChevronDown, Sparkles } from 'lucide-react';
import { signalLabel, sourceLabel } from '../intelligence';

export function ClassificationExplanation({ analysis }) {
  if (!analysis) return null;
  return <details className='classification-explanation'>
    <summary><Sparkles size={14} aria-hidden='true' />Why this category?<ChevronDown size={14} aria-hidden='true' /></summary>
    <div className='explanation-content'>
      <p>{analysis.explanation_summary}</p>
      {analysis.signals.length > 0 && <ul className='signal-list' aria-label='Classification signals'>
        {analysis.signals.map(item => <li key={item.signal}>
          <span>{signalLabel(item.signal)}</span>
          {item.evidence && <small>{item.evidence}</small>}
        </li>)}
      </ul>}
      <p className='explanation-source'>
        Source: {sourceLabel(analysis.source)}
        {analysis.model_version ? ' - ' + analysis.model_version : ''}
        {analysis.retrieval_used ? ' - used your feedback precedent' : ''}
      </p>
      {analysis.source === 'system' && <p className='system-explanation'>This is a system state explanation, not a model classification.</p>}
    </div>
  </details>;
}

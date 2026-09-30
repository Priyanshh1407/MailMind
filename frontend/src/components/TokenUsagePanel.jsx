import { lazy, Suspense } from 'react';
import { m } from 'motion/react';
import { BarChart3, Cloud, Cpu, Info, MoveDown, MoveUp } from 'lucide-react';
import { formatCount } from '../dashboard';
import { countLabel, hasEstimatedUsage, operationLabel, sourceLabel } from '../intelligence';
import { Surface, SectionTitle } from './ui/Surface';
import { AnimatedCount } from './ui/AnimatedCount';
import { EASE_OUT, staggerDelay } from '../motion';

// Recharts is the largest dependency and only this tab uses it: load it on demand.
const TokenUsageChart = lazy(() => import('./TokenUsageChart')
  .then(module => ({ default: module.TokenUsageChart })));

const WINDOWS = [
  { value: 'day', label: 'Today' },
  { value: 'week', label: 'Week' },
  { value: 'month', label: 'Month' },
];

function UsageMetric({ icon: Icon, label, value }) {
  return <div className='usage-metric'><Icon size={16} aria-hidden='true' /><span>{label}</span><strong><AnimatedCount value={value} /></strong></div>;
}

// Ranked share bars: one series, so one hue; values stay in text colours.
function Breakdown({ title, rows, label }) {
  const ranked = [...rows].sort((a, b) => b.total_tokens - a.total_tokens);
  const total = ranked.reduce((sum, row) => sum + row.total_tokens, 0);
  const largest = ranked[0]?.total_tokens || 0;
  return <section className='usage-breakdown'>
    <h3>{title}</h3>
    {ranked.length ? <ul>{ranked.map((row, index) => {
      // Events whose tokens were never counted are not "0 tokens".
      const unmeasured = row.total_tokens === 0 && row.unknown_events > 0;
      const share = total ? row.total_tokens / total : 0;
      const detail = unmeasured ? countLabel(row.event_count, 'event') + ' · token count not reported'
        : Math.round(share * 100) + '% of tokens · ' + countLabel(row.event_count, 'event');
      return <li key={row.key} title={label(row.key) + ': ' + (unmeasured ? 'not measured' : formatCount(row.total_tokens) + ' tokens') + ' · ' + countLabel(row.event_count, 'event')}>
        <div className='breakdown-line'>
          <span className='breakdown-name'>{label(row.key)}</span>
          <strong className={unmeasured ? 'breakdown-value unmeasured' : 'breakdown-value'}>{unmeasured ? 'Not measured' : formatCount(row.total_tokens)}</strong>
        </div>
        <div className='breakdown-track' aria-hidden='true'>
          {/* Bars grow from the baseline, largest first, so the ranking reads in order. */}
          <m.span className='breakdown-bar' initial={{ scaleX: 0 }} animate={{ scaleX: 1 }} transition={{ duration: 0.6, ease: EASE_OUT, delay: 0.1 + staggerDelay(index, 0.06) }} style={{ width: largest && !unmeasured ? Math.max(2, row.total_tokens / largest * 100) + '%' : '0%' }} />
        </div>
        <small>{detail}</small>
      </li>;
    })}</ul> : <p className='breakdown-empty'>No token events in this window.</p>}
  </section>;
}

export function TokenUsagePanel({ snapshot, setQuery }) {
  const usage = snapshot.tokenUsage;
  const summary = usage.summary;
  return <Surface className='token-panel'>
    <SectionTitle icon={<BarChart3 size={18} />} eyebrow='Measured usage' title='Token usage' aside={hasEstimatedUsage(summary) ? <span className='estimated-badge'>Includes estimates</span> : null} />
    <div className='window-selector' role='group' aria-label='Token usage window'>
      {WINDOWS.map(item => <button key={item.value} className={'filter-chip ' + (summary.window === item.value ? 'active' : '')} aria-pressed={summary.window === item.value} onClick={() => setQuery(previous => ({ ...previous, tokenWindow: item.value }))}>{item.label}</button>)}
    </div>
    {usage.state === 'error' && <div className='section-error' role='alert'><strong>Token analytics could not be loaded.</strong><p>{usage.error.message}</p></div>}
    {usage.state === 'disabled' && <p className='empty-state'>Token collection is disabled. No usage is inferred or fabricated.</p>}
    {usage.state === 'unavailable' && <p className='empty-state'>Connect Google before account usage can be displayed.</p>}
    {usage.state === 'ready' && <>
      <div className='usage-metrics'>
        <UsageMetric icon={MoveDown} label='Input' value={summary.totals.input_tokens} />
        <UsageMetric icon={MoveUp} label='Output' value={summary.totals.output_tokens} />
        <UsageMetric icon={Cloud} label='Cloud billed' value={summary.provider_billed_tokens} />
        <UsageMetric icon={Cpu} label='Local processed' value={summary.local_processed_tokens} />
      </div>
      <p className='usage-note'><Info size={14} />Token counts are operational usage, not a cost estimate. Local processing is shown separately from provider-billed usage.</p>
      <Suspense fallback={<p className='usage-note' role='status'>Loading chart...</p>}>
        <TokenUsageChart daily={summary.daily} />
      </Suspense>
      <div className='breakdown-grid'>
        <Breakdown title='By provider' rows={summary.providers} label={sourceLabel} />
        <Breakdown title='By operation' rows={summary.operations} label={operationLabel} />
      </div>
    </>}
  </Surface>;
}

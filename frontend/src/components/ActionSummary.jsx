import { CircleDot, ClockAlert, Coins, TriangleAlert } from 'lucide-react';
import { AnimatedCount } from './ui/AnimatedCount';

function SummaryItem({ icon: Icon, label, value, state }) {
  return <div className='intelligence-kpi'>
    <Icon size={16} aria-hidden='true' />
    <span>{label}</span>
    <strong>{state === 'ready' ? <AnimatedCount value={value} /> : '-'}</strong>
  </div>;
}

export function ActionSummary({ snapshot }) {
  const actions = snapshot?.actions;
  const usage = snapshot?.tokenUsage;
  const summary = actions?.summary;
  return <section className='intelligence-summary' aria-label='Intelligence summary'>
    <SummaryItem icon={CircleDot} label='Open' value={summary?.status_counts.open} state={actions?.summaryState} />
    <SummaryItem icon={ClockAlert} label='Due soon' value={summary?.due_soon} state={actions?.summaryState} />
    <SummaryItem icon={TriangleAlert} label='Overdue' value={summary?.overdue} state={actions?.summaryState} />
    <SummaryItem icon={Coins} label='Tokens today' value={usage?.today.totals.total_tokens} state={usage?.todayState} />
  </section>;
}

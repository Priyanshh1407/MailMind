import { Activity, CheckCircle2, CircleDot, ClockAlert, Coins, Database, Tag, TriangleAlert } from 'lucide-react';
import { Surface } from './ui/Surface';
import { formatCount, formatTenthsAsSeconds, millisecondsToTenthsUp } from '../dashboard';
import { AnimatedCount } from './ui/AnimatedCount';

function Stat({ label, value, detail, icon: Icon }) {
  return <Surface className="stat">
    <h2>{label}</h2><Icon className="stat-icon" size={15} aria-hidden="true" />
    <strong>{value}</strong>
    <p>{detail}</p>
  </Surface>;
}

// One overview for the whole dashboard: inbox health on the first row, your
// follow-through (actions) and usage on the second, all in the same card design.
export function DashboardStats({ snapshot }) {
  const totals = snapshot?.telemetry?.totals;
  const timing = snapshot?.telemetry?.classification_timing;
  const actions = snapshot?.actions;
  const summary = actions?.summary;
  const actionsReady = actions?.summaryState === 'ready';
  const usage = snapshot?.tokenUsage;
  const tenths = millisecondsToTenthsUp(timing?.mean_ms);
  const completionPct = totals?.saved ? ((totals.completed / totals.saved) * 100).toFixed(1) + '% workflow completion' : 'Messages that completed the workflow';
  const actionValue = value => actionsReady ? <AnimatedCount value={value} /> : '-';
  return <section className="stats-grid overview-grid" aria-label="Overview">
    <Stat icon={Database} label="Saved emails" value={<AnimatedCount value={totals?.saved} />} detail="Securely stored for this account" />
    <Stat icon={CheckCircle2} label="Processed" value={<AnimatedCount value={totals?.completed} />} detail={totals ? completionPct : 'Messages that completed the workflow'} />
    <Stat icon={Tag} label="Feedback" value={<AnimatedCount value={totals?.labelled} />} detail={totals ? totals.corrected + ' corrected · ' + totals.confirmed + ' confirmed' : 'Corrections improve future decisions'} />
    <Stat icon={Activity} label="Avg. classification" value={tenths == null ? '-' : <><AnimatedCount value={tenths} format={formatTenthsAsSeconds} /> s</>} detail={timing ? 'Across ' + formatCount(timing.sample_count) + ' measured samples' : 'Waiting for measured results'} />
    <Stat icon={CircleDot} label="Open actions" value={actionValue(summary?.status_counts.open)} detail="Tasks found in your mail" />
    <Stat icon={ClockAlert} label="Due soon" value={actionValue(summary?.due_soon)} detail="Due in the next 7 days" />
    <Stat icon={TriangleAlert} label="Overdue" value={actionValue(summary?.overdue)} detail="Past their deadline" />
    <Stat icon={Coins} label="Tokens today" value={usage?.todayState === 'ready' ? <AnimatedCount value={usage.today.totals.total_tokens} /> : '-'} detail="Cloud and local, since midnight" />
  </section>;
}

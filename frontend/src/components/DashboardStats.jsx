import { Activity, CheckCircle2, Database, Tag } from 'lucide-react';
import { Surface } from './ui/Surface';
import { formatCount } from '../dashboard';

function Stat({ label, value, detail, icon: Icon }) {
  return <Surface className="stat">
    <h2>{label}</h2><Icon className="stat-icon" size={15} />
    <strong>{value}</strong>
    <p>{detail}</p>
  </Surface>;
}

export function DashboardStats({ snapshot }) {
  const totals = snapshot?.telemetry?.totals;
  const timing = snapshot?.telemetry?.classification_timing;
  const completionPct = totals?.saved ? ((totals.completed / totals.saved) * 100).toFixed(1) + '% workflow completion' : 'Messages that completed the workflow';
  return <section className="stats-grid" aria-label="Account statistics">
    <Stat icon={Database} label="Saved emails" value={formatCount(totals?.saved)} detail="Securely stored for this account" />
    <Stat icon={CheckCircle2} label="Processed" value={formatCount(totals?.completed)} detail={totals ? completionPct : 'Messages that completed the workflow'} />
    <Stat icon={Tag} label="Feedback" value={formatCount(totals?.labelled)} detail={totals ? totals.corrected + ' corrected · ' + totals.confirmed + ' confirmed' : 'Corrections improve future decisions'} />
    <Stat icon={Activity} label="Avg. classification" value={timing?.mean_ms == null ? '-' : Math.round(timing.mean_ms).toLocaleString() + ' ms'} detail={timing ? 'Across ' + formatCount(timing.sample_count) + ' measured samples' : 'Waiting for measured results'} />
  </section>;
}

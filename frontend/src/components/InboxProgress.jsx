import { Activity, MailCheck } from 'lucide-react';
import { formatEmailTime } from '../dashboard';
import { SectionTitle, Surface } from './ui/Surface';
import { AnimatedCount } from './ui/AnimatedCount';

export function InboxProgress({ snapshot }) {
  const status = snapshot.status;
  const ingestion = status.ingestion;
  const counts = status.processing_counts || {};
  const waiting = (counts.queued || 0) + (counts.retry || 0);
  const stats = [
    ['Fetched & saved', snapshot.telemetry?.totals?.saved ?? status.workflow_total_tasks],
    ['Waiting', waiting],
    ['Classifying now', counts.running || 0],
    ['Completed', counts.complete || 0],
    ['Needs review', counts.dead || 0],
  ];
  const activity = status.is_polling ? 'Worker is checking Gmail now'
    : status.active_pending_tasks > 0 ? 'Saved work will continue automatically'
      : ingestion?.backlog_authorized ? 'Waiting for the next Gmail check'
        : 'No active inbox work';
  const target = status.current_batch_target_tasks || 0;
  const admitted = status.current_batch_admitted_tasks || 0;
  const percent = target ? Math.min(100, Math.round(admitted / target * 100)) : 0;

  return <Surface className="progress-panel" aria-label="Inbox work progress" aria-live="polite">
    <SectionTitle icon={<Activity size={16} />} eyebrow="Live work" title="Inbox work progress" aside={<span className="inline-status success"><span className="status-dot success" />{activity}</span>} />
    <div className="progress-value-row">
      <div><strong><AnimatedCount value={admitted} /> <span>of {target || 100} admitted</span></strong><p>Workflow finished {status.workflow_finished_tasks} of {status.workflow_total_tasks}</p></div>
      <span><AnimatedCount value={percent} />%</span>
    </div>
    <div className="progress-track" role="progressbar" aria-label="Current older-mail batch" aria-valuemin="0" aria-valuemax={target || 100} aria-valuenow={admitted}><span style={{ width: percent + '%' }} /></div>
    <div className="progress-counts">{stats.map(([label, value]) => <div key={label}><strong><AnimatedCount value={value} /></strong><span>{label}</span></div>)}</div>
    {ingestion && <div className="progress-footer">
      <span><MailCheck size={14} /> Latest check{['error', 'partial', 'deferred'].includes(ingestion.status) ? ' (' + ingestion.status + ')' : ''} fetched {ingestion.fetched_count} {ingestion.fetched_count === 1 ? 'message' : 'messages'} · {formatEmailTime(ingestion.last_checked_at)}</span>
      {ingestion.has_more && <span>More historical pages remain</span>}
    </div>}
  </Surface>;
}

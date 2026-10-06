import { RefreshCw, Sparkles, Wifi } from 'lucide-react';
import { formatEmailTime } from '../dashboard';
import { SectionTitle, Surface } from './ui/Surface';

export function InboxIntake({ snapshot, loading, disabled, pending, mutate }) {
  const status = snapshot.status;
  const ingestion = status.ingestion;
  const offline = snapshot.telemetry?.mode?.local_only;
  const backfill = status.intelligence_backfill;
  const backfillActive = Boolean(
    backfill && backfill.queued + backfill.running + backfill.retry > 0);
  return <Surface className="intake-panel" aria-label="Inbox intake controls">
    <SectionTitle icon={<Wifi size={16} />} title="Inbox intake" aside={<span className={'inline-status ' + (status.live_monitoring ? 'success' : 'neutral')}><span className={'status-dot ' + (status.live_monitoring ? 'success' : 'neutral')} />{status.live_monitoring ? 'Live monitoring' : 'Monitoring unavailable'}</span>} />
    <div className="intake-grid">
      <div><span>Active tasks</span><strong>{status.active_pending_tasks} / {status.max_pending_tasks}</strong></div>
      <div><span>Live pending</span><strong>{status.live_pending_tasks} messages</strong></div>
      <div><span>Historical backlog</span><strong>{status.backlog_pending_tasks} pending</strong></div>
      <div><span>Last new-mail sync</span><strong>{ingestion?.last_live_sync_at ? ingestion.live_status + ' / ' + formatEmailTime(ingestion.last_live_sync_at) : 'Not recorded'}</strong></div>
    </div>
    <div className="actions intake-actions">
      <button className="button primary" disabled={loading || disabled || pending.sync || status.purge_pending || offline} onClick={() => mutate('sync', '/inbox/sync')}>
        <RefreshCw size={14} className={pending.sync ? 'spin-icon' : ''} />{pending.sync ? 'Checking Gmail...' : 'Sync new messages'}
      </button>
      <button className="button secondary" disabled={loading || disabled || pending.backfill || backfillActive || !backfill?.enabled || !backfill?.eligible || offline} onClick={() => mutate('backfill', '/intelligence/backfill', { body: { limit: 20 } })}>
        <Sparkles size={14} />{pending.backfill ? 'Queuing...' : 'Analyze up to 20 saved emails'}
      </button>
    </div>
    {ingestion?.has_more && <p className="panel-note">Older mail loads automatically, 20 at a time, as you page towards the end of your inbox. New mail always comes first.</p>}
    {backfill && <p className="panel-note">{backfill.enabled ? <>Intelligence backfill: {backfill.eligible} eligible / {backfill.queued + backfill.running + backfill.retry} active / {backfill.complete} complete. It never sends historical alerts, marks mail read, or creates automatic reminders.</> : <>Saved-mail analysis is unavailable until Action Center extraction is enabled.</>}</p>}
  </Surface>;
}

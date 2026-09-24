import { Archive, RefreshCw, Wifi } from 'lucide-react';
import { formatEmailTime } from '../dashboard';
import { SectionTitle, Surface } from './ui/Surface';

export function InboxIntake({ snapshot, loading, disabled, pending, mutate }) {
  const status = snapshot.status;
  const ingestion = status.ingestion;
  const offline = snapshot.telemetry?.mode?.local_only;
  const fetchNextReady = Boolean(status.fetch_next_available);
  return <Surface className="intake-panel" aria-label="Inbox intake controls">
    <SectionTitle icon={<Wifi size={16} />} title="Inbox intake" aside={<span className={'inline-status ' + (status.live_monitoring ? 'success' : 'neutral')}><span className={'status-dot ' + (status.live_monitoring ? 'success' : 'neutral')} />{status.live_monitoring ? 'Live monitoring' : 'Monitoring unavailable'}</span>} />
    <div className="intake-grid">
      <div><span>Active tasks</span><strong>{status.active_pending_tasks} / {status.max_pending_tasks}</strong></div>
      <div><span>Live pending</span><strong>{status.live_pending_tasks} messages</strong></div>
      <div><span>Historical backlog</span><strong>{status.backlog_pending_tasks} pending</strong></div>
      <div><span>Last new-mail sync</span><strong>{ingestion?.last_live_sync_at ? ingestion.live_status + ' · ' + formatEmailTime(ingestion.last_live_sync_at) : 'Not recorded'}</strong></div>
    </div>
    <div className="actions intake-actions">
      <button className="button primary" disabled={loading || disabled || pending.sync || status.purge_pending || offline} onClick={() => mutate('sync', '/inbox/sync')}>
        <RefreshCw size={14} className={pending.sync ? 'spin-icon' : ''} />{pending.sync ? 'Checking Gmail...' : 'Sync new messages'}
      </button>
      <button className="button secondary" disabled={loading || disabled || pending.extract || !fetchNextReady || offline} onClick={() => mutate('extract', '/ingestion/fetch-next')}>
        <Archive size={14} />{pending.extract ? 'Authorizing...' : 'Fetch next 100'}
      </button>
    </div>
    {status.ingestion_paused && !fetchNextReady && <p className="panel-note">Older-mail intake is paused while this batch finishes. New mail still receives priority.</p>}
  </Surface>;
}

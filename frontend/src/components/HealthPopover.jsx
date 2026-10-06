import { useEffect, useRef, useState } from 'react';
import { localModelSummary } from '../dashboard';
import { BrainCircuit, ChevronDown, Cloud, RefreshCw, Server, ShieldCheck, Sparkles } from 'lucide-react';
import { formatTime, workerState } from '../dashboard';

function HealthItem({ icon: Icon, title, value, note }) {
  return <div className="health-item">
    <div className="health-item-label"><Icon size={14} />{title}</div>
    <p>{value}</p>
    <small>{note}</small>
  </div>;
}

export function HealthPopover({ snapshot, stale }) {
  const [open, setOpen] = useState(false);
  const container = useRef(null);
  const telemetry = snapshot?.telemetry;
  const status = snapshot?.status;
  const local = telemetry?.local_model;
  const healthy = Boolean(snapshot) && !stale && !status?.worker?.last_error_code;

  useEffect(() => {
    if (!open) return undefined;
    const onDocument = event => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('keydown', onDocument);
    return () => {
      document.removeEventListener('keydown', onDocument);
    };
  }, [open]);

  const localModel = localModelSummary(local);
  const providerName = value => value === 'disabled_local_only' ? 'Disabled' : value === 'unconfigured' ? 'Not configured' : 'Configured';

  return <div ref={container} className="health-menu-container">
    <button className={'header-pill health-trigger ' + (healthy ? 'healthy' : '')} type="button" aria-expanded={open} aria-controls="health-popover" onClick={() => setOpen(value => !value)}>
      <ShieldCheck size={16} />
      <span>{healthy ? 'Systems healthy' : stale ? 'Systems need attention' : 'Checking systems'}</span>
      <ChevronDown size={14} className={open ? 'rotate-icon' : ''} />
    </button>
    {open && <section id="health-popover" className="health-popover" aria-label="System health details">
      <div className="health-popover-head">
        <div><p className="section-eyebrow">SYSTEM STATUS</p><h2>System health</h2></div>
        <span className={healthy ? 'health-state success' : 'health-state warning'}><span className={'status-dot ' + (healthy ? 'success' : 'warning')} />{healthy ? 'Operational' : stale ? 'Last known state' : 'Checking'}</span>
      </div>
      {telemetry?.mode?.local_only && <p className="health-callout">Local-only mode is active. Cloud, Gmail and external alerts are disabled.</p>}
      <div className="health-grid">
        <HealthItem icon={BrainCircuit} title="Local model" value={localModel.value} note={localModel.note} />
        <HealthItem icon={Cloud} title="Cloud providers" value={'Gemini: ' + providerName(telemetry?.providers?.gemini) + ' · Groq: ' + providerName(telemetry?.providers?.groq)} note="Independent provider fallbacks" />
        <HealthItem icon={Server} title="Worker" value={snapshot ? workerState(snapshot) : 'Waiting for status'} note={'Heartbeat ' + formatTime(status?.worker?.heartbeat_at)} />
        <HealthItem icon={Sparkles} title="Latest classification" value={telemetry?.latest_prediction ? telemetry.latest_prediction.source + ' - ' + telemetry.latest_prediction.outcome : 'Not recorded'} note={formatTime(telemetry?.latest_prediction?.created_at)} />
        <HealthItem icon={RefreshCw} title="Dashboard refresh" value={snapshot ? 'Completed in ' + Math.round(snapshot.refreshMs) + ' ms' : 'Loading'} note={'Checked ' + formatTime(snapshot?.checkedAt)} />
      </div>
      {status?.worker?.last_error_code && <p className="health-callout danger">Current worker issue ({formatTime(status.worker.last_error_at)}): {status.worker.last_error_code}</p>}
      <div className="health-footer">
        <span>Last cycle: {formatTime(status?.worker?.last_success_at)}</span>
        <span>Feedback pending: {status?.feedback_index_pending ?? 0}</span>
        <span>Automatic mark-as-read: {status?.auto_mark_read ? 'On' : 'Off'}</span>
      </div>
    </section>}
  </div>;
}

import { formatTime, workerState } from '../dashboard';
function Stat({ label, value, detail }) { return <div className="glass-card stat"><h2>{label}</h2><strong>{value}</strong><p>{detail}</p></div>; }
export function DashboardStats({ snapshot, stale }) {
  const telemetry = snapshot?.telemetry, totals = telemetry?.totals, timing = telemetry?.classification_timing;
  return <section aria-label="Account statistics"><div className="stats-grid">
      <Stat label="Saved emails" value={totals?.saved ?? '—'} detail="Messages securely stored for this account" />
      <Stat label="Processed" value={totals?.completed ?? '—'} detail="Messages that completed the workflow" />
      <Stat label="Feedback" value={totals?.labelled ?? '—'} detail={totals ? totals.corrected + ' corrected · ' + totals.confirmed + ' confirmed' : 'Corrections improve future decisions'} />
      <Stat label="Average classification" value={timing?.mean_ms == null ? '—' : Math.round(timing.mean_ms) + ' ms'} detail={timing ? timing.sample_count + ' measured samples' : 'Waiting for measured results'} />
    </div>
    {telemetry && <details className="glass-card health-panel"><summary>System health {stale ? '· showing last known state' : '· services and model status'}</summary>
      <div className="health-content">
        {telemetry.mode?.local_only && <p>Local-only mode is active. Cloud, Gmail and external alerts are disabled.</p>}
        <div className="health-grid"><p><b>Local model:</b> {telemetry.local_model.ready ? telemetry.local_model.evaluation_scope === 'synthetic_benchmark_only' ? 'Loaded · synthetic benchmark only' : 'Loaded · quality not yet evaluated' : telemetry.local_model.status === 'loading' ? 'Loading · cloud is handling requests' : 'Unavailable for three-category inference'}{telemetry.local_model.version ? ' (' + telemetry.local_model.version + ')' : ''}</p>
          <p><b>Gemini:</b> {telemetry.providers.gemini === 'disabled_local_only' ? 'Disabled' : telemetry.providers.gemini === 'unconfigured' ? 'Not configured' : 'Configured'}</p>
          <p><b>Groq:</b> {telemetry.providers.groq === 'disabled_local_only' ? 'Disabled' : telemetry.providers.groq === 'unconfigured' ? 'Not configured' : 'Configured'}</p>
          <p><b>Worker:</b> {workerState(snapshot)} · heartbeat {formatTime(snapshot.status.worker?.heartbeat_at)}</p>
        </div>
        <p>Last completed cycle: {formatTime(snapshot.status.worker?.last_success_at)}{snapshot.status.worker?.last_error_code ? ' · Latest issue: ' + snapshot.status.worker.last_error_code : ''}</p>
        {telemetry.latest_prediction && <p>Latest classification: {telemetry.latest_prediction.source} · {telemetry.latest_prediction.outcome} · {formatTime(telemetry.latest_prediction.created_at)}</p>}
        <p>Feedback updates pending: {snapshot.status.feedback_index_pending ?? 0} · Saved feedback events: {totals.feedback_events}</p>
        <p>Automatic mark-as-read: {snapshot.status.auto_mark_read ? 'On' : 'Off'}</p>
        <p className="muted">Dashboard refreshed in {Math.round(snapshot.refreshMs)} ms · {formatTime(snapshot.checkedAt)}</p>
      </div>
    </details>}
  </section>;
}
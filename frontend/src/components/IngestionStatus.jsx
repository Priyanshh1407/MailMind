import { CircleAlert } from 'lucide-react';
import { formatTime } from '../dashboard';

export function IngestionStatus({ snapshot, disabled, pending, mutate }) {
  const ingestion = snapshot.status.ingestion;
  const failures = snapshot.status.ingestion_failures || [];
  if (!ingestion && !failures.length) return null;
  const message = ingestion?.status === 'error' ? 'The last inbox check failed; this does not mean it was empty.'
    : ingestion?.status === 'partial' ? 'The last inbox check had some failures; good emails were kept.'
      : ingestion?.status === 'empty' ? 'The last inbox check found no new unread emails.'
        : ingestion?.status === 'deferred' ? 'Inbox checking was deferred; another check is needed.'
          : 'The last inbox check fetched emails.';
  return <div className="ingestion-stack">
    {ingestion && <div className="status-callout" role="status">
      <CircleAlert size={16} />
      <p>{message} <span>Fetched: {ingestion.fetched_count}. Failed: {ingestion.failed_count}. Checked: {formatTime(ingestion.last_checked_at)}.{ingestion.has_more ? ' More pages remain.' : ''}</span></p>
    </div>}
    {failures.length > 0 && <details className="status-callout failures">
      <summary>Emails needing fetch or parsing recovery ({failures.length} recent)</summary>
      {failures.map(row => <div className="failure-row" key={row.email_id}>
        <p>Message {row.email_id}: {row.status} - {row.error_code} - Attempts: {row.attempt_count}</p>
        <button className="button secondary compact" disabled={disabled || pending[row.email_id]} onClick={() => mutate(row.email_id, '/tasks/' + encodeURIComponent(row.email_id) + '/retry')}>Retry fetching message</button>
      </div>)}
    </details>}
  </div>;
}

import { ArrowLeft, ArrowRight } from 'lucide-react';

export function Pagination({ page, query, setQuery, loading, status }) {
  // Older mail loads automatically; say so near the end of the list.
  const browsing = !query.search && !query.category && !query.emailId;
  const nearEnd = page.offset + 2 * page.limit >= page.total;
  const ingestion = status?.ingestion;
  const loadingOlder = browsing && nearEnd && Boolean(ingestion?.backlog_authorized || status?.backlog_pending_tasks);
  const reachedOldest = browsing && !page.has_more && page.total > 0 && ingestion && !ingestion.has_more;
  return <nav className="pagination" aria-label="Email pages">
    <div><p>{page.total === 0 ? 'No matching saved emails' : (page.offset + 1) + '–' + Math.min(page.offset + page.emails.length, page.total) + ' of ' + page.total + ' matching saved emails'}</p><small>{loadingOlder ? 'Loading older emails from Gmail…' : reachedOldest ? 'You have reached the oldest email in your inbox' : 'Results update automatically · 20 messages per page'}</small></div>
    <div className="actions">
      <button className="button secondary compact" disabled={loading || query.offset === 0} onClick={() => setQuery(previous => ({ ...previous, offset: Math.max(0, previous.offset - 20) }))}><ArrowLeft size={14} />Previous page</button>
      <button className="button secondary compact" disabled={loading || !page.has_more} onClick={() => setQuery(previous => ({ ...previous, offset: previous.offset + 20 }))}>Next page<ArrowRight size={14} /></button>
    </div>
  </nav>;
}

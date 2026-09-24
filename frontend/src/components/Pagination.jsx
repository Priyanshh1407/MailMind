import { ArrowLeft, ArrowRight } from 'lucide-react';

export function Pagination({ page, query, setQuery, loading }) {
  return <nav className="pagination" aria-label="Email pages">
    <div><p>{page.total === 0 ? 'No matching saved emails' : (page.offset + 1) + '–' + Math.min(page.offset + page.emails.length, page.total) + ' of ' + page.total + ' matching saved emails'}</p><small>Results update automatically · 20 messages per page</small></div>
    <div className="actions">
      <button className="button secondary compact" disabled={loading || query.offset === 0} onClick={() => setQuery(previous => ({ ...previous, offset: Math.max(0, previous.offset - 20) }))}><ArrowLeft size={14} />Previous page</button>
      <button className="button secondary compact" disabled={loading || !page.has_more} onClick={() => setQuery(previous => ({ ...previous, offset: previous.offset + 20 }))}>Next page<ArrowRight size={14} /></button>
    </div>
  </nav>;
}

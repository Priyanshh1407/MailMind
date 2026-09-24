import { Search, Sparkles, X } from 'lucide-react';
import { CATEGORIES, categoryName } from '../dashboard';
import { Surface } from './ui/Surface';

export function SearchFilters({ search, setSearch, query, setQuery, loading, page }) {
  const submit = event => {
    event.preventDefault();
    setQuery(previous => ({ ...previous, offset: 0, search: search.trim() }));
  };
  const clear = () => {
    setSearch('');
    setQuery(previous => ({ ...previous, offset: 0, search: '' }));
  };
  const hint = search.trim() !== query.search ? 'Queries of at least three characters can use meaning-based search'
    : loading && query.search ? 'Searching saved mail...'
      : query.search.length > 0 && query.search.length < 3 ? 'Text match active. Type 3 or more characters for meaning-based matches.'
        : page?.search_mode === 'hybrid' ? 'Showing text and meaning-based matches.'
          : query.search ? page?.semantic_index?.pending ? 'Text results are ready. Semantic indexing continues in the background (' + page.semantic_index.pending + ' remaining).' : 'Showing text matches. Semantic search will join automatically when relevant matches exist.'
            : 'Results update automatically after a short pause.';
  const index = page?.semantic_index;
  const total = index ? index.pending + index.indexed + index.failed : 0;
  const percent = total ? Math.round(index.indexed / total * 100) : 0;

  return <Surface as="form" onSubmit={submit} className="filters" aria-label="Search saved emails">
    <div className="search-form">
      <label htmlFor="email-search">Search sender, subject, body, or meaning</label>
      <span className="search-input-wrap">
        <Search size={16} />
        <input id="email-search" type="search" maxLength={200} value={search} placeholder="Try: flight problems or approval needed" autoComplete="off" onChange={event => setSearch(event.target.value)} />
        {search && <button type="button" className="clear-icon" aria-label="Clear search" onClick={clear}><X size={15} /></button>}
      </span>
    </div>
    <div className="filter-actions">
      <label className="category-filter" htmlFor="category-filter">Category
        <select id="category-filter" value={query.category} onChange={event => setQuery(previous => ({ ...previous, offset: 0, category: event.target.value }))}>
          <option value="">All categories</option>
          {CATEGORIES.map(item => <option key={item} value={item}>{categoryName(item)}</option>)}
        </select>
      </label>
      <button type="submit" className="button primary search-submit" disabled={loading || search.trim() === query.search}><Search size={16} />Search now</button>
    </div>
    <div className="search-footer">
      <p className="search-hint" role="status">{hint}</p>
      <span className="semantic-status"><Sparkles size={13} />{index ? 'Semantic index ' + percent + '% complete' : 'Semantic status loading'}</span>
    </div>
  </Surface>;
}

import { CATEGORIES, categoryName, effectiveCategory } from '../dashboard';
import { EmailCard } from './EmailCard';
const LANE_STYLES = {
  IMPORTANT: { className: 'lane-important', badge: 'bg-primary/10 text-primary border-primary/30', hover: 'group-hover:text-primary' },
  UPDATES: { className: 'lane-updates', badge: 'bg-secondary/10 text-secondary border-secondary/30', hover: 'group-hover:text-secondary' },
  SPAM: { className: 'lane-spam', badge: 'bg-error/10 text-error border-error/30', hover: 'group-hover:text-error' },
  NEEDS_REVIEW: { className: 'lane-review', badge: 'bg-surface-variant text-on-surface border-outline-variant', hover: 'group-hover:text-on-surface' },
};
export function EmailBoard({ page, pending, disabled, mutate, api, generation }) {
  return <div className="board">{CATEGORIES.map(category => {
    const rows = page.emails.filter(email => effectiveCategory(email) === category);
    return <section key={category} className={`lane ${LANE_STYLES[category].className}`} aria-label={`${categoryName(category)} emails`}>
      <h2>{category === 'NEEDS_REVIEW' && rows.some(email => email.processing_state === 'pending') ? 'Processing / Needs Review' : categoryName(category)} <span>{rows.length} on this page</span></h2>
      {rows.length ? rows.map(email => <EmailCard key={`${generation}:${email.id}`} email={email} style={LANE_STYLES[category]} pending={Boolean(pending[email.id])} disabled={disabled} mutate={mutate} api={api} generation={generation} />) : <p className="lane-empty">No matching emails on this page.</p>}
    </section>;
  })}</div>;
}

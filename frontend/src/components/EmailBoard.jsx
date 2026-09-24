import { CATEGORIES, categoryName, effectiveCategory } from '../dashboard';
import { EmailCard } from './EmailCard';

const LANE_STYLES = {
  IMPORTANT: { className: 'lane-important', badge: 'important', dot: 'important', label: 'Important', empty: 'No important mail on this page.' },
  UPDATES: { className: 'lane-updates', badge: 'updates', dot: 'updates', label: 'Updates', empty: 'No updates on this page.' },
  SPAM: { className: 'lane-spam', badge: 'spam', dot: 'spam', label: 'Spam', empty: 'No spam on this page.' },
  NEEDS_REVIEW: { className: 'lane-review', badge: 'review', dot: 'review', label: 'Review', empty: 'Nothing needs review.' },
};

export function EmailBoard({ page, pending, disabled, mutate, api, generation }) {
  return <div id="emails" className="board">{CATEGORIES.map(category => {
    const rows = page.emails.filter(email => effectiveCategory(email) === category);
    const style = LANE_STYLES[category];
    const title = category === 'NEEDS_REVIEW' && rows.some(email => email.processing_state === 'pending') ? 'Processing / Needs Review' : style.label;
    return <section key={category} className={'lane ' + style.className} aria-label={categoryName(category) + ' emails'}>
      <div className="lane-heading">
        <h2><span className={'lane-dot ' + style.dot} />{title}</h2>
        <span>{rows.length} on this page</span>
      </div>
      <div className="lane-cards">
        {rows.map(email => <EmailCard key={generation + ':' + email.id} email={email} style={style} pending={Boolean(pending[email.id])} disabled={disabled} mutate={mutate} api={api} generation={generation} />)}
        {!rows.length && <p className="lane-empty">{style.empty}</p>}
      </div>
    </section>;
  })}</div>;
}

import { CATEGORIES, categoryName, effectiveCategory } from '../dashboard';
import { LayoutGroup, m } from 'motion/react';
import { EmailCard } from './EmailCard';
import { enterTransition, fadeUp, spring } from '../motion';

const LANE_STYLES = {
  IMPORTANT: { className: 'lane-important', badge: 'important', dot: 'important', label: 'Important', empty: 'No important mail on this page.' },
  UPDATES: { className: 'lane-updates', badge: 'updates', dot: 'updates', label: 'Updates', empty: 'No updates on this page.' },
  SPAM: { className: 'lane-spam', badge: 'spam', dot: 'spam', label: 'Spam', empty: 'No spam on this page.' },
  NEEDS_REVIEW: { className: 'lane-review', badge: 'review', dot: 'review', label: 'Review', empty: 'Nothing needs review.' },
};

export function EmailBoard({ page, pending, disabled, mutate, api, generation }) {
  return <LayoutGroup><div id='emails' className='board'>{CATEGORIES.map(category => {
    const rows = page.emails.filter(email => effectiveCategory(email) === category);
    const style = LANE_STYLES[category];
    const title = category === 'NEEDS_REVIEW' && rows.some(email => email.processing_state === 'pending') ? 'Processing / Needs Review' : style.label;
    return <section key={category} className={'lane ' + style.className} aria-label={categoryName(category) + ' emails'}>
      <div className='lane-heading'>
        <h2><span className={'lane-dot ' + style.dot} />{title}</h2>
        <span>{rows.length} on this page</span>
      </div>
      <div className='lane-cards'>
        {/* The wrapper owns the motion so the card keeps its own CSS hover lift.
            A shared layoutId lets a relabelled email glide to its new lane. */}
        {rows.map((email, index) => <m.div key={generation + ':' + email.id} layoutId={'email-' + generation + ':' + email.id} layout='position' className='motion-item' initial={fadeUp.initial} animate={fadeUp.animate} transition={{ ...enterTransition(index), layout: spring }}>
          <EmailCard email={email} style={style} pending={Boolean(pending[email.id])} disabled={disabled} mutate={mutate} api={api} generation={generation} />
        </m.div>)}
        {!rows.length && <p className='lane-empty'>{style.empty}</p>}
      </div>
    </section>;
  })}</div></LayoutGroup>;
}

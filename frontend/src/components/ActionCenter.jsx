import { ClipboardCheck } from 'lucide-react';
import { ACTION_FILTERS, actionFilterQuery, actionTypeLabel } from '../intelligence';
import { ActionCard } from './ActionCard';
import { Surface, SectionTitle } from './ui/Surface';

export function ActionCenter({
  snapshot, query, setQuery, loading, disabled, pending, mutate, openSource,
}) {
  const section = snapshot.actions;
  const actions = section.page.actions;
  const active = query.actionFilter || 'open';
  const changeFilter = key => setQuery(previous => ({
    ...previous, actionFilter: key, ...actionFilterQuery(key),
  }));
  // Offer only the types this account actually has (from the summary counts).
  const typeCounts = section.summary?.type_counts || {};
  const activeType = query.actionType || '';
  const typeOptions = Object.entries(typeCounts);
  if (activeType && !(activeType in typeCounts)) typeOptions.push([activeType, 0]);
  const totalActions = Object.values(typeCounts).reduce((sum, count) => sum + count, 0);
  const changeType = value => setQuery(previous => ({ ...previous, actionType: value, actionOffset: 0 }));
  const previousDisabled = loading || query.actionOffset === 0;
  const nextDisabled = loading || actions.length < section.page.limit;
  const emptyMessage = active === 'completed'
    ? 'No completed actions yet.'
    : active === 'snoozed'
      ? 'No snoozed actions. Snooze an open item when it can wait.'
      : active === 'overdue'
        ? 'Nothing is overdue.'
        : active === 'due_soon'
          ? 'Nothing is due in the next seven days.'
          : 'No open actions were extracted from your saved email.';
  const shownEmptyMessage = activeType
    ? 'No ' + actionTypeLabel(activeType).toLowerCase() + ' actions match this view.'
    : emptyMessage;

  return <Surface className='action-center'>
    <SectionTitle icon={<ClipboardCheck size={18} />} eyebrow='Follow-through' title='Action Center' aside={<span className='section-state'>{section.pageState}</span>} />
    <div className='action-toolbar'>
      <div className='action-filter-bar' role='group' aria-label='Filter actions'>
        {ACTION_FILTERS.map(filter => <button key={filter.key} type='button' className={'filter-chip ' + (active === filter.key ? 'active' : '')} aria-pressed={active === filter.key} onClick={() => changeFilter(filter.key)}>{filter.label}</button>)}
      </div>
      {(typeOptions.length > 0 || activeType) && <label className='action-type-filter'>
        <span>Type</span>
        <select value={activeType} onChange={event => changeType(event.target.value)} aria-label='Filter actions by type'>
          <option value=''>All types · {totalActions}</option>
          {typeOptions.map(([type, count]) => <option key={type} value={type}>{actionTypeLabel(type)} · {count}</option>)}
        </select>
      </label>}
    </div>
    {section.pageState === 'error' && <div className='section-error' role='alert'><strong>Actions could not be loaded.</strong><p>{section.pageError.message}</p></div>}
    {section.pageState === 'disabled' && <p className='empty-state'>Action extraction is disabled in this installation.</p>}
    {section.pageState === 'unavailable' && <p className='empty-state'>Connect Google before MailMind can show account actions.</p>}
    {section.pageState === 'ready' && <div className='action-list' aria-busy={loading}>
      {actions.map(action => <ActionCard key={action.action_id} action={action} busy={Boolean(pending['action:' + action.action_id])} disabled={disabled} mutate={mutate} openSource={openSource} />)}
      {!actions.length && <p className='empty-state'>{shownEmptyMessage}</p>}
    </div>}
    {section.pageState === 'ready' && actions.length > 0 && <nav className='action-pagination' aria-label='Action pages'>
      <button className='button secondary compact' disabled={previousDisabled} onClick={() => setQuery(previous => ({ ...previous, actionOffset: Math.max(0, previous.actionOffset - section.page.limit) }))}>Previous</button>
      <span>Starting at {query.actionOffset + 1}</span>
      <button className='button secondary compact' disabled={nextDisabled} onClick={() => setQuery(previous => ({ ...previous, actionOffset: previous.actionOffset + section.page.limit }))}>Next</button>
    </nav>}
  </Surface>;
}

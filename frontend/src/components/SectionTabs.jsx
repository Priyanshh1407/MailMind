import { m } from 'motion/react';
import { spring } from '../motion';

// Inbox / Action Center / Usage, shown in the top navbar. A real ARIA tablist:
// arrow keys, Home and End move between sections; the panels live in App.
export function SectionTabs({ tabs, activeTab, onSelect, onKeyDown, tabRefs }) {
  return <div className='dashboard-tabs' role='tablist' aria-label='Dashboard sections' onKeyDown={onKeyDown}>
    {tabs.map((tab, index) => <button key={tab.key} ref={node => { tabRefs.current[index] = node; }}
      id={'tab-' + tab.key} role='tab' aria-selected={activeTab === tab.key} aria-controls={'panel-' + tab.key}
      tabIndex={activeTab === tab.key ? 0 : -1} onClick={() => onSelect(tab.key)}>
      {activeTab === tab.key && <m.span layoutId='active-tab' className='tab-indicator' transition={spring} aria-hidden='true' />}
      <span className='tab-label'>{tab.label}</span>
    </button>)}
  </div>;
}

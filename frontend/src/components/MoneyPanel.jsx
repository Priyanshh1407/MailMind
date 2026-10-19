import { lazy, Suspense, useState } from 'react';
import { CalendarClock, ChevronLeft, ChevronRight, IndianRupee, Pencil, Receipt, Repeat, ShieldCheck, Undo2 } from 'lucide-react';
import { Surface, SectionTitle } from './ui/Surface';
import { SourceEmailDialog } from './SourceEmailDialog';
import { daysUntil, formatMoney, KIND_LABELS, monthLabel, parseMoney, PERIOD_LABELS, shiftMonth, SOURCE_LABELS } from '../money';
import { formatEmailTime } from '../dashboard';

// Recharts is shared with the Usage tab and loads on demand.
const MoneyChart = lazy(() => import('./MoneyChart').then(module => ({ default: module.MoneyChart })));

function Metric({ icon: Icon, label, value, note }) {
  return <div className='money-metric'><Icon size={16} aria-hidden='true' /><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</div>;
}

function EntryRow({ entry, disabled, mutate, onSource }) {
  const [editing, setEditing] = useState(false);
  const [amount, setAmount] = useState((entry.amount_minor / 100).toFixed(2));
  const [merchant, setMerchant] = useState(entry.merchant);
  const [kind, setKind] = useState(entry.kind);
  const minor = parseMoney(amount);
  const save = async event => {
    event.preventDefault();
    if (minor == null || !merchant.trim()) return;
    const changes = { expected_revision: entry.revision };
    if (minor !== entry.amount_minor) changes.amount_minor = minor;
    if (merchant.trim() !== entry.merchant) changes.merchant = merchant.trim();
    if (kind !== entry.kind) changes.kind = kind;
    if (Object.keys(changes).length === 1) { setEditing(false); return; }
    if (await mutate('ledger-' + entry.entry_id, '/ledger/entries/' + entry.entry_id, { method: 'PATCH', body: changes })) setEditing(false);
  };
  return <li className={'money-entry ' + entry.kind}>
    <div className='money-entry-main'>
      <div>
        <strong>{entry.merchant}</strong>
        <span className='money-entry-meta'>{KIND_LABELS[entry.kind]} · {formatEmailTime(entry.occurred_at)}{entry.user_corrected ? ' · corrected by you' : ''}</span>
      </div>
      <span className='money-amount'>{entry.kind === 'refund' ? '+' : ''}{formatMoney(entry.amount_minor, entry.currency)}</span>
    </div>
    {entry.evidence && <q className='signal-evidence'>{entry.evidence}</q>}
    <div className='actions money-entry-actions'>
      <button className='button ghost compact' onClick={() => onSource(entry)}><Receipt size={13} />Open source</button>
      <button className='button ghost compact' disabled={disabled} aria-expanded={editing} onClick={() => setEditing(value => !value)}><Pencil size={13} />{editing ? 'Cancel' : 'Correct'}</button>
      <span className='money-source' title='How this amount was found'>{SOURCE_LABELS[entry.source]}</span>
    </div>
    {editing && <form className='money-edit' onSubmit={save} onKeyDown={event => { if (event.key === 'Escape') setEditing(false); }}>
      <label>Amount ({entry.currency})<input inputMode='decimal' value={amount} onChange={event => setAmount(event.target.value)} aria-invalid={minor == null} /></label>
      <label>Merchant<input value={merchant} maxLength={80} onChange={event => setMerchant(event.target.value)} /></label>
      <label>Kind<select value={kind} onChange={event => setKind(event.target.value)}>
        {Object.entries(KIND_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <button type='submit' className='button primary compact' disabled={disabled || minor == null || !merchant.trim()}>Save correction</button>
    </form>}
  </li>;
}

export function MoneyPanel({ snapshot, query, setQuery, disabled, mutate, api, openSource }) {
  const ledger = snapshot.ledger;
  const [source, setSource] = useState(null);
  if (!ledger || ledger.state === 'disabled') return <Surface className='money-panel'><p className='empty-state'>The Money tab is switched off. Set MAILMIND_LEDGER_ENABLED=true and restart MailMind.</p></Surface>;
  if (ledger.state === 'unavailable') return <Surface className='money-panel'><p className='empty-state'>Connect Google to see this account&apos;s spending.</p></Surface>;
  if (ledger.state === 'error' || !ledger.summary) return <Surface className='money-panel'><div className='section-error' role='alert'><strong>Spending could not be loaded.</strong><p>{ledger.error?.message}</p></div></Surface>;
  const summary = ledger.summary;
  const currency = summary.currency;
  const totals = summary.totals[currency] || { spent_minor: 0, refunds_minor: 0, bills_minor: 0, net_minor: 0, count: 0 };
  const subscriptions = ledger.subscriptions?.subscriptions || [];
  const insights = ledger.subscriptions?.insights || [];
  const monthly = subscriptions.filter(item => item.currency === currency).reduce((sum, item) => sum + item.monthly_minor, 0);
  const next = subscriptions.find(item => daysUntil(item.renews_on) >= 0);
  const entries = ledger.entries?.entries || [];
  const month = summary.month;
  const goMonth = delta => setQuery(previous => ({ ...previous, ledgerMonth: shiftMonth(month, delta), ledgerOffset: 0 }));
  const others = Object.entries(summary.totals).filter(([key]) => key !== currency);
  return <Surface className='money-panel'>
    <SectionTitle icon={<IndianRupee size={18} />} eyebrow='From your receipts' title='Money' aside={<span className='estimated-badge'><ShieldCheck size={12} aria-hidden='true' /> Amounts never leave this computer</span>} />
    <div className='window-selector money-month' role='group' aria-label='Month'>
      <button className='filter-chip' aria-label='Previous month' onClick={() => goMonth(-1)}><ChevronLeft size={14} /></button>
      <strong>{monthLabel(month)}</strong>
      <button className='filter-chip' aria-label='Next month' onClick={() => goMonth(1)}><ChevronRight size={14} /></button>
    </div>
    <div className='money-metrics'>
      <Metric icon={IndianRupee} label='Spent' value={formatMoney(totals.spent_minor, currency)} note={totals.refunds_minor ? formatMoney(totals.refunds_minor, currency) + ' refunded' : null} />
      <Metric icon={Receipt} label='Bills due' value={formatMoney(totals.bills_minor, currency)} />
      <Metric icon={Repeat} label='Subscriptions' value={formatMoney(monthly, currency) + ' / month'} note={subscriptions.length + ' active'} />
      <Metric icon={CalendarClock} label='Next renewal' value={next ? next.merchant : 'None found'} note={next ? formatMoney(next.amount_minor, next.currency) + ' in ' + daysUntil(next.renews_on) + ' days' : null} />
    </div>
    {others.length > 0 && <p className='usage-note'>Also this month: {others.map(([key, value]) => formatMoney(value.spent_minor, key) + ' spent').join(', ')}.</p>}
    <Suspense fallback={<p className='usage-note' role='status'>Loading chart...</p>}><MoneyChart trend={summary.trend} currency={currency} /></Suspense>
    <div className='breakdown-grid'>
      <section className='usage-breakdown'>
        <h3>Top merchants this month</h3>
        {summary.merchants.length ? <ul className='money-merchants'>{summary.merchants.map(row => <li key={row.merchant + row.currency}>
          <span>{row.merchant}</span><strong>{formatMoney(row.spent_minor, row.currency)}</strong><small>{row.count} payment{row.count === 1 ? '' : 's'}</small>
        </li>)}</ul> : <p className='breakdown-empty'>No spending recorded this month.</p>}
      </section>
      <section className='usage-breakdown'>
        <h3>Subscriptions</h3>
        {insights.map(item => <p key={item.category} className='money-insight'><Repeat size={13} aria-hidden='true' />You pay for {item.merchants.length} {item.category} services ({item.merchants.join(', ')}): {formatMoney(item.monthly_minor, item.currency)} a month.</p>)}
        {subscriptions.length ? <ul className='money-subscriptions'>{subscriptions.map(item => <li key={item.merchant + item.currency}>
          <div><strong>{item.merchant}</strong><small>{formatMoney(item.amount_minor, item.currency)} / {PERIOD_LABELS[item.period]} · {item.basis}</small></div>
          <span className={daysUntil(item.renews_on) <= 7 ? 'renews soon' : 'renews'}>Renews {new Date(item.renews_on + 'T12:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}</span>
        </li>)}</ul> : <p className='breakdown-empty'>No recurring charges found yet.</p>}
      </section>
    </div>
    <section className='money-entries' aria-label='Transactions'>
      <h3>Transactions in {monthLabel(month)} <small>({ledger.entries?.total ?? 0})</small></h3>
      {entries.length ? <ul>{entries.map(entry => <EntryRow key={entry.entry_id + ':' + entry.revision} entry={entry} disabled={disabled} mutate={mutate} onSource={setSource} />)}</ul>
        : <p className='breakdown-empty'>No transactions found in this month&apos;s saved mail.</p>}
      {ledger.entries && ledger.entries.total > ledger.entries.limit && <div className='actions'>
        <button className='button ghost compact' disabled={!query.ledgerOffset} onClick={() => setQuery(previous => ({ ...previous, ledgerOffset: Math.max(0, previous.ledgerOffset - 50) }))}><Undo2 size={13} />Newer</button>
        <button className='button ghost compact' disabled={ledger.entries.offset + ledger.entries.limit >= ledger.entries.total} onClick={() => setQuery(previous => ({ ...previous, ledgerOffset: previous.ledgerOffset + 50 }))}>Older</button>
      </div>}
    </section>
    <p className='usage-note'>Scanned {summary.scan.done + summary.scan.not_transaction} saved emails{summary.scan.waiting ? `, ${summary.scan.waiting} waiting` : ''}. Each amount is found on this computer and hidden from AI providers as a numbered placeholder; the provider only says which placeholder is the total.</p>
    {source && <SourceEmailDialog api={api} emailId={source.email_id} evidence={source.evidence} generation={snapshot.session.generation}
      onClose={() => setSource(null)} onOpenInInbox={() => { setSource(null); openSource(source.email_id); }} />}
  </Surface>;
}

// Money tab helpers: amounts arrive in minor units (paise, cents) and are
// formatted here, in the browser; providers never see them.
const formatters = new Map();

export function formatMoney(minor, currency = 'INR') {
  if (!Number.isInteger(minor)) return '-';
  const key = currency;
  if (!formatters.has(key)) {
    formatters.set(key, new Intl.NumberFormat(currency === 'INR' ? 'en-IN' : 'en-US', {
      style: 'currency', currency, maximumFractionDigits: 2, minimumFractionDigits: 0,
    }));
  }
  return formatters.get(key).format(minor / 100);
}

// "1,299.50" -> 129950; anything else -> null.
export function parseMoney(text) {
  const value = String(text ?? '').replace(/[,\s₹$€£]/g, '');
  if (!/^\d{1,9}(?:\.\d{1,2})?$/.test(value)) return null;
  const [whole, fraction = ''] = value.split('.');
  const minor = Number(whole) * 100 + Number((fraction + '00').slice(0, 2));
  return minor > 0 ? minor : null;
}

export function shiftMonth(month, delta) {
  const [year, number] = month.split('-').map(Number);
  const index = year * 12 + number - 1 + delta;
  return `${String(Math.floor(index / 12)).padStart(4, '0')}-${String(index % 12 + 1).padStart(2, '0')}`;
}

export function monthLabel(month, style = 'long') {
  const [year, number] = month.split('-').map(Number);
  return new Date(Date.UTC(year, number - 1, 15)).toLocaleDateString(undefined, { month: style, year: 'numeric', timeZone: 'UTC' });
}

export const KIND_LABELS = { purchase: 'Purchase', subscription: 'Subscription', refund: 'Refund', bill: 'Bill due', debit: 'Debit' };
export const PERIOD_LABELS = { weekly: 'week', monthly: 'month', quarterly: 'quarter', yearly: 'year' };
export const SOURCE_LABELS = { local: 'Local rules', gemini: 'Gemini (amount filled in locally)', groq: 'Groq (amount filled in locally)' };

export function daysUntil(day, today = new Date()) {
  const target = Date.UTC(...day.split('-').map((part, index) => Number(part) - (index === 1 ? 1 : 0)));
  const base = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.round((target - base) / 86400000);
}

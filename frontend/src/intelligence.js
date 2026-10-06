import { formatTenthsAsSeconds, millisecondsToTenthsUp } from './dashboard.js';

export const ACTION_FILTERS = [
  { key: 'open', label: 'Open' },
  { key: 'due_soon', label: 'Due soon' },
  { key: 'overdue', label: 'Overdue' },
  { key: 'snoozed', label: 'Snoozed' },
  { key: 'completed', label: 'Completed' },
];

const DAY_MS = 86400000;

export function actionFilterQuery(key, now = new Date()) {
  if (!ACTION_FILTERS.some(item => item.key === key)) {
    throw new TypeError('Unsupported action filter.');
  }
  if (!(now instanceof Date) || !Number.isFinite(now.getTime())) {
    throw new TypeError('Action filter clock must be a valid Date.');
  }
  const base = { actionOffset: 0, actionDueFrom: '', actionDueTo: '' };
  if (key === 'snoozed' || key === 'completed') {
    return { ...base, actionStatus: key };
  }
  if (key === 'open') return { ...base, actionStatus: 'open' };
  const stamp = now.getTime();
  return {
    ...base,
    actionStatus: 'open',
    actionDueFrom: new Date(
      key === 'overdue' ? stamp - 366 * DAY_MS : stamp,
    ).toISOString(),
    actionDueTo: new Date(
      key === 'overdue' ? stamp : stamp + 7 * DAY_MS,
    ).toISOString(),
  };
}

export function actionTypeLabel(value) {
  return ({
    reply_required: 'Reply',
    approval_required: 'Approval',
    payment_required: 'Payment',
    document_required: 'Document',
    meeting: 'Meeting',
    review_required: 'Review',
    follow_up_required: 'Follow up',
    general_task: 'Task',
  })[value] || 'Action';
}

// The AI grades its own certainty and says "high" for almost every task, so a
// row on every card says nothing. Flag only the rare tasks it was unsure about.
// (Low ones are never saved; only high ones get automatic reminders.)
export function needsDoubleCheck(action) {
  return action?.confidence !== 'high';
}

export function signalLabel(value) {
  if (value === 'feedback_precedent') return 'Your earlier correction';
  return value.split('_').map(word => (
    word === 'no' ? 'No' : word.charAt(0).toUpperCase() + word.slice(1)
  )).join(' ');
}

export function sourceLabel(value) {
  return ({
    gemini: 'Gemini',
    groq: 'Groq',
    local_heuristic: 'Local model',
    system: 'MailMind system',
    local: 'Local model',
    embedding: 'Local search index',
  })[value] || value;
}

export function operationLabel(value) {
  return value.split('_').map(
    word => word.charAt(0).toUpperCase() + word.slice(1),
  ).join(' ');
}

export function countLabel(value, noun) {
  return Number(value).toLocaleString() + ' ' + noun + (Number(value) === 1 ? '' : 's');
}

// A date-only deadline is stored at the local reminder hour; show only the day
// the email named, never that internal time.
export function formatDeadline(value, precision, { locale, timeZone } = {}) {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return 'No deadline detected';
  const day = { weekday: 'short', day: 'numeric', month: 'short', timeZone };
  if (precision === 'date_only') return date.toLocaleDateString(locale, day).replace(',', '');
  const dayText = date.toLocaleDateString(locale, day).replace(',', '');
  const timeText = date.toLocaleTimeString(locale, { hour: 'numeric', minute: '2-digit', timeZone });
  return dayText + ', ' + timeText;
}

export function hasEstimatedUsage(summary) {
  return summary.count_methods.some(
    item => ['estimated', 'unavailable'].includes(item.key)
      && item.event_count > 0,
  ) || summary.totals.unknown_events > 0;
}

// What each category means: the same definitions the classifier is given.
export const CATEGORY_MEANING = {
  IMPORTANT: 'Direct communication, or something that needs your action or attention.',
  UPDATES: 'Legitimate, useful information that is not urgent.',
  SPAM: 'Unwanted or junk mail.',
};
const CATEGORY_NAME = { IMPORTANT: 'Important', UPDATES: 'Updates', SPAM: 'Spam' };

// The whole decision as short, readable parts for "Why this category?".
// Built only from what was saved with the decision; no extra model call.
export function describeDecision(decision) {
  if (!decision) return [];
  const chosen = CATEGORY_NAME[decision.category];
  const model = sourceLabel(decision.provider) + (decision.model_version ? ' (' + decision.model_version + ')' : '');
  const timing = decision.elapsed_ms == null ? '' : ' in ' + formatTenthsAsSeconds(millisecondsToTenthsUp(decision.elapsed_ms)) + ' s';
  const decided = {
    primary: model + ' chose ' + chosen + timing + '.',
    fallback: model + ' chose ' + chosen + timing + '. The main Gemini model was unavailable, so the backup Gemini model answered.',
    groq: model + ' chose ' + chosen + timing + '. The Gemini models were unavailable, so the backup provider answered.',
    gemini: model + ' chose ' + chosen + timing + '.',
    local: 'The local model' + (decision.model_version ? ' (' + decision.model_version + ')' : '') + ' chose ' + chosen + timing + ', running entirely on this computer.',
  }[decision.route] || model + ' chose ' + chosen + timing + '.';
  const parts = [
    { key: 'decision', title: 'Decision', text: decided },
    { key: 'meaning', title: 'What ' + chosen + ' means', text: CATEGORY_MEANING[decision.category] },
  ];
  const precedents = decision.precedents || {};
  const corrections = {
    used: countLabel(precedents.used, 'similar email') + ' you corrected ' + (precedents.used === 1 ? 'was' : 'were') + ' shown to the model as examples.',
    none_close_enough: 'None of your past corrections were similar enough, so this email was judged on its own.',
    unavailable: 'Your past corrections could not be checked this time, so this email was judged on its own.',
  }[precedents.lookup];
  if (corrections) parts.push({ key: 'precedents', title: 'Your past corrections', text: corrections });
  const second = decision.second_opinion;
  if (second) parts.push({ key: 'second', title: 'Second opinion', text: second.agrees
    ? 'The local model also chose ' + chosen + '.'
    : `The local model would have chosen ${CATEGORY_NAME[second.category]}; the cloud model's decision is the one used.` });
  if (decision.decided_by === 'you') parts.push({ key: 'yours', title: 'Your label', text: decision.your_label === decision.category
    ? 'You confirmed ' + chosen + '.'
    : 'You changed this to ' + CATEGORY_NAME[decision.your_label] + ', so your label is what the dashboard uses.' });
  return parts;
}

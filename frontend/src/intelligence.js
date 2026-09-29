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

export function signalLabel(value) {
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

export function hasEstimatedUsage(summary) {
  return summary.count_methods.some(
    item => ['estimated', 'unavailable'].includes(item.key)
      && item.event_count > 0,
  ) || summary.totals.unknown_events > 0;
}

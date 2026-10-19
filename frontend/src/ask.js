// "Ask your inbox" helpers: number citations once per source, describe the
// agent's steps from its content-free trace, and explain non-answers.

export function numberCitations(answer) {
  const sources = [];
  const index = new Map();
  const sentences = (answer?.sentences || []).map(sentence => ({
    text: sentence.text,
    refs: [...new Set(sentence.citations.map(citation => {
      const key = citation.email_id + '\u0000' + citation.quote;
      if (!index.has(key)) {
        sources.push(citation);
        index.set(key, sources.length);
      }
      return index.get(key);
    }))],
  }));
  return { sentences, sources };
}

export function describeStep(step) {
  switch (step.action) {
    case 'search_mail': return `Searched${step.query ? ' "' + step.query + '"' : ' recent mail'}: ${step.results} match${step.results === 1 ? '' : 'es'}`;
    case 'read_email': return step.ok ? 'Read an email' : 'Tried to read an email that is not available';
    case 'list_actions': return 'Checked the Action Center';
    case 'ledger_summary': return step.ok ? 'Checked spending (amounts stay on this computer)' : 'Spending is not available';
    case 'risk_report': return 'Checked a scam report';
    case 'final': return 'Wrote and verified the answer';
    case 'stopped': return 'Stopped: ' + (step.reason || 'limit reached');
    default: return step.action;
  }
}

export function questionOutcome(item) {
  if (item.status === 'unanswerable') return "I couldn't find that in your saved mail.";
  if (item.status === 'unverified') return item.answer?.stopped
    ? 'I ran out of steps before finding a verified answer. Try a more specific question.'
    : "I couldn't verify an answer: none of the AI's claims matched a quote in your emails, so nothing is shown.";
  if (item.status === 'failed') return item.error_code === 'cloud_not_configured_or_unavailable'
    ? 'No AI provider is configured, so questions cannot be answered.'
    : 'The question could not be answered right now (' + (item.error_code || 'error') + '). Try again shortly.';
  if (item.status === 'cancelled') return 'Stopped because the account changed.';
  if (item.status === 'answered' && item.answer?.dropped_sentences) return null;
  return null;
}

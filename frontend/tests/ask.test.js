import test from 'node:test';
import assert from 'node:assert/strict';
import { validateResponse } from '../src/api.js';
import { describeStep, numberCitations, questionOutcome } from '../src/ask.js';

const citation = { email_id: 'landlord', quote: 'by 15 October', subject: 'Deposit', date: '2026-10-02' };
const answer = {
  sentences: [{ text: 'The deposit comes back by 15 October.', citations: [citation] },
    { text: 'After the inspection.', citations: [citation, { ...citation, quote: 'after the inspection' }] }],
  unanswerable: false, dropped_sentences: 1, citations_total: 4, citations_verified: 3,
};
const question = {
  account_id: 'synthetic@example.test', generation: 1, question_id: 7, question: 'When is my deposit back?',
  status: 'answered', answer, trace: [{ step: 1, action: 'search_mail', query: 'deposit', results: 2 }, { step: 2, action: 'final' }],
  total_tokens: 900, latency_ms: 4200, error_code: null, created_at: '2026-10-12T10:00:00.000000Z', updated_at: '2026-10-12T10:00:04.000000Z',
};

test('each source is numbered once, in the order it is first cited', () => {
  const { sentences, sources } = numberCitations(answer);
  assert.deepEqual(sentences.map(sentence => sentence.refs), [[1], [1, 2]]);
  assert.equal(sources.length, 2);
  assert.deepEqual(numberCitations(null), { sentences: [], sources: [] });
});

test('steps and non-answers are described in plain language', () => {
  assert.equal(describeStep({ action: 'search_mail', query: 'deposit', results: 1 }), 'Searched "deposit": 1 match');
  assert.equal(describeStep({ action: 'ledger_summary', ok: true }), 'Checked spending (amounts stay on this computer)');
  assert.match(questionOutcome({ status: 'unanswerable' }), /couldn't find/);
  assert.match(questionOutcome({ status: 'unverified', answer: { stopped: false } }), /none of the AI's claims/);
  assert.match(questionOutcome({ status: 'failed', error_code: 'cloud_not_configured_or_unavailable' }), /No AI provider/);
  assert.equal(questionOutcome(question), null);
});

test('question responses are validated, including that every shown sentence has a citation', () => {
  assert.doesNotThrow(() => validateResponse('/questions/7', 'GET', question));
  assert.doesNotThrow(() => validateResponse('/questions', 'GET', { account_id: question.account_id, generation: 1, questions: [question] }));
  assert.doesNotThrow(() => validateResponse('/questions', 'POST', { question_id: 7, status: 'running', message: 'Searching...' }));
  const uncited = { ...question, answer: { ...answer, sentences: [{ text: 'Trust me.', citations: [] }] } };
  assert.throws(() => validateResponse('/questions/7', 'GET', uncited), /Unexpected/);
  assert.throws(() => validateResponse('/questions/7', 'GET', { ...question, status: 'guessed' }), /Unexpected/);
});

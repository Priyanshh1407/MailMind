"""UPG-01: the evaluation harness scores recordings exactly like production."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import evaluate_cloud
from src.config import Settings

ROWS = [
    {'id': 'a', 'group_id': 'g1', 'subject': 'Sign the contract',
     'body': 'Please sign the contract today.', 'human_label': 'IMPORTANT'},
    {'id': 'b', 'group_id': 'g2', 'subject': 'Weekly digest',
     'body': 'Your weekly summary is ready.', 'human_label': 'UPDATES'},
    {'id': 'c', 'group_id': 'g3', 'subject': 'You won',
     'body': 'Claim your prize now.', 'human_label': 'SPAM'},
]


def answer(category, actions=()):
    return json.dumps({'category': category,
                       'explanation': {'summary': 'Observable reason.', 'signals': []},
                       'actions': list(actions)})


def entry(row_id, route, provider, text=None, status='ok'):
    return {'id': row_id, 'route': route, 'provider': provider, 'model': f'{route}-model',
            'status': status, 'text': text, 'error_code': None if status == 'ok'
            else 'provider_transient', 'latency_ms': 100.0, 'input_tokens': 10,
            'output_tokens': 5, 'attempts': 1}


class CloudEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.recordings = Path(self.temp.name)
        patcher = patch.object(evaluate_cloud, 'RECORDINGS', self.recordings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = Settings(gemini_models=('primary', 'fallback'))

    def write(self, route, entries):
        (self.recordings / f'{route}.jsonl').write_text(
            ''.join(json.dumps(item) + '\n' for item in entries), encoding='utf-8')

    def test_invalid_answers_and_errors_count_as_no_answer(self):
        self.write('gemini_primary', [
            entry('a', 'gemini_primary', 'gemini', answer('IMPORTANT')),
            entry('b', 'gemini_primary', 'gemini', 'not json'),
            entry('c', 'gemini_primary', 'gemini', status='error'),
        ])
        results, _models, _actions = evaluate_cloud.replay(self.settings, ROWS)
        primary = results['gemini_primary']
        self.assertAlmostEqual(primary['coverage'], 1 / 3)
        self.assertAlmostEqual(primary['accuracy'], 1 / 3)
        self.assertEqual((primary['errors'], primary['invalid_output']), (1, 1))

    def test_failover_chain_takes_the_first_valid_answer_in_production_order(self):
        self.write('gemini_primary', [
            entry('a', 'gemini_primary', 'gemini', answer('IMPORTANT')),
            entry('b', 'gemini_primary', 'gemini', 'not json'),
            entry('c', 'gemini_primary', 'gemini', status='error'),
        ])
        self.write('gemini_fallback', [
            entry('a', 'gemini_fallback', 'gemini', answer('SPAM')),
            entry('b', 'gemini_fallback', 'gemini', answer('UPDATES')),
            entry('c', 'gemini_fallback', 'gemini', status='error'),
        ])
        self.write('groq', [entry(row['id'], 'groq', 'groq', answer(row['human_label']))
                            for row in ROWS])
        results, _models, _actions = evaluate_cloud.replay(self.settings, ROWS)
        chain = results['failover_chain']
        self.assertEqual(chain['accuracy'], 1.0)
        self.assertEqual(chain['answered_by'],
                         {'gemini_primary': 1, 'gemini_fallback': 1, 'groq': 1})

    def test_actions_are_counted_before_and_after_production_validation(self):
        grounded = {'type': 'approval_required', 'title': 'Sign the contract',
                    'description': 'Sign the contract today.', 'due_at': None,
                    'due_precision': 'unknown', 'evidence': 'sign the contract today',
                    'confidence': 'high'}
        ungrounded = {**grounded, 'evidence': 'approve the budget'}
        self.write('gemini_primary', [
            entry('a', 'gemini_primary', 'gemini', answer('IMPORTANT', [grounded])),
            entry('b', 'gemini_primary', 'gemini', answer('UPDATES', [grounded, ungrounded])),
        ])
        _results, _models, actions = evaluate_cloud.replay(self.settings, ROWS[:1])
        self.assertEqual(actions['gemini_primary'], {'proposed': 3, 'passed_validation': 1})

    def test_one_ungrounded_action_discards_that_answers_whole_action_list(self):
        # Production rule (llm_api._parse_actions): if any proposed action fails
        # validation, none of that answer's actions are trusted. The category is
        # still accepted.
        grounded = {'type': 'approval_required', 'title': 'Sign the contract',
                    'description': 'Sign the contract today.', 'due_at': None,
                    'due_precision': 'unknown', 'evidence': 'sign the contract today',
                    'confidence': 'high'}
        self.write('gemini_primary', [entry('a', 'gemini_primary', 'gemini', answer(
            'IMPORTANT', [grounded, {**grounded, 'evidence': 'approve the budget'}]))])
        results, _models, actions = evaluate_cloud.replay(self.settings, ROWS[:1])
        self.assertEqual(actions['gemini_primary'], {'proposed': 2, 'passed_validation': 0})
        self.assertEqual(results['gemini_primary']['accuracy'], 1.0)

    def test_requests_are_built_with_the_production_payload(self):
        contents, evidence_source = evaluate_cloud.request_text(ROWS[0])
        payload = json.loads(contents)
        # The label (and its annotation) must never reach the provider.
        self.assertEqual(set(payload), {'data_trust', 'email', 'precedents'})
        self.assertEqual(set(payload['email']), {'sender', 'text', 'received_at'})
        self.assertNotIn('human_label', contents)
        self.assertNotIn(ROWS[0]['human_label'], contents)
        self.assertEqual(payload['data_trust'], 'untrusted_email_and_precedents')
        # TZ-01: the same instant, shown in the user's zone.
        from datetime import datetime
        self.assertEqual(datetime.fromisoformat(payload['email']['received_at']),
                         datetime.fromisoformat(evaluate_cloud.RECEIVED_AT))
        self.assertTrue(evidence_source.startswith('[SENDER]\n'))


if __name__ == '__main__':
    unittest.main()

"""UPG-02: provider fault-injection matrix for the classification chain.

Each fault is injected into the real llm_api.classify_email, either on the
primary Gemini model only or on every route (Gemini primary, Gemini fallback,
Groq). EXPECTED is the documented contract; scripts/render_fault_matrix.py
renders the same results into docs/RESILIENCE.md.
"""
import json
import unittest
from unittest.mock import Mock, patch

import requests

from src import llm_api
from src.config import Settings
from src.pipeline import PERMANENT_CLASSIFICATION_FAILURES

EMAIL = ('sender@example.test', 'Contract signature',
         'Please sign the revised contract before 5 pm today.')
GOOD = json.dumps({'category': 'IMPORTANT',
                   'explanation': {'summary': 'The sender asks for a signature.',
                                   'signals': []},
                   'actions': []})


class ProviderError(Exception):
    def __init__(self, status_code):
        super().__init__(f'synthetic provider status {status_code}')
        self.status_code = status_code


def _answer(text):
    return {'text': text}


def _raise(error):
    return {'error': error}


# name -> (description, gemini behaviour, groq behaviour)
FAULTS = {
    'timeout': ('Provider timed out', lambda: _raise(TimeoutError('synthetic')),
                lambda: _raise(requests.exceptions.ReadTimeout('synthetic'))),
    'quota_429': ('HTTP 429 quota/rate limit', lambda: _raise(ProviderError(429)),
                  lambda: _raise(ProviderError(429))),
    'server_500': ('HTTP 500 provider error', lambda: _raise(ProviderError(500)),
                   lambda: _raise(ProviderError(500))),
    'bad_request_400': ('HTTP 400 rejected request/parameter',
                        lambda: _raise(ProviderError(400)), lambda: _raise(ProviderError(400))),
    'not_found_404': ('HTTP 404 retired or unknown model',
                      lambda: _raise(ProviderError(404)), lambda: _raise(ProviderError(404))),
    'auth_401': ('HTTP 401 rejected API key', lambda: _raise(ProviderError(401)),
                 lambda: _raise(ProviderError(401))),
    'malformed_json': ('Answer is not JSON', lambda: _answer('not json'),
                       lambda: _answer('not json')),
    'unknown_category': ('Valid JSON, category outside the enum',
                         lambda: _answer('{"category":"URGENT"}'),
                         lambda: _answer('{"category":"URGENT"}')),
    'ungrounded_action': ('Action evidence not present in the email', lambda: _answer(json.dumps({
        'category': 'IMPORTANT',
        'explanation': {'summary': 'The sender asks for a signature.', 'signals': []},
        'actions': [{'type': 'approval_required', 'title': 'Approve budget',
                     'description': 'Approve the budget.', 'due_at': None,
                     'due_precision': 'unknown', 'evidence': 'approve the budget',
                     'confidence': 'high'}]})), None),
}

# (fault, scope) -> (routes called, outcome, category or failure reason, email result)
EXPECTED = {
    ('timeout', 'primary'): ('primary, fallback', 'CLASSIFIED', 'IMPORTANT', 'classified'),
    ('quota_429', 'primary'): ('primary, fallback', 'CLASSIFIED', 'IMPORTANT', 'classified'),
    ('server_500', 'primary'): ('primary, fallback', 'CLASSIFIED', 'IMPORTANT', 'classified'),
    ('bad_request_400', 'primary'): ('primary, fallback', 'CLASSIFIED', 'IMPORTANT', 'classified'),
    ('not_found_404', 'primary'): ('primary, fallback', 'CLASSIFIED', 'IMPORTANT', 'classified'),
    ('auth_401', 'primary'): ('primary, groq', 'CLASSIFIED', 'IMPORTANT', 'classified'),
    ('malformed_json', 'primary'): ('primary, fallback', 'CLASSIFIED', 'IMPORTANT', 'classified'),
    ('unknown_category', 'primary'): ('primary, fallback', 'CLASSIFIED', 'IMPORTANT', 'classified'),
    ('ungrounded_action', 'primary'): ('primary', 'CLASSIFIED', 'IMPORTANT',
                                       'classified; action dropped'),
    ('timeout', 'all_routes'): ('primary, fallback, groq', 'ERROR', 'provider_timeout',
                                'retried later'),
    ('quota_429', 'all_routes'): ('primary, fallback, groq', 'ERROR', 'provider_quota',
                                  'retried later'),
    ('server_500', 'all_routes'): ('primary, fallback, groq', 'ERROR', 'provider_transient',
                                   'retried later'),
    ('bad_request_400', 'all_routes'): ('primary, fallback, groq', 'ERROR',
                                        'provider_invalid_request', 'Needs Review'),
    ('not_found_404', 'all_routes'): ('primary, fallback, groq', 'ERROR',
                                      'provider_invalid_request', 'Needs Review'),
    ('auth_401', 'all_routes'): ('primary, groq', 'ERROR', 'provider_auth', 'Needs Review'),
    ('malformed_json', 'all_routes'): ('primary, fallback, groq', 'ERROR',
                                       'invalid_provider_output', 'Needs Review'),
    ('unknown_category', 'all_routes'): ('primary, fallback, groq', 'ERROR',
                                         'invalid_provider_output', 'Needs Review'),
    ('ungrounded_action', 'all_routes'): ('primary', 'CLASSIFIED', 'IMPORTANT',
                                          'classified; action dropped'),
}


def run_scenario(fault, scope):
    """Inject one fault and return (routes called, outcome, detail, email result)."""
    _description, gemini_fault, groq_fault = FAULTS[fault]
    groq_fault = groq_fault or (lambda: _answer(GOOD))
    called = []

    def behave(spec):
        if 'error' in spec:
            raise spec['error']
        return spec['text']

    def generate_content(model, contents, config):
        called.append(model)
        faulty = model == 'primary' or scope == 'all_routes'
        response = Mock(usage_metadata=None)
        response.text = behave(gemini_fault() if faulty else _answer(GOOD))
        return response

    def groq_post(*args, **kwargs):
        called.append('groq')
        text = behave(groq_fault() if scope == 'all_routes' else _answer(GOOD))
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {'choices': [{'message': {'content': text}}]}
        return response

    client = Mock()
    client.models.generate_content.side_effect = generate_content
    llm_api._model_cooldowns.clear()
    try:
        with patch.object(llm_api, 'get_client', return_value=client), \
             patch.object(llm_api.vector_db, 'search_similar_emails', return_value=[]), \
             patch.dict(llm_api.os.environ, {'GROQ_API_KEY': 'synthetic'}, clear=False), \
             patch('requests.post', side_effect=groq_post):
            result = llm_api.classify_email(
                *EMAIL, settings=Settings(gemini_models=('primary', 'fallback')))
    finally:
        llm_api._model_cooldowns.clear()
    if result.outcome == 'CLASSIFIED':
        detail = result.category
        email = ('classified; action dropped' if fault == 'ungrounded_action'
                 and not result.analysis.actions else 'classified')
    else:
        detail = result.reason
        email = ('Needs Review' if detail in PERMANENT_CLASSIFICATION_FAILURES
                 else 'retried later')
    return ', '.join(called), result.outcome, detail, email


class ProviderFaultMatrixTests(unittest.TestCase):
    def test_every_cell_has_an_expectation(self):
        self.assertEqual(set(EXPECTED),
                         {(fault, scope) for fault in FAULTS
                          for scope in ('primary', 'all_routes')})

    def test_fault_matrix(self):
        for (fault, scope), expected in EXPECTED.items():
            with self.subTest(fault=fault, scope=scope):
                self.assertEqual(run_scenario(fault, scope), expected)


if __name__ == '__main__':
    unittest.main()

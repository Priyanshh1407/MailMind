"""Audit #1 regressions (2026-09-30): each test reproduces a confirmed defect.

A test marked expectedFailure documents a bug that is still open. The phase
that fixes the bug removes the decorator, so the same test proves the fix.
Only synthetic mail and mocked providers are used.
"""
from contextlib import nullcontext
import json
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.app import authoritative_prediction, create_app
from src import llm_api
from src.action_center import list_actions
from src.config import Settings
from src.database import connection
from src.db_utils import log_email_to_db
from src.notifier import Delivery
from src.pipeline import process_task
from src.prediction import Prediction
from src.privacy import provider_email_text
from src.work_queue import (
    claim_cycle, finish_cycle, newest_due_tasks, reconcile_email_analysis,
)
from tests.test_intelligence_phase4 import (
    A, ORIGIN, SOURCE_TIME, EmptyCollection, Phase4Base,
)


class ProviderError(Exception):
    def __init__(self, status_code):
        super().__init__(f'synthetic provider status {status_code}')
        self.status_code = status_code


def gemini_response(payload):
    response = Mock()
    response.text = payload if isinstance(payload, str) else json.dumps(payload)
    response.usage_metadata = None
    return response


def enriched_output(category, actions=()):
    return {
        'category': category,
        'explanation': {'summary': 'The sender makes a direct request.',
                        'signals': []},
        'actions': list(actions),
    }


def action_output(action_type, evidence):
    return {
        'type': action_type, 'title': 'Handle the request',
        'description': 'Complete the request in the email.',
        'due_at': None, 'due_precision': 'unknown',
        'evidence': evidence, 'confidence': 'high',
    }


class LoadedShadowModel:
    """A ready local shadow model that disagrees with the cloud decision."""
    model_loaded = True
    load_reason = 'ready'
    model_version = 'synthetic-shadow-v1'
    training_scope = 'synthetic_benchmark_only'

    def predict_with_usage(self, subject, body, *, sender='', account_id=None):
        return Prediction(category='SPAM', outcome='CLASSIFIED', source='local',
                          model_version=self.model_version), 7


class ProviderFailoverRegressionTests(unittest.TestCase):
    """LLM-01: a broken primary route must not end the failover chain."""

    def setUp(self):
        self.settings = Settings(gemini_models=('audit-primary', 'audit-fallback'))
        llm_api._model_cooldowns.clear()
        self.addCleanup(llm_api._model_cooldowns.clear)

    def classify(self, primary_failure, fallback=None, groq=None):
        """Run the real chain; each route is a callable that returns or raises."""
        called = []
        routes = {
            'audit-primary': primary_failure,
            'audit-fallback': fallback or (
                lambda: gemini_response(enriched_output('UPDATES'))),
        }

        def generate_content(model, contents, config):
            called.append(model)
            return routes[model]()

        def groq_post(*args, **kwargs):
            called.append('groq')
            return groq()

        client = Mock()
        client.models.generate_content.side_effect = generate_content
        with patch.object(llm_api, 'get_client', return_value=client), \
             patch.object(llm_api.vector_db, 'search_similar_emails', return_value=[]), \
             patch.dict(llm_api.os.environ,
                        {'GROQ_API_KEY': 'synthetic' if groq else ''}, clear=False), \
             patch('requests.post', side_effect=groq_post):
            result = llm_api.classify_email(
                'sender@example.test', 'Weekly digest', 'Your weekly summary.',
                settings=self.settings)
        return result, called

    @staticmethod
    def raises(status):
        def route():
            raise ProviderError(status)
        return route

    @staticmethod
    def groq_answer(text):
        def route():
            response = Mock(status_code=200)
            response.raise_for_status.return_value = None
            response.json.return_value = {'choices': [{'message': {'content': text}}]}
            return response
        return route

    def test_retired_primary_model_fails_over_to_next_gemini_model(self):
        result, called = self.classify(self.raises(404))
        self.assertEqual(called, ['audit-primary', 'audit-fallback'])
        self.assertEqual((result.outcome, result.category, result.model_version),
                         ('CLASSIFIED', 'UPDATES', 'audit-fallback'))

    def test_invalid_primary_output_fails_over_to_next_gemini_model(self):
        result, called = self.classify(lambda: gemini_response('not json'))
        self.assertEqual(called, ['audit-primary', 'audit-fallback'])
        self.assertEqual((result.outcome, result.category), ('CLASSIFIED', 'UPDATES'))

    def test_broken_model_is_cooled_down_but_bad_answer_is_not(self):
        self.classify(self.raises(404))
        self.assertFalse(llm_api._model_is_available('audit-primary'))
        llm_api._model_cooldowns.clear()
        self.classify(lambda: gemini_response('not json'))
        self.assertTrue(llm_api._model_is_available('audit-primary'))

    def test_rejected_gemini_key_skips_other_gemini_models_and_uses_groq(self):
        result, called = self.classify(
            self.raises(401),
            groq=self.groq_answer(json.dumps(enriched_output('IMPORTANT'))))
        self.assertEqual(called, ['audit-primary', 'groq'])
        self.assertEqual((result.outcome, result.category, result.source),
                         ('CLASSIFIED', 'IMPORTANT', 'groq'))

    def test_every_route_rejecting_the_request_is_a_permanent_failure(self):
        result, called = self.classify(
            self.raises(400), fallback=self.raises(404), groq=self.raises(400))
        self.assertEqual(called, ['audit-primary', 'audit-fallback', 'groq'])
        self.assertEqual((result.outcome, result.reason),
                         ('ERROR', 'provider_invalid_request'))

    def test_temporary_failure_on_any_route_keeps_the_email_retryable(self):
        result, called = self.classify(
            self.raises(429), fallback=self.raises(404),
            groq=self.groq_answer('not json'))
        self.assertEqual(called, ['audit-primary', 'audit-fallback', 'groq'])
        self.assertEqual((result.outcome, result.reason), ('ERROR', 'provider_quota'))


class ReanalyzeAuthorityRegressionTests(Phase4Base):
    """BUG-01: Re-analyze must use the authoritative cloud route in normal mode."""

    def setUp(self):
        super().setUp()
        self.app = create_app(
            settings=self.settings,
            model_factory=Mock(return_value=LoadedShadowModel()),
            vector_factory=Mock(return_value=EmptyCollection()))
        self.client = TestClient(self.app, base_url='http://localhost')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.headers['Origin'] = ORIGIN
        self.client.headers['X-CSRF-Token'] = self.client.post('/session').json()['csrf_token']
        context, _ = self.app.state.accounts.session(
            self.client.cookies.get('mailmind_session'))
        self.app.state.accounts.finish_auth(
            self.app.state.accounts.begin_auth(context), (A, '{}'))
        self.client.headers['X-CSRF-Token'] = self.client.get('/session').json()['csrf_token']
        self.body = 'Please approve the launch checklist. Reply before the deadline.'
        with connection(self.settings.db_path) as conn:
            log_email_to_db(
                'mail-1', 'Sender <sender@example.test>', 'Approval needed', self.body,
                Prediction(category='IMPORTANT', outcome='CLASSIFIED', source='gemini',
                           model_version='synthetic-gemini'),
                Prediction(category='SPAM', outcome='CLASSIFIED', source='local'),
                account_id=A, db_conn=conn)

    def test_local_only_mode_decides_with_the_local_model(self):
        cloud = Mock(side_effect=AssertionError('cloud must not run in local-only mode'))
        result = authoritative_prediction(
            LoadedShadowModel(), 'Approval needed', self.body, A,
            Settings(local_only=True), Mock(), Mock(), classifier=cloud)
        self.assertEqual((result.source, result.category), ('local', 'SPAM'))
        cloud.assert_not_called()

    def test_reanalyze_keeps_cloud_authority_and_extracts_actions(self):
        client = Mock()
        client.models.generate_content.return_value = gemini_response(enriched_output(
            'IMPORTANT',
            [action_output('approval_required', 'approve the launch checklist')]))
        with patch.object(llm_api, 'get_client', return_value=client), \
             patch.object(llm_api.vector_db, 'search_similar_emails', return_value=[]):
            response = self.client.post('/emails/mail-1/reanalyze', json={})
        self.assertEqual(response.status_code, 200)
        email = self.client.get('/emails').json()['emails'][0]
        self.assertEqual(email['latest_prediction']['source'], 'gemini')
        self.assertEqual(email['effective_category'], 'IMPORTANT')
        self.assertEqual(response.json()['actions']['created_count'], 1)


class RedactedEvidenceRegressionTests(Phase4Base):
    """BUG-02: actions grounded in the redacted provider text must persist."""

    def setUp(self):
        super().setUp()
        self.seed_email(subject='Invoice due',
                        body='Please pay Rs 5,000 for the venue by Friday.')
        self.manager, _token = self.connected_manager()
        self.context = self.manager.worker_context()
        with connection(self.settings.db_path) as conn:
            conn.execute(
                """INSERT INTO processing_tasks(
                   account_id,email_id,status,stage,created_at,updated_at)
                   VALUES (?,?,'queued','classify',?,?)""",
                (A, 'mail-1', SOURCE_TIME, SOURCE_TIME))

    def run_worker_task(self, evidence, *, persist_failure=None):
        client = Mock()
        client.models.generate_content.return_value = gemini_response(enriched_output(
            'IMPORTANT', [action_output('payment_required', evidence)]))
        shadow = Mock(return_value=(Prediction(
            category='IMPORTANT', outcome='CLASSIFIED', source='local',
            model_version='synthetic-local'), 5))
        token, job = claim_cycle(self.manager, self.context)
        self.addCleanup(finish_cycle, self.manager, self.context, token, job)
        task = newest_due_tasks(self.manager, self.context, token, 1)[0]
        failure = (patch('src.pipeline.persist_analysis_actions', side_effect=persist_failure)
                   if persist_failure else nullcontext())
        with patch.object(llm_api, 'get_client', return_value=client), \
             patch.object(llm_api.vector_db, 'search_similar_emails', return_value=[]), \
             failure:
            completed = process_task(
                task, self.manager, self.context, token, Mock(), Mock(), Mock(),
                classifier=llm_api.classify_email, shadow=shadow,
                notifier=Mock(return_value=Delivery('sent', message_id='1')),
                marker=Mock(), logger=log_email_to_db, validator=Mock(), job_id=job)
        self.assertTrue(completed)
        return token

    def action_types(self):
        return [action['action_type']
                for action in list_actions(A, db_path=self.settings.db_path)]

    def test_payment_action_quoting_a_redacted_amount_is_persisted(self):
        self.run_worker_task('pay [AMOUNT] for the venue')
        self.assertEqual(self.action_types(), ['payment_required'])

    def test_deferred_action_retry_uses_the_same_provider_text(self):
        token = self.run_worker_task(
            'pay [AMOUNT] for the venue',
            persist_failure=RuntimeError('synthetic derived failure'))
        self.assertEqual(self.action_types(), [])
        self.assertEqual(reconcile_email_analysis(
            self.manager, self.context, token, limit=5), 1)
        self.assertEqual(self.action_types(), ['payment_required'])

    def test_evidence_absent_from_the_email_is_still_rejected(self):
        self.run_worker_task('pay [AMOUNT] for the catering')
        self.assertEqual(self.action_types(), [])

    def test_provider_payload_and_grounding_share_one_text(self):
        payload, _ = llm_api.build_classification_payload(
            'Sender <sender@example.test>', 'Invoice due',
            'Please pay Rs 5,000 via https://pay.example.test by Friday.', [])
        self.assertEqual(json.loads(payload)['email']['text'],
                         provider_email_text(
                             'Invoice due',
                             'Please pay Rs 5,000 via https://pay.example.test by Friday.'))


class LocalOnlyDefaultModelTests(unittest.TestCase):
    """ML-02: local-only mode must default to a three-category checkpoint."""

    def test_default_local_model_is_the_three_category_checkpoint(self):
        self.assertEqual(Settings().model_path, Settings().shadow_model_path)
        with patch.dict(llm_api.os.environ, {}, clear=True):
            configured = Settings.from_environment()
        self.assertEqual(configured.model_path.name, 'inbox-approved-v2')

    def test_explicit_model_path_still_overrides_the_default(self):
        with patch.dict(llm_api.os.environ,
                        {'MAILMIND_MODEL_PATH': 'custom-checkpoint'}, clear=True):
            configured = Settings.from_environment()
        self.assertEqual(configured.model_path.name, 'custom-checkpoint')


if __name__ == '__main__':
    unittest.main()

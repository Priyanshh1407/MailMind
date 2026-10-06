"""Audit #1 regressions (2026-09-30): each test reproduces a confirmed defect.

A test marked expectedFailure documents a bug that is still open. The phase
that fixes the bug removes the decorator, so the same test proves the fix.
Only synthetic mail and mocked providers are used.
"""
from contextlib import nullcontext
from dataclasses import replace
import threading
import time
from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.app import authoritative_prediction, create_app, manual_prediction
from src import llm_api
from src.action_center import list_actions, persist_analysis_actions
from src.prediction import EmailAnalysis
from src.config import Settings
from src.database import connection
from src.db_utils import get_recent_emails, log_email_to_db
from src.notifier import Delivery
from src.pipeline import process_task
from src.prediction import Prediction
from src.privacy import provider_email_text
from src.work_queue import (
    claim_cycle, finish_cycle, newest_due_tasks, reconcile_email_analysis,
)
from tests.test_intelligence_phase4 import (
    A, B, ORIGIN, SOURCE_TIME, EmptyCollection, Phase4Base,
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

    def __init__(self):
        self.senders = []

    def predict_with_usage(self, subject, body, *, sender='', account_id=None):
        self.senders.append(sender)
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
        self.settings = replace(self.settings, shadow_model_enabled=True)  # these tests compare against the shadow
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

    def test_local_only_decision_uses_the_sender_like_training_and_the_worker(self):
        # BUG-03: the checkpoint is trained with the sender in its input
        # (mailmind_uses_sender) and the worker passes it; re-analysis did not.
        model = LoadedShadowModel()
        authoritative_prediction(
            model, 'Approval needed', self.body, A, Settings(local_only=True),
            Mock(), Mock(), sender='Sender <sender@example.test>')
        self.assertEqual(model.senders, ['Sender <sender@example.test>'])

    def test_manual_sandbox_has_no_sender_placeholder_in_model_input(self):
        model = LoadedShadowModel()
        manual_prediction(model, 'Subject', 'Body', A, Settings(local_only=True),
                          Mock(), Mock())
        self.assertEqual(model.senders, [''])

    def test_reanalyze_records_the_shadow_beside_the_cloud_decision(self):
        client = Mock()
        client.models.generate_content.return_value = gemini_response(
            enriched_output('IMPORTANT'))
        with patch.object(llm_api, 'get_client', return_value=client), \
             patch.object(llm_api.vector_db, 'search_similar_emails', return_value=[]):
            self.assertEqual(
                self.client.post('/emails/mail-1/reanalyze', json={}).status_code, 200)
        latest = self.client.get('/emails').json()['emails'][0]['latest_prediction']
        self.assertEqual((latest['source'], latest['category']), ('gemini', 'IMPORTANT'))
        self.assertEqual((latest['local']['source'], latest['local']['category']),
                         ('local', 'SPAM'))
        with connection(self.settings.db_path) as conn:
            operations = {row[0] for row in conn.execute(
                'SELECT operation FROM token_usage_events')}
        self.assertIn('local_shadow', operations)

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


class DeadlineContractTests(Phase4Base):
    """LLM-04: found by the live evaluation. 35 of 60 Gemini actions and 34 of 63
    Groq actions were discarded over valid deadline shapes the prompt never forbade."""

    def parse(self, due_at, due_precision):
        action = {**action_output('payment_required', 'pay [AMOUNT] for the venue'),
                  'due_at': due_at, 'due_precision': due_precision}
        _category, analysis = llm_api.parse_provider_analysis(
            json.dumps(enriched_output('IMPORTANT', [action])), source='gemini',
            model_version='synthetic', source_text='Please pay [AMOUNT] for the venue by Friday.')
        return analysis.actions

    def test_date_only_deadline_may_be_a_plain_calendar_date(self):
        actions = self.parse('2026-10-02', 'date_only')
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].due_precision, 'date_only')
        self.assertEqual(actions[0].due_at, '2026-10-02T00:00:00+05:30')

    def test_date_only_deadline_is_stored_on_that_local_calendar_day(self):
        self.seed_email(subject='Invoice due', body='Please pay Rs 5,000 for the venue by Friday.')
        with connection(self.settings.db_path) as conn:
            result = persist_analysis_actions(
                A, 'mail-1', EmailAnalysis(
                    predicted_category='IMPORTANT', explanation_summary='Payment request.',
                    signals=(), source='gemini', model_version='synthetic',
                    actions=self.parse('2026-10-02', 'date_only')),
                source_created_at=SOURCE_TIME,
                source_text=provider_email_text('Invoice due',
                                                'Please pay Rs 5,000 for the venue by Friday.'),
                db_conn=conn)
        self.assertEqual(result['created_count'], 1)
        due = list_actions(A, db_path=self.settings.db_path)[0]['due_at']
        local = datetime.fromisoformat(due.replace('Z', '+00:00')).astimezone(
            ZoneInfo('Asia/Kolkata'))
        self.assertEqual((local.date().isoformat(), local.hour), ('2026-10-02', 9))

    def test_unresolved_deadline_keeps_the_task_without_inventing_a_date(self):
        actions = self.parse(None, 'relative')
        self.assertEqual([(action.due_at, action.due_precision) for action in actions],
                         [(None, 'unknown')])

    def test_a_time_without_an_offset_is_local_but_a_bare_date_is_not_exact(self):
        # TZ-01: a time with no zone is the user's local time.
        self.assertEqual(self.parse('2026-10-02T17:00:00', 'exact_time')[0].due_at, '2026-10-02T17:00:00+05:30')
        self.assertEqual(self.parse('2026-10-02', 'exact_time'), ())


class ExplanationHonestyTests(Phase4Base):
    """LLM-05: 368 of 412 real emails showed 'no additional validated explanation',
    although none had ever been generated; and one imperfect quote discarded a
    whole valid explanation."""

    SOURCE = 'Subject: Contract | Body: Please sign the "revised" contract before 5 pm - today.'

    def explanation(self, signals):
        _category, analysis = llm_api.parse_provider_analysis(
            json.dumps({'category': 'IMPORTANT',
                        'explanation': {'summary': 'The sender asks for a signature today.',
                                        'signals': signals},
                        'actions': []}),
            source='gemini', model_version='synthetic', source_text=self.SOURCE)
        return analysis

    def test_email_without_a_recorded_explanation_says_so_honestly(self):
        with connection(self.settings.db_path) as conn:
            log_email_to_db('old-mail', 'Sender <s@example.test>', 'Old', 'Saved before explanations.',
                            Prediction(category='IMPORTANT', outcome='CLASSIFIED', source='gemini',
                                       model_version='synthetic'),
                            Prediction(), account_id=A, db_conn=conn)
            summary = get_recent_emails(1, account_id=A, db_conn=conn, ranked_ids=['old-mail'],
                                        include_analysis=True)[0]['analysis']['explanation_summary']
        self.assertIn('No explanation was recorded', summary)
        self.assertNotIn('no additional validated explanation', summary)

    def test_one_unverifiable_quote_does_not_discard_the_explanation(self):
        analysis = self.explanation([
            {'signal': 'direct_request', 'evidence': 'Please sign the "revised" contract'},
            {'signal': 'deadline', 'evidence': 'by end of day'},   # not in the email
        ])
        self.assertEqual(analysis.explanation_summary, 'The sender asks for a signature today.')
        self.assertEqual([(s.signal, s.evidence) for s in analysis.signals],
                         [('direct_request', 'Please sign the "revised" contract'), ('deadline', None)])

    def test_typographic_punctuation_still_counts_as_the_same_quote(self):
        analysis = self.explanation([
            {'signal': 'direct_request', 'evidence': 'sign the “revised” contract'},
            {'signal': 'deadline', 'evidence': 'before 5 pm — today…'},
        ])
        # Both signals must survive with their quotes (not a vacuous empty list).
        self.assertEqual([signal.signal for signal in analysis.signals], ['direct_request', 'deadline'])
        self.assertTrue(all(signal.evidence for signal in analysis.signals))

    def test_actions_use_the_same_quote_matching(self):
        # Grounding must mean the same thing when parsing and when persisting.
        self.seed_email(subject='Contract', body='Please sign the "revised" contract before 5 pm - today.')
        _category, analysis = llm_api.parse_provider_analysis(
            json.dumps(enriched_output('IMPORTANT', [
                action_output('approval_required', 'sign the “revised” contract')])),
            source='gemini', model_version='synthetic', source_text=self.SOURCE)
        self.assertEqual(len(analysis.actions), 1)
        with connection(self.settings.db_path) as conn:
            result = persist_analysis_actions(
                A, 'mail-1', analysis, source_created_at=SOURCE_TIME,
                source_text=provider_email_text(
                    'Contract', 'Please sign the "revised" contract before 5 pm - today.'),
                db_conn=conn)
        self.assertEqual(result['created_count'], 1)


class TelegramDeliveryTimeoutTests(Phase4Base):
    """NOTIFY-01: reported as 'dead - notify - provider_timeout' (7 of 79 real
    alerts). The worker stopped waiting before the request's own timeouts, and a
    send that never started was recorded as an ambiguous delivery."""

    def setUp(self):
        super().setUp()
        self.settings = replace(self.settings, provider_timeout_seconds=1)
        self.seed_email(subject='Contract', body='Please sign the contract today.')
        self.manager, _token = self.connected_manager(self.settings)
        self.context = self.manager.worker_context()
        with connection(self.settings.db_path) as conn:
            conn.execute("""INSERT INTO processing_tasks(account_id,email_id,status,stage,created_at,updated_at)
                            VALUES (?,?,'queued','classify',?,?)""", (A, 'mail-1', SOURCE_TIME, SOURCE_TIME))

    def run_task(self, notifier):
        token, job = claim_cycle(self.manager, self.context)
        self.addCleanup(finish_cycle, self.manager, self.context, token, job)
        task = newest_due_tasks(self.manager, self.context, token, 1)[0]
        cloud = Mock(return_value=Prediction(category='IMPORTANT', outcome='CLASSIFIED',
                                             source='gemini', model_version='synthetic'))
        shadow = Mock(return_value=(Prediction(category='IMPORTANT', outcome='CLASSIFIED',
                                               source='local'), 5))
        process_task(task, self.manager, self.context, token, Mock(), Mock(), Mock(),
                     classifier=cloud, shadow=shadow, notifier=notifier, marker=Mock(),
                     logger=log_email_to_db, validator=Mock(), job_id=job)
        with connection(self.settings.db_path) as conn:
            outbox = conn.execute("SELECT status FROM notification_outbox WHERE email_id='mail-1'").fetchone()[0]
            task_state = conn.execute("SELECT status FROM processing_tasks WHERE email_id='mail-1'").fetchone()[0]
        return outbox, task_state

    def test_slow_but_successful_send_is_recorded_as_sent(self):
        # Longer than the old 1 s wait, well inside the request's own limits.
        def slow_send(*args, **kwargs):
            time.sleep(1.6)
            return Delivery('sent', message_id='42')
        self.assertEqual(self.run_task(slow_send), ('sent', 'complete'))

    def test_send_that_never_started_is_retried_not_marked_unknown(self):
        release = threading.Event()
        from src.provider_policy import TELEGRAM_CALLS
        with self.assertRaises(TimeoutError):   # an earlier send still occupies the only slot
            TELEGRAM_CALLS.run(lambda: release.wait(10), 0.01)
        self.addCleanup(release.set)
        notifier = Mock(return_value=Delivery('sent', message_id='1'))
        self.assertEqual(self.run_task(notifier), ('retry', 'retry'))
        notifier.assert_not_called()


class ActionTypeFilterTests(Phase4Base):
    """FEAT-01: filter the Action Center by action type; options come from the
    types the account actually has."""

    def setUp(self):
        super().setUp()
        self.seed_email()
        for index, action_type in enumerate(('payment_required', 'payment_required', 'meeting')):
            self.create_action(action_type=action_type, title=f'Task {index}',
                               evidence=f'approve the launch checklist {index}')
        model = Mock(model_loaded=False, load_reason='missing_checkpoint')
        self.app = create_app(settings=self.settings, model_factory=Mock(return_value=model),
                              vector_factory=Mock(return_value=EmptyCollection()))
        self.client = TestClient(self.app, base_url='http://localhost')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.headers['Origin'] = ORIGIN
        self.client.headers['X-CSRF-Token'] = self.client.post('/session').json()['csrf_token']
        context, _ = self.app.state.accounts.session(self.client.cookies.get('mailmind_session'))
        self.app.state.accounts.finish_auth(self.app.state.accounts.begin_auth(context), (A, '{}'))

    def test_actions_can_be_filtered_by_type(self):
        payments = self.client.get('/actions?action_type=payment_required').json()['actions']
        self.assertEqual({a['action_type'] for a in payments}, {'payment_required'})
        self.assertEqual(len(payments), 2)

    def test_summary_counts_only_the_types_that_exist(self):
        summary = self.client.get('/actions/summary').json()
        self.assertEqual(summary['type_counts'], {'payment_required': 2, 'meeting': 1})

    def test_an_unknown_type_is_rejected(self):
        self.assertEqual(self.client.get('/actions?action_type=urgent').status_code, 422)

    def test_the_api_accepts_exactly_the_contract_action_types(self):
        from typing import get_args
        from api.app import ACTION_TYPE_VALUES
        from src.intelligence_contract import ActionType
        self.assertEqual(set(get_args(ACTION_TYPE_VALUES)), {item.value for item in ActionType})


class NonNumericRetryTimeTests(Phase4Base):
    """QUEUE-01: 'Fetch next 100' stayed disabled. Seven real tasks had a text
    next_retry_at ('2026-09-29T13:00:26...+00:00'); SQLite ranks text above every
    number, so 'next_retry_at <= now' was never true and the backlog never ended."""

    def setUp(self):
        super().setUp()
        self.seed_email()
        self.manager, _token = self.connected_manager()
        self.context = self.manager.worker_context()
        with connection(self.settings.db_path) as conn:
            conn.execute("""INSERT INTO processing_tasks(account_id,email_id,status,stage,source,
                            next_retry_at,created_at,updated_at)
                            VALUES (?,?,'retry','classify','backlog','2026-09-29T13:00:26.931491+00:00',?,?)""",
                         (A, 'mail-1', SOURCE_TIME, SOURCE_TIME))

    def test_a_text_retry_time_is_repaired_and_the_task_becomes_due(self):
        token, job = claim_cycle(self.manager, self.context)
        self.addCleanup(finish_cycle, self.manager, self.context, token, job)
        with connection(self.settings.db_path) as conn:
            kind = conn.execute("SELECT typeof(next_retry_at) FROM processing_tasks WHERE email_id='mail-1'").fetchone()[0]
        self.assertIn(kind, ('real', 'integer'))
        due = newest_due_tasks(self.manager, self.context, token, 5)
        self.assertEqual([task['email_id'] for task in due], ['mail-1'])


class ConfigurationMessageTests(unittest.TestCase):
    """CFG-01: local-only startup failed with 'Telegram action reminders are
    unavailable in local-only mode' but did not say which setting to change."""

    def assert_message_names(self, settings, *settings_to_change):
        with self.assertRaises(ValueError) as caught:
            Settings(**settings)
        for name in settings_to_change:
            self.assertIn(name, str(caught.exception))

    def test_local_only_conflict_names_the_setting_to_turn_off(self):
        self.assert_message_names(
            dict(local_only=True, action_extraction_enabled=True, action_reminders_enabled=True,
                 telegram_action_reminders_enabled=True),
            'MAILMIND_TELEGRAM_ACTION_REMINDERS_ENABLED=false', 'MAILMIND_LOCAL_ONLY=false')

    def test_feature_dependencies_name_their_settings(self):
        self.assert_message_names(dict(action_reminders_enabled=True),
                                  'MAILMIND_ACTION_EXTRACTION_ENABLED=true')
        self.assert_message_names(dict(action_extraction_enabled=True, telegram_action_reminders_enabled=True),
                                  'MAILMIND_ACTION_REMINDERS_ENABLED=true')
        self.assert_message_names(dict(token_collection_enabled=False, token_analytics_visible=True),
                                  'MAILMIND_TOKEN_COLLECTION_ENABLED=true')


class AccountDeletionErrorTests(Phase4Base):
    """ERR-02: found by the Git Bash guide's sandbox run. A vector-store failure
    after the purge transition was reported as 'There is no account to delete.'"""

    def client_for(self, vector_factory):
        model = Mock(model_loaded=False, load_reason='missing_checkpoint')
        app = create_app(settings=self.settings, model_factory=Mock(return_value=model),
                         vector_factory=vector_factory)
        client = TestClient(app, base_url='http://localhost')
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        client.headers['Origin'] = ORIGIN
        client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
        return app, client

    def test_interrupted_deletion_says_retry_not_that_there_is_no_account(self):
        app, client = self.client_for(Mock(side_effect=ValueError('vector store unavailable')))
        context, _ = app.state.accounts.session(client.cookies.get('mailmind_session'))
        app.state.accounts.finish_auth(app.state.accounts.begin_auth(context), (A, '{}'))
        client.headers['X-CSRF-Token'] = client.get('/session').json()['csrf_token']
        self.seed_email()
        response = client.delete('/account-data')
        self.assertEqual(response.status_code, 503)
        self.assertIn('Retry', response.json()['detail'])
        self.assertTrue(client.get('/status').json()['purge_pending'])

    def test_deleting_without_an_account_is_a_conflict(self):
        _app, client = self.client_for(Mock(return_value=EmptyCollection()))
        response = client.delete('/account-data')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], 'There is no account to delete.')


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


class PagedHistoryMailbox:
    """Gmail-like history paging: rejects maxResults < 1 with HTTP 400."""

    def __init__(self, ids, *, page_size, current='99', fail_page=None):
        from tests.test_phase3_ingestion import leaf
        self.ids, self.page_size, self.current, self.fail_page = ids, page_size, current, fail_page
        self.items = {key: {'payload': leaf('Live mail ' + key)} for key in ids}
        self.requests, self.gets = [], []

    def users(self): return self
    def history(self): return self
    def messages(self): return self

    def list(self, **kwargs):
        from tests.support import FakeRequest
        self.requests.append(kwargs)
        page = int(kwargs.get('pageToken', 0))
        if kwargs['maxResults'] < 1 or page == self.fail_page:
            error = RuntimeError('synthetic history rejection')
            error.resp = Mock(status=400 if kwargs['maxResults'] < 1 else 500)
            raise error
        start = page * self.page_size
        chunk = self.ids[start:start + min(self.page_size, kwargs['maxResults'])]
        result = {'historyId': self.current,
                  'history': [{'messagesAdded': [{'message': {'id': key}} for key in chunk]}]}
        if start + len(chunk) < len(self.ids):
            result['nextPageToken'] = str(page + 1)
        return FakeRequest(result)

    def get(self, **kwargs):
        from tests.support import FakeRequest
        self.gets.append(kwargs['id'])
        return FakeRequest(self.items[kwargs['id']])


class HistoryPagingRegressionTests(unittest.TestCase):
    """INGEST-01: a full first history page made the next request ask for
    maxResults=0 (Gmail: HTTP 400). The error path returned with the cursor
    already moved to the mailbox's current position, so the whole batch of
    new mail was skipped for good."""

    def test_full_first_page_admits_the_batch_and_keeps_the_cursor(self):
        from src.email_client import get_new_emails
        service = PagedHistoryMailbox([f'live-{i}' for i in range(5)], page_size=2)
        batch = get_new_emails(service, '10', max_results=2)
        self.assertTrue(all(request['maxResults'] >= 1 for request in service.requests))
        self.assertFalse(batch.listing_error)
        self.assertEqual([email['id'] for email in batch.emails], ['live-0', 'live-1'])
        # More mail remains, so the next cycle must re-read from the same cursor.
        self.assertTrue(batch.has_more)
        self.assertEqual(batch.history_id, '10')

    def test_failed_later_page_never_advances_the_cursor(self):
        from src.email_client import get_new_emails
        service = PagedHistoryMailbox([f'live-{i}' for i in range(5)], page_size=2, fail_page=1)
        batch = get_new_emails(service, '10', max_results=10)
        self.assertTrue(batch.listing_error)
        self.assertEqual(batch.history_id, '10')

    def test_backlog_of_new_mail_drains_across_cycles(self):
        from src.email_client import get_new_emails
        ids = [f'live-{i}' for i in range(23)]
        service = PagedHistoryMailbox(ids, page_size=20)
        cursor, admitted = '10', []
        for _ in range(5):
            batch = get_new_emails(service, cursor, max_results=20, exclude_ids=set(admitted))
            self.assertFalse(batch.listing_error)
            admitted += [email['id'] for email in batch.emails]
            cursor = batch.history_id
            if not batch.has_more:
                break
        self.assertEqual(sorted(admitted), sorted(ids))
        self.assertEqual(cursor, '99')


class IngestionCycleBase(unittest.TestCase):
    """A connected synthetic account with a seeded history cursor and one
    message queued as a due ingestion retry."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from src.account_state import AccountManager
        from src.database import initialize_database
        from tests.test_phase3_ingestion import A as ACCOUNT
        self.account = ACCOUNT
        self.temp = tempfile.TemporaryDirectory(prefix='mailmind-ingest-retry-')
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name), batch_size=2,
                                 gmail_page_size=1, gmail_max_pages=2)
        initialize_database(self.settings.db_path)
        self.manager = AccountManager(self.settings)
        token, _ = self.manager.open_session()
        context, _ = self.manager.session(token)
        self.manager.finish_auth(self.manager.begin_auth(context), (ACCOUNT, '{}'))
        from src.db_utils import ensure_ingestion_state
        with self.manager.transaction() as conn:
            ensure_ingestion_state(ACCOUNT, conn, batch_limit=self.settings.max_pending_tasks)
            conn.execute("""UPDATE ingestion_state SET history_id=?,history_bootstrap_complete=1,
                initial_batch_complete=1 WHERE account_id=?""", ('10', ACCOUNT))
            conn.execute("""INSERT INTO ingestion_failures(account_id,email_id,attempt_count,status,
                next_retry_at,error_code,updated_at) VALUES (?,?,1,'retry',0,'message_fetch_failed',?)""",
                (ACCOUNT, 'stuck-1', '2026-10-01T00:00:00.000000Z'))


class IngestionFailureRetryTests(IngestionCycleBase):
    """INGEST-02: a message that failed to download was recorded for retry,
    but nothing ever fetched it again (the history cursor had moved on)."""

    def test_due_ingestion_failure_is_fetched_again(self):
        from src import main
        from src.db_utils import get_email
        service = PagedHistoryMailbox([], page_size=20, current='10')
        service.items['stuck-1'] = PagedHistoryMailbox(['stuck-1'], page_size=1).items['stuck-1']
        model = Mock(model_loaded=True)
        model.predict.return_value = Prediction(category='UPDATES', outcome='CLASSIFIED')
        with patch.object(main, 'refresh_gmail', return_value=service), \
             patch.object(main, 'classify_email', return_value=Prediction(
                 category='UPDATES', outcome='CLASSIFIED', source='gemini')), \
             patch.object(main, 'send_telegram_alert', return_value=True):
            main._run_agent(settings=self.settings, manager=self.manager, model=model,
                            collection=Mock())
        self.assertIn('stuck-1', service.gets)
        self.assertIsNotNone(get_email('stuck-1', account_id=self.account,
                                       db_path=self.settings.db_path))
        with connection(self.settings.db_path) as conn:
            remaining = conn.execute('SELECT COUNT(*) FROM ingestion_failures').fetchone()[0]
        self.assertEqual(remaining, 0)


class RecentInboxSafetyNetTests(IngestionCycleBase):
    """INGEST-03: history-cursor sync is not the only line of defence. Recent
    INBOX mail that MailMind never saved (any future cursor bug or a Gmail
    quirk) is found by a periodic, bounded sweep and fetched."""

    def setUp(self):
        super().setUp()
        with self.manager.transaction() as conn:
            conn.execute('DELETE FROM ingestion_failures')
        from src import main
        main._LAST_RECENT_SWEEP.clear()

    def cycle(self, service):
        from src import main
        model = Mock(model_loaded=True)
        model.predict.return_value = Prediction(category='UPDATES', outcome='CLASSIFIED')
        with patch.object(main, 'refresh_gmail', return_value=service), \
             patch.object(main, 'classify_email', return_value=Prediction(
                 category='UPDATES', outcome='CLASSIFIED', source='gemini')), \
             patch.object(main, 'send_telegram_alert', return_value=True):
            main._run_agent(settings=self.settings, manager=self.manager, model=model,
                            collection=Mock())

    def recent_service(self, recent_ids):
        from tests.support import FakeRequest
        service = PagedHistoryMailbox([], page_size=20, current='10')
        for key in recent_ids:
            service.items[key] = PagedHistoryMailbox([key], page_size=1).items[key]
        history_list = service.list
        service.recent_queries = []

        def listing(**kwargs):
            if 'startHistoryId' in kwargs:
                return history_list(**kwargs)
            if 'q' in kwargs:
                service.recent_queries.append(kwargs)
                return FakeRequest({'messages': [{'id': key} for key in recent_ids]})
            return FakeRequest({'messages': []})
        service.list = listing
        return service

    def test_recent_inbox_mail_missed_by_history_is_recovered(self):
        from src.db_utils import get_email
        service = self.recent_service(['missed-1', 'missed-2'])
        self.cycle(service)
        query = service.recent_queries[0]
        self.assertEqual(query['labelIds'], ['INBOX'])
        self.assertIn('newer_than:', query['q'])
        for key in ('missed-1', 'missed-2'):
            self.assertIsNotNone(get_email(key, account_id=self.account,
                                           db_path=self.settings.db_path))

    def test_saved_mail_is_not_fetched_again_and_sweep_is_rate_limited(self):
        service = self.recent_service(['missed-1'])
        self.cycle(service)
        self.cycle(service)  # immediately again: no second listing
        self.assertEqual(service.gets.count('missed-1'), 1)
        self.assertEqual(len(service.recent_queries), 1)



class DecisionContextTests(unittest.TestCase):
    """EXPLAIN-01: "Why this category?" shows the whole decision, not only the
    signals: who decided (and whether a fallback stepped in), the user's past
    corrections used as examples, the local second opinion, the user's own
    label and the time taken, all from data saved with the decision."""

    MODELS = ('gemini-primary', 'gemini-fallback')

    def item(self, **latest):
        base = {'category': 'IMPORTANT', 'outcome': 'CLASSIFIED', 'source': 'gemini',
                'model_version': 'gemini-primary', 'support': 0, 'elapsed_ms': 1201.0,
                'retrieval_status': 'not_used', 'local': {'category': 'IMPORTANT', 'outcome': 'CLASSIFIED'}}
        base.update(latest)
        return {'latest_prediction': base, 'human_label': None}

    def context(self, item):
        from src.email_analysis import decision_context
        return decision_context(item, self.MODELS)

    def test_primary_decision_with_agreeing_second_opinion(self):
        context = self.context(self.item())
        self.assertEqual((context['decided_by'], context['category'], context['route']),
                         ('model', 'IMPORTANT', 'primary'))
        self.assertEqual((context['provider'], context['model_version']), ('gemini', 'gemini-primary'))
        self.assertEqual(context['precedents'], {'used': 0, 'lookup': 'none_close_enough'})
        self.assertEqual(context['second_opinion'], {'category': 'IMPORTANT', 'agrees': True})
        self.assertEqual(context['elapsed_ms'], 1201.0)
        self.assertIsNone(context['your_label'])

    def test_fallback_groq_and_local_routes(self):
        self.assertEqual(self.context(self.item(model_version='gemini-fallback'))['route'], 'fallback')
        self.assertEqual(self.context(self.item(source='groq', model_version='openai/x'))['route'], 'groq')
        local = self.context(self.item(source='local', model_version='distilbert', local=None))
        self.assertEqual(local['route'], 'local')
        self.assertIsNone(local['second_opinion'])

    def test_precedents_disagreement_and_user_override(self):
        item = self.item(support=3, retrieval_status='available',
                         local={'category': 'SPAM', 'outcome': 'CLASSIFIED'})
        item['human_label'] = 'UPDATES'
        context = self.context(item)
        self.assertEqual(context['precedents'], {'used': 3, 'lookup': 'used'})
        self.assertEqual(context['second_opinion'], {'category': 'SPAM', 'agrees': False})
        self.assertEqual((context['decided_by'], context['your_label']), ('you', 'UPDATES'))
        unavailable = self.context(self.item(retrieval_status='unavailable'))
        self.assertEqual(unavailable['precedents']['lookup'], 'unavailable')

    def test_unclassified_mail_has_no_decision_context(self):
        self.assertIsNone(self.context({'latest_prediction': None, 'human_label': None}))
        self.assertIsNone(self.context(self.item(category=None, outcome='ERROR')))



class ShadowModelSwitchTests(Phase4Base):
    """SHADOW-01: the local shadow model is off by default. It only ever
    compared answers in normal mode, so by default MailMind skips the PyTorch
    subprocess and the extra CPU per email; MAILMIND_SHADOW_MODEL_ENABLED=true
    brings it back. Local-only mode always uses its model."""

    def test_default_is_off_and_local_only_ignores_the_switch(self):
        self.assertFalse(Settings().shadow_model_enabled)
        with patch.dict(llm_api.os.environ, {}, clear=True):
            self.assertFalse(Settings.from_environment().shadow_model_enabled)
        with patch.dict(llm_api.os.environ, {'MAILMIND_SHADOW_MODEL_ENABLED': 'true'}, clear=True):
            self.assertTrue(Settings.from_environment().shadow_model_enabled)
        self.assertFalse(Settings().shadow_active)
        self.assertTrue(Settings(shadow_model_enabled=True).shadow_active)
        self.assertFalse(Settings(local_only=True, shadow_model_enabled=True).shadow_active)

    def test_api_does_not_start_the_model_and_reports_it_off(self):
        import api.app as app_module
        from src.local_llm import MailMindModel
        with patch.object(app_module, 'DeferredMailMindModel',
                          side_effect=AssertionError('shadow must not start')):
            app = create_app(settings=self.settings, model_factory=MailMindModel,
                             vector_factory=Mock(return_value=EmptyCollection()))
            with TestClient(app, base_url='http://localhost') as client:
                client.headers['Origin'] = ORIGIN
                client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
                context, _ = app.state.accounts.session(client.cookies.get('mailmind_session'))
                app.state.accounts.finish_auth(app.state.accounts.begin_auth(context), (A, '{}'))
                local = client.get('/telemetry').json()['local_model']
                self.assertEqual((local['status'], local['ready']), ('off', False))
                self.assertEqual(app.state.model.load_reason, 'shadow_disabled')

    def test_sandbox_prediction_borrows_the_cloud_when_the_shadow_is_off(self):
        from src.classification_service import manual_prediction as sandbox
        cloud = Mock(return_value=Prediction(category='UPDATES', outcome='CLASSIFIED', source='gemini'))
        off = Mock(model_loaded=False, load_reason='shadow_disabled')
        result = sandbox(off, 'Subject', 'Body', A, self.settings, Mock(), Mock(), classifier=cloud)
        self.assertEqual(result.source, 'gemini')
        cloud.assert_called_once()

    def test_worker_classifies_without_starting_or_calling_the_shadow(self):
        from src import main
        from tests.test_phase3_ingestion import Mailbox, leaf
        manager, _ = self.connected_manager()
        service = Mailbox({'live-1': {'payload': leaf('Please confirm the meeting.')}})
        with patch.object(main, 'DeferredMailMindModel', side_effect=AssertionError('shadow must not start')),              patch.object(main, 'MailMindModel', side_effect=AssertionError('no local model in normal mode')),              patch.object(main, 'refresh_gmail', return_value=service),              patch.object(main, 'classify_email', return_value=Prediction(
                 category='IMPORTANT', outcome='CLASSIFIED', source='gemini')),              patch.object(main, 'send_telegram_alert', return_value=Delivery('sent', message_id='1')):
            main._model = None
            main._run_agent(settings=self.settings, manager=manager, collection=Mock())
        with connection(self.settings.db_path) as conn:
            row = conn.execute("SELECT prediction,local_prediction FROM email_logs WHERE email_id='live-1'").fetchone()
            operations = {r[0] for r in conn.execute('SELECT operation FROM token_usage_events')}
        self.assertEqual((row['prediction'], row['local_prediction']), ('IMPORTANT', None))
        self.assertNotIn('local_shadow', operations)



class NetworkLossTests(Phase4Base):
    """NET-01: losing the internet must not log the user out of Google.
    Before, any failure of the per-cycle Gmail check paused the account, so a
    Wi-Fi blip meant clicking Connect Google again. Now only a proven login
    problem pauses; network trouble keeps the account connected, skips the
    cycle (so emails don't burn their retries offline) and is reported."""

    def refresh_with(self, error):
        from src import email_client
        manager, _ = self.connected_manager()
        context = manager.worker_context()
        manager.write_credentials(context, '{}')
        creds = Mock(valid=True)
        with patch.object(email_client.Credentials, 'from_authorized_user_file', return_value=creds),              patch.object(email_client, 'build_gmail', return_value=Mock()),              patch.object(email_client, 'execute_gmail', side_effect=error):
            return email_client.refresh_gmail(manager, context)

    def test_network_errors_are_temporary_not_a_logout(self):
        import socket
        import httplib2
        from google.auth.exceptions import TransportError
        from src.email_client import GmailUnavailable
        for error in (httplib2.ServerNotFoundError('offline'), socket.gaierror('dns'),
                      TransportError('no route'), TimeoutError('slow'), ConnectionResetError('reset')):
            with self.subTest(error=type(error).__name__), self.assertRaises(GmailUnavailable) as raised:
                self.refresh_with(error)
            self.assertEqual(raised.exception.code, 'network_unavailable')

    def test_gmail_outage_is_temporary_but_login_problems_still_pause(self):
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError
        from src.email_client import GmailUnavailable

        def http_error(status):
            return HttpError(Mock(status=status, reason='synthetic'), b'{}')
        with self.assertRaises(GmailUnavailable) as raised:
            self.refresh_with(http_error(503))
        self.assertEqual(raised.exception.code, 'gmail_temporarily_unavailable')
        for error in (RefreshError('invalid_grant'), http_error(401), http_error(403)):
            with self.subTest(error=repr(error)[:30]):
                self.assertIsNone(self.refresh_with(error))

    def run_cycle(self, refresh):
        from src import main
        manager, _ = self.connected_manager()
        self.seed_email('queued-mail')
        with manager.transaction() as conn:
            conn.execute("""INSERT INTO processing_tasks(account_id,email_id,created_at,updated_at,source)
                VALUES (?,?,?,?,'live') ON CONFLICT DO NOTHING""",
                (A, 'queued-mail', '2026-10-06T00:00:00.000000Z', '2026-10-06T00:00:00.000000Z'))
            before = conn.execute('SELECT generation FROM runtime_state').fetchone()[0]
        cloud = Mock(side_effect=AssertionError('nothing is classified while offline'))
        with patch.object(main, 'refresh_gmail', **refresh), patch.object(main, 'classify_email', cloud):
            result = main._run_agent(settings=self.settings, manager=manager, collection=Mock())
        with manager.transaction() as conn:
            state = conn.execute('SELECT connected,generation FROM runtime_state').fetchone()
            health = conn.execute('SELECT last_error_code FROM worker_health WHERE account_id=?', (A,)).fetchone()
            task = conn.execute("SELECT status,attempt_count FROM processing_tasks WHERE email_id='queued-mail'").fetchone()
        return result, before, state, health, task

    def test_offline_cycle_keeps_the_account_connected_and_spares_the_retries(self):
        from src.email_client import GmailUnavailable
        result, before, state, health, task = self.run_cycle(
            {'side_effect': GmailUnavailable('network_unavailable')})
        self.assertEqual(result['status'], 'offline')
        self.assertEqual((state['connected'], state['generation']), (1, before))
        self.assertEqual(health['last_error_code'], 'network_unavailable')
        self.assertEqual((task['status'], task['attempt_count']), ('queued', 0))

    def test_a_real_login_problem_still_pauses_the_account(self):
        result, before, state, health, _task = self.run_cycle({'return_value': None})
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(state['connected'], 0)



class OfflineBackoffTests(Phase4Base):
    """NET-02: while Gmail is unreachable the worker retries with exponential
    backoff (5 s, 10 s, 20 s, 40 s, then every 60 s), resets on success, and
    /status tells the dashboard how many checks failed and when the next is."""

    def test_backoff_doubles_from_five_seconds_and_caps_at_a_minute(self):
        from src.provider_policy import offline_retry_delay
        self.assertEqual([offline_retry_delay(n) for n in range(1, 8)], [5, 10, 20, 40, 60, 60, 60])

    def test_worker_waits_longer_while_offline_and_resets_on_success(self):
        from src.provider_policy import worker_wait
        failures, waits = 0, []
        for status in ('offline', 'offline', 'offline', 'complete', 'offline'):
            wait, failures = worker_wait(status, failures, poll_seconds=5)
            waits.append(wait)
        self.assertEqual(waits, [5, 10, 20, 5, 5])

    def test_status_reports_failed_checks_and_the_next_retry(self):
        from src.provider_policy import offline_retry_delay
        manager, token = self.connected_manager()
        app = create_app(settings=self.settings, model_factory=Mock(return_value=Mock(model_loaded=False)),
                         vector_factory=Mock(return_value=EmptyCollection()))
        with TestClient(app, base_url='http://localhost') as client:
            client.headers['Origin'] = ORIGIN
            client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
            context, _ = app.state.accounts.session(client.cookies.get('mailmind_session'))
            app.state.accounts.finish_auth(app.state.accounts.begin_auth(context), (A, '{}'))
            self.assertEqual(client.get('/status').json()['connectivity'],
                             {'gmail': 'ok', 'failed_checks': 0, 'retry_in_seconds': None})
            from src.database import utc_timestamp
            with app.state.accounts.transaction() as conn:
                generation = conn.execute('SELECT generation FROM runtime_state').fetchone()[0]
                for _ in range(3):
                    stamp = utc_timestamp()
                    conn.execute("""INSERT INTO worker_jobs(account_id,status,error_code,created_at,updated_at,generation)
                        VALUES (?,'failed','network_unavailable',?,?,?)""", (A, stamp, stamp, generation))
                conn.execute("""INSERT INTO worker_health(account_id,heartbeat_at,last_error_at,last_error_code)
                    VALUES (?,?,?,'network_unavailable') ON CONFLICT(account_id) DO UPDATE SET
                    last_error_at=excluded.last_error_at,last_error_code=excluded.last_error_code""", (A, stamp, stamp))
            connectivity = client.get('/status').json()['connectivity']
        self.assertEqual((connectivity['gmail'], connectivity['failed_checks']), ('unreachable', 3))
        self.assertTrue(0 <= connectivity['retry_in_seconds'] <= offline_retry_delay(3))



class LoginErrorClearingTests(Phase4Base):
    """AUTH-01: the dashboard says "sign-in needs renewing" only while the
    pause really came from a rejected login. Signing in again, or a deliberate
    Disconnect, clears that reason even before the next successful sync."""

    def rejected_login(self):
        manager, token = self.connected_manager()
        context = manager.worker_context()
        with manager.transaction() as conn:
            conn.execute("""INSERT INTO worker_health(account_id,last_error_at,last_error_code)
                VALUES (?,?,'gmail_unavailable') ON CONFLICT(account_id) DO UPDATE SET
                last_error_at=excluded.last_error_at,last_error_code=excluded.last_error_code""",
                         (A, '2026-10-06T00:00:00Z'))
        manager.pause(context)
        return manager, token

    def error_code(self, manager):
        with manager.transaction() as conn:
            row = conn.execute('SELECT last_error_code FROM worker_health WHERE account_id=?', (A,)).fetchone()
        return row['last_error_code'] if row else None

    def test_signing_in_again_clears_the_login_error(self):
        manager, token = self.rejected_login()
        self.assertEqual(self.error_code(manager), 'gmail_unavailable')
        browser, _ = manager.session(token)
        manager.finish_auth(manager.begin_auth(browser), (A, '{}'))
        self.assertIsNone(self.error_code(manager))

    def test_deliberate_disconnect_clears_the_login_error(self):
        manager, token = self.rejected_login()
        browser, _ = manager.session(token)
        manager.disconnect(browser)
        self.assertIsNone(self.error_code(manager))


class ReadOnlyAfterLoginProblemTests(Phase4Base):
    """AUTH-02: while Google sign-in needs renewing, saved mail, history and
    actions stay readable; every write (feedback, retries, sync) is refused.
    A deliberate disconnect still hides the account's mail."""

    def client_for(self, stack):
        app = create_app(settings=self.settings, model_factory=Mock(return_value=Mock(model_loaded=False)),
                         vector_factory=Mock(return_value=EmptyCollection()))
        client = stack.enter_context(TestClient(app, base_url='http://localhost'))
        client.headers['Origin'] = ORIGIN
        client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
        manager = app.state.accounts
        context, _ = manager.session(client.cookies.get('mailmind_session'))
        manager.finish_auth(manager.begin_auth(context), (A, '{}'))
        log_email_to_db('saved-1', 'Sender', 'Saved subject', 'Saved body',
                        Prediction(category='UPDATES', outcome='CLASSIFIED'), Prediction(outcome='UNAVAILABLE'),
                        account_id=A, db_path=self.settings.db_path)
        return client, manager

    def reject_login(self, manager):
        context = manager.worker_context()
        with manager.transaction() as conn:
            conn.execute("""INSERT INTO worker_health(account_id,last_error_at,last_error_code)
                VALUES (?,?,'gmail_unavailable') ON CONFLICT(account_id) DO UPDATE SET
                last_error_at=excluded.last_error_at,last_error_code=excluded.last_error_code""",
                         (A, '2026-10-06T00:00:00Z'))
        manager.pause(context)

    def test_saved_mail_is_readable_but_writes_are_refused(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            client, manager = self.client_for(stack)
            self.reject_login(manager)
            session = client.get('/session').json()
            self.assertEqual((session['connected'], session['read_only']), (False, True))
            emails = client.get('/emails')
            self.assertEqual(emails.status_code, 200)
            self.assertEqual([row['id'] for row in emails.json()['emails']], ['saved-1'])
            self.assertEqual(client.get('/emails?email_id=saved-1').status_code, 200)
            self.assertEqual(client.get('/emails/saved-1/history').status_code, 200)
            self.assertEqual(client.post('/feedback', json={'email_id': 'saved-1', 'label': 'SPAM'}).status_code, 409)
            self.assertEqual(client.post('/inbox/sync').status_code, 409)

    def test_deliberate_disconnect_still_hides_mail(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            client, manager = self.client_for(stack)
            self.assertEqual(client.post('/disconnect').status_code, 200)
            self.assertFalse(client.get('/session').json()['read_only'])
            self.assertEqual(client.get('/emails').status_code, 409)

    def test_read_only_survives_a_restart(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            client, manager = self.client_for(stack)
            self.reject_login(manager)
            manager.restart()
            client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
            session = client.get('/session').json()
            self.assertEqual((session['connected'], session['read_only']), (False, True))
            self.assertEqual(client.get('/emails').status_code, 200)


class SilentReconnectTests(Phase4Base):
    """AUTH-03: Disconnect and Sign out park the saved Google login for 24 h.
    Connect Google inside that window renews it silently (no Google page)
    when Google still accepts it; after 24 h, or if Google refuses, the
    normal Google sign-in page is used. Switching accounts is never silent."""

    def app_with(self, stack, silent):
        self.browser = Mock(return_value=(A, '{"browser": true}'))
        app = create_app(settings=self.settings, model_factory=Mock(return_value=Mock(model_loaded=False)),
                         vector_factory=Mock(return_value=EmptyCollection()),
                         oauth_factory=self.browser, silent_login=silent)
        client = stack.enter_context(TestClient(app, base_url='http://localhost'))
        client.headers['Origin'] = ORIGIN
        client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
        self.app, self.client, self.manager = app, client, app.state.accounts
        self.connect()
        self.browser.reset_mock()
        return client

    def connect(self):
        job = self.client.post('/authenticate').json()['job_id']
        self.app.state.auth_futures[job].result(timeout=5)

    def connected(self):
        with self.manager.transaction() as conn:
            return bool(self.manager.state(conn)['connected'])

    def test_disconnect_parks_the_login_and_reconnect_is_silent(self):
        from contextlib import ExitStack
        silent = Mock(return_value=(A, '{"renewed": true}'))
        with ExitStack() as stack:
            client = self.app_with(stack, silent)
            self.assertEqual(client.post('/disconnect').status_code, 200)
            self.assertFalse(self.manager.credential_path(A).exists())   # nothing in use while disconnected
            self.assertTrue(self.manager.parked_credential_path(A).exists())
            self.connect()
        silent.assert_called_once()
        self.assertEqual(silent.call_args.args[1], A)          # must renew the same account
        self.browser.assert_not_called()
        self.assertTrue(self.connected())
        self.assertEqual(self.manager.credential_path(A).read_text(), '{"renewed": true}')
        self.assertFalse(self.manager.parked_credential_path(A).exists())

    def test_sign_out_also_parks_the_login(self):
        from contextlib import ExitStack
        silent = Mock(return_value=(A, '{}'))
        with ExitStack() as stack:
            client = self.app_with(stack, silent)
            self.assertEqual(client.post('/logout').status_code, 200)
            self.assertTrue(self.manager.parked_credential_path(A).exists())
            client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
            self.connect()
        silent.assert_called_once()
        self.browser.assert_not_called()

    def test_after_24_hours_the_parked_login_is_deleted_and_google_asks(self):
        import os
        from contextlib import ExitStack
        silent = Mock(return_value=(A, '{}'))
        with ExitStack() as stack:
            client = self.app_with(stack, silent)
            client.post('/disconnect')
            parked = self.manager.parked_credential_path(A)
            old = time.time() - 24 * 3600 - 60
            os.utime(parked, (old, old))
            self.connect()
        silent.assert_not_called()
        self.browser.assert_called_once()
        self.assertFalse(parked.exists())

    def test_google_refusing_the_saved_login_falls_back_to_the_sign_in_page(self):
        from contextlib import ExitStack
        silent = Mock(return_value=None)
        with ExitStack() as stack:
            client = self.app_with(stack, silent)
            client.post('/disconnect')
            self.connect()
        silent.assert_called_once()
        self.browser.assert_called_once()
        self.assertTrue(self.connected())

    def test_switching_account_while_connected_always_opens_google(self):
        from contextlib import ExitStack
        silent = Mock(return_value=(A, '{}'))
        with ExitStack() as stack:
            self.app_with(stack, silent)
            self.connect()
        silent.assert_not_called()
        self.browser.assert_called_once()

    def test_deleting_account_data_deletes_the_parked_login(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            client = self.app_with(stack, Mock(return_value=None))
            client.post('/disconnect')
            self.connect()
            self.assertEqual(client.delete('/account-data').status_code, 200)
        self.assertFalse(self.manager.parked_credential_path(A).exists())
        self.assertFalse(self.manager.credential_path(A).exists())

    def test_renewal_checks_google_and_the_account(self):
        import json
        import tempfile
        from pathlib import Path
        from google.auth.exceptions import RefreshError
        from src import email_client
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'saved.json'
            path.write_text('{}')
            creds = Mock(valid=False, expired=True, refresh_token='synthetic')
            creds.to_json.return_value = json.dumps({'token': 'new'})
            service = Mock()
            service.users.return_value.getProfile.return_value.execute.return_value = {'emailAddress': A}
            with patch.object(email_client.Credentials, 'from_authorized_user_file', return_value=creds), \
                 patch.object(email_client.GMAIL_CREDENTIAL_CALLS, 'run', side_effect=lambda call, timeout: call()), \
                 patch.object(email_client, 'build_gmail', return_value=service):
                self.assertEqual(email_client.renew_saved_login(path, A, timeout=5), (A, '{"token": "new"}'))
                self.assertIsNone(email_client.renew_saved_login(path, B, timeout=5))   # different account
                creds.refresh.side_effect = RefreshError('revoked')
                self.assertIsNone(email_client.renew_saved_login(path, A, timeout=5))


class FeedbackRuleTests(Phase4Base):
    """FB-01: a correction you made wins for similar later mail. Before, a
    correction reached the AI only as one of >= 3 examples within cosine
    distance 0.25, and the AI could still ignore it. Measured on real data:
    same-sender look-alikes have median distance 0.28 (only 42% <= 0.25); at 0.40 the rule matched the owner's own label 95% of the time,
    while different senders were never closer than 0.32."""

    def neighbor(self, email_id, label, distance):
        return {'email_id': email_id, 'label': label, 'distance': distance, 'text': 'corrected email'}

    def test_same_sender_look_alike_matches_and_others_do_not(self):
        from src.feedback_rules import match_correction
        senders = {'c1': 'Shop <deals@shop.test>', 'c2': 'friend@mail.test'}.get
        same = 'SHOP Offers <Deals@Shop.test>'
        self.assertEqual(match_correction([self.neighbor('c1', 'SPAM', .40)], same, senders)['label'], 'SPAM')
        self.assertIsNone(match_correction([self.neighbor('c1', 'SPAM', .50)], same, senders))         # too different
        self.assertIsNone(match_correction([self.neighbor('c2', 'SPAM', .30)], same, senders))         # other sender, not a near-copy
        self.assertEqual(match_correction([self.neighbor('c2', 'SPAM', .15)], same, senders)['label'], 'SPAM')  # near-copy
        self.assertIsNone(match_correction([{'email_id': 'c1', 'label': 'SPAM'}], same, senders))      # no distance: no rule
        nearest = match_correction([self.neighbor('c1', 'SPAM', .40), self.neighbor('c1b', 'IMPORTANT', .10)], same,
                                   {'c1': 'deals@shop.test', 'c1b': 'deals@shop.test'}.get)
        self.assertEqual(nearest['label'], 'IMPORTANT')                                              # nearest wins

    def classify(self, gemini_category, neighbors, validator):
        import json
        from src import llm_api
        client = Mock()
        client.models.generate_content.return_value.text = json.dumps({
            'category': gemini_category,
            'explanation': {'summary': 'Looks like a newsletter.', 'signals': []},
            'actions': [],
        })
        with patch.object(llm_api, 'get_client', return_value=client), \
             patch.object(llm_api.vector_db, 'search_similar_emails', return_value=neighbors):
            return llm_api.classify_email('Shop <deals@shop.test>', 'Weekly deals', 'New offers inside.',
                                          account_id=A, validator=validator, settings=self.settings)

    def corrections(self, senders):
        validator = Mock(return_value=True)
        validator.sender_of = lambda email_id: senders.get(email_id)
        return validator

    def test_your_correction_overrides_the_ai_for_similar_mail(self):
        from src.intelligence_contract import ExplanationSignal
        result = self.classify('UPDATES', [self.neighbor('c1', 'SPAM', .30)],
                               self.corrections({'c1': 'deals@shop.test'}))
        self.assertEqual((result.outcome, result.category), ('CLASSIFIED', 'SPAM'))
        self.assertEqual(result.analysis.predicted_category, 'SPAM')
        self.assertIn('your earlier correction', result.analysis.explanation_summary.lower())
        self.assertIn(ExplanationSignal.FEEDBACK_PRECEDENT.value, [item.signal for item in result.analysis.signals])

    def test_unverified_examples_can_never_force_a_label(self):
        # Without the SQLite-backed corrections hook, retrieved text is only context.
        result = self.classify('UPDATES', [self.neighbor('c1', 'SPAM', .05)], None)
        self.assertEqual(result.category, 'UPDATES')

    def test_unrelated_mail_keeps_the_ai_decision(self):
        result = self.classify('UPDATES', [self.neighbor('c1', 'SPAM', .60)],
                               self.corrections({'c1': 'deals@shop.test'}))
        self.assertEqual(result.category, 'UPDATES')

    def test_worker_validator_finds_the_sender_of_your_corrections(self):
        from src.feedback import CurrentFeedback
        log_email_to_db('fb-1', 'Shop <deals@shop.test>', 'Deals', 'Body',
                        Prediction(category='UPDATES', outcome='CLASSIFIED'), Prediction(outcome='UNAVAILABLE'),
                        account_id=A, db_path=self.settings.db_path)
        lookup = CurrentFeedback(self.settings.db_path, A)
        self.assertEqual(lookup.sender_of('fb-1'), 'Shop <deals@shop.test>')
        self.assertIsNone(lookup.sender_of('missing'))
        self.assertFalse(lookup({'account_id': A, 'email_id': 'fb-1', 'revision_id': 1, 'label': 'SPAM'}))


class LockedFileRetryTests(Phase4Base):
    """RESILIENCE-D: on Windows a rename fails with "Access is denied" while
    another process (antivirus, a reader) briefly holds the target open.
    Saving or parking the Google login retries instead of failing sign-in."""

    def test_saving_the_login_survives_a_briefly_locked_file(self):
        import os
        manager, _ = self.connected_manager()
        context = manager.worker_context()
        real_replace, calls = os.replace, []

        def flaky(source, target):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(5, 'Access is denied')
            return real_replace(source, target)
        with patch('os.replace', side_effect=flaky), patch('time.sleep'):
            manager.write_credentials(context, '{"saved": true}')
        self.assertEqual(manager.credential_path(A).read_text(), '{"saved": true}')
        self.assertEqual(len(calls), 3)

    def test_a_file_that_stays_locked_still_reports_the_error(self):
        manager, _ = self.connected_manager()
        context = manager.worker_context()
        with patch('os.replace', side_effect=PermissionError(5, 'Access is denied')), patch('time.sleep'):
            with self.assertRaises(PermissionError):
                manager.write_credentials(context, '{}')   # a login is never silently lost


class DeadlineTimeZoneTests(unittest.TestCase):
    """TZ-01: "submit by 5 pm" showed as 10:30 pm. The model saw the received
    time in UTC (...Z), never the user's zone, and returned 17:00Z, which the
    browser rightly shows as 22:30 in India (UTC+5:30)."""

    def action(self, due_at, evidence, precision='exact_time'):
        return {'type': 'general_task', 'title': 'Submit the report', 'description': 'Send the report.',
                'due_at': due_at, 'due_precision': precision, 'evidence': evidence, 'confidence': 'high'}

    def due(self, due_at, text='Please submit the report by 5 pm on Friday.', precision='exact_time'):
        from src import llm_api
        return llm_api._parse_actions([self.action(due_at, text, precision)], text)[0].due_at

    def test_the_model_sees_the_received_time_in_the_users_zone(self):
        import json
        from src import llm_api
        wire, _ = llm_api.build_classification_payload('s', 'subject', 'body', [],
                                                       source_timestamp='2026-10-06T11:30:00.000000Z')
        self.assertEqual(json.loads(wire)['email']['received_at'], '2026-10-06T17:00:00+05:30')   # same instant
        self.assertIn('local', llm_api.SYSTEM_INSTRUCTION)
        self.assertIn('same UTC offset as email.received_at', llm_api.SYSTEM_INSTRUCTION)

    def test_a_time_without_a_stated_zone_is_local(self):
        self.assertEqual(self.due('2026-10-09T17:00:00Z'), '2026-10-09T17:00:00+05:30')   # the old bug, repaired
        self.assertEqual(self.due('2026-10-09T17:00:00'), '2026-10-09T17:00:00+05:30')    # no offset at all
        self.assertEqual(self.due('2026-10-09T17:00:00+05:30'), '2026-10-09T17:00:00+05:30')

    def test_a_zone_the_email_states_is_kept(self):
        self.assertEqual(self.due('2026-10-09T17:00:00Z', text='Submit by 17:00 UTC on Friday.'), '2026-10-09T17:00:00Z')
        self.assertEqual(self.due('2026-10-09T17:00:00-04:00', text='Call at 5 pm EDT.'), '2026-10-09T17:00:00-04:00')

    def test_five_pm_is_stored_as_five_pm_local(self):
        from src.action_center import resolve_action_candidate
        from src.prediction import ActionCandidate
        candidate = ActionCandidate(action_type='general_task', title='Submit', description='Send the report.',
                                    due_at=self.due('2026-10-09T17:00:00Z'), due_precision='exact_time',
                                    evidence='Please submit the report by 5 pm on Friday.', confidence='high')
        stored = resolve_action_candidate(candidate, source_created_at='2026-10-06T11:30:00Z')['due_at']
        self.assertEqual(stored, '2026-10-09T11:30:00.000000Z')   # 17:00 in India


class RestartKeepsGoogleTests(Phase4Base):
    """RESILIENCE-A: an API restart (crash or manual) keeps Google connected
    when the saved login still exists, so work resumes on its own. It still
    revokes browser sessions and bumps the generation. Without a saved login,
    or mid-deletion, it stays disconnected as before."""

    def restarted(self, prepare=None):
        manager, _ = self.connected_manager()
        context = manager.worker_context()
        manager.write_credentials(context, '{}')
        if prepare:
            prepare(manager, context)
        manager.restart()
        with manager.transaction() as conn:
            return manager, context, dict(conn.execute('SELECT connected,generation FROM runtime_state').fetchone())

    def test_saved_login_survives_a_restart(self):
        manager, before, state = self.restarted()
        self.assertEqual(state['connected'], 1)
        self.assertEqual(state['generation'], before.generation + 1)
        self.assertIsNotNone(manager.worker_context())

    def test_missing_login_file_means_disconnected(self):
        _, _, state = self.restarted(lambda manager, context: manager.credential_path(context.account_id).unlink())
        self.assertEqual(state['connected'], 0)

    def test_a_restart_during_account_deletion_stays_disconnected(self):
        def deleting(manager, context):
            with manager.transaction() as conn:
                conn.execute('UPDATE runtime_state SET purge_pending=1')
        _, _, state = self.restarted(deleting)
        self.assertEqual(state['connected'], 0)



class SupervisorResilienceTests(unittest.TestCase):
    """RESILIENCE-B: the supervisor restarts every service forever with
    backoff; only a startup crash loop (5 crashes in a row, each within 10 s
    of starting) is "impossible". It then warns, keeps trying for 60 s, and
    stops only if the service never recovers."""

    def test_a_service_that_ran_fine_restarts_quickly_and_resets_the_count(self):
        from scripts.launch import ServiceWatch
        watch = ServiceWatch('worker', now=0)
        watch.exited(now=3, code=1)            # died fast: failure 1
        self.assertEqual((watch.fast_failures, watch.restart_at), (1, 4))
        watch.started(now=4)
        watch.exited(now=100, code=1)          # ran 96 s: not a startup crash
        self.assertEqual((watch.fast_failures, watch.restart_at), (0, 101))
        self.assertFalse(watch.crash_loop)

    def test_five_fast_crashes_in_a_row_is_a_crash_loop(self):
        from scripts.launch import ServiceWatch
        watch, now, delays = ServiceWatch('api', now=0), 0, []
        for _ in range(5):
            now += 2
            watch.exited(now=now, code=3)
            delays.append(watch.restart_at - now)
            now = watch.restart_at
            watch.started(now=now)
        self.assertEqual(delays, [1, 2, 4, 8, 16])
        self.assertTrue(watch.crash_loop)
        self.assertEqual(watch.last_exit_code, 3)
        # Staying up 10 s proves recovery.
        watch.check_stable(now=now + 10)
        self.assertFalse(watch.crash_loop)

    def test_plan_warns_first_then_stops_or_recovers(self):
        from scripts.launch import ServiceWatch, shutdown_plan, SHUTDOWN_GRACE_SECONDS
        watch = ServiceWatch('api', now=0)
        watch.fast_failures = 5
        watch.last_exit_code = 1
        deadline, message = shutdown_plan([watch], now=100, deadline=None)
        self.assertEqual(deadline, 100 + SHUTDOWN_GRACE_SECONDS)
        self.assertIn('api', message)
        self.assertIn('exit code 1', message)
        self.assertEqual(shutdown_plan([watch], now=120, deadline=deadline)[0], deadline)  # countdown continues
        watch.fast_failures = 0
        self.assertEqual(shutdown_plan([watch], now=130, deadline=deadline), (None, None))  # recovered: cancelled

    def run_supervisor(self, exit_codes, *, seconds):
        """Drive supervise() with fake processes and a fake clock."""
        import tempfile
        from pathlib import Path
        from scripts.launch import supervise
        clock = {'now': 0.0}
        codes = iter(exit_codes)
        lines, spawned = [], []

        class Process:
            def __init__(self):
                self.code = next(codes, None)   # None: keeps running

            def poll(self):
                return self.code
        services = {'worker': {'process': Process(), 'command': ['worker'], 'log': Mock()}}

        def spawn(name, command, append=False):
            spawned.append(clock['now'])
            services[name] = {'process': Process(), 'command': command, 'log': Mock()}

        def stop(wait):
            clock['now'] += 1
            return clock['now'] > seconds
        with tempfile.TemporaryDirectory() as folder:
            try:
                supervise(services, spawn, Path(folder) / 'status.json', stop=stop,
                          clock=lambda: clock['now'], say=lines.append)
                stopped = None
            except RuntimeError as error:
                stopped = (str(error), clock['now'])
        return spawned, lines, stopped

    def test_supervisor_restarts_a_crashed_service_and_keeps_it_running(self):
        spawned, lines, stopped = self.run_supervisor([1, 1, None], seconds=120)
        self.assertEqual(len(spawned), 2)
        self.assertIsNone(stopped)
        self.assertFalse(any(line.startswith('WARNING') for line in lines))

    def test_supervisor_warns_first_then_stops_a_hopeless_crash_loop(self):
        spawned, lines, stopped = self.run_supervisor([1] * 50, seconds=600)
        warning = [index for index, line in enumerate(lines) if line.startswith('WARNING')]
        self.assertTrue(warning, 'the user must be warned before the shutdown')
        self.assertIn('Shutting down in 60 s', lines[warning[0]])
        self.assertIsNotNone(stopped)
        self.assertIn('keeps failing right after it starts', stopped[0])
        self.assertGreater(len(spawned), 5)   # it kept retrying during the warning

    def test_a_locked_status_file_never_stops_mailmind(self):
        # RESILIENCE-D: on Windows, os.replace fails with "Access is denied"
        # while the dashboard server or antivirus has the file open. That
        # exception escaped supervise() and shut every service down.
        from scripts import launch
        denied = PermissionError(5, 'Access is denied')
        with patch.object(launch.os, 'replace', side_effect=denied), patch.object(launch.time, 'sleep'):
            spawned, lines, stopped = self.run_supervisor([1, None], seconds=30)
        self.assertIsNone(stopped)                       # still running
        self.assertEqual(len(spawned), 1)                # and still restarting services
        self.assertEqual(sum('status file' in line for line in lines), 1)   # reported once, not every tick

    def test_status_write_retries_a_briefly_locked_file(self):
        import tempfile
        from pathlib import Path
        from scripts import launch
        real_replace = launch.os.replace
        attempts = []

        def flaky(source, target):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(5, 'Access is denied')
            return real_replace(source, target)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'supervisor-status.json'
            watch = launch.ServiceWatch('worker', now=0)
            with patch.object(launch.os, 'replace', side_effect=flaky), patch.object(launch.time, 'sleep'):
                self.assertTrue(launch.write_status(path, [watch], now=0, deadline=None, message=None))
            self.assertEqual(len(attempts), 3)
            self.assertIn('"worker"', path.read_text(encoding='utf-8'))

    def test_unchanged_status_is_not_rewritten(self):
        import tempfile
        from pathlib import Path
        from scripts import launch
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'supervisor-status.json'
            watch = launch.ServiceWatch('worker', now=0)
            with patch.object(launch.os, 'replace', wraps=launch.os.replace) as replace:
                for tick in range(5):
                    launch.write_status(path, [watch], now=tick, deadline=None, message=None)
                self.assertEqual(replace.call_count, 1)
                watch.exited(now=6, code=1)
                launch.write_status(path, [watch], now=6, deadline=None, message=None)
                self.assertEqual(replace.call_count, 2)

    def test_status_file_is_written_atomically_without_secrets(self):
        import json
        import tempfile
        from pathlib import Path
        from scripts.launch import ServiceWatch, write_status
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'supervisor-status.json'
            watch = ServiceWatch('worker', now=0)
            watch.exited(now=2, code=1)
            write_status(path, [watch], now=2, deadline=None, message=None)
            status = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(status['services']['worker']['state'], 'restarting')
        self.assertEqual(status['services']['worker']['retry_in_seconds'], 1)
        self.assertIsNone(status['shutdown_in_seconds'])
        self.assertNotIn('token', json.dumps(status))



class RetryUntilRecoveredTests(Phase4Base):
    """RESILIENCE-C: temporary failures retry forever with capped backoff;
    only provably permanent ones stop."""

    def test_gmail_download_failures_are_split_into_temporary_and_permanent(self):
        from src.email_client import fetch_failure_code

        class Status(Exception):
            def __init__(self, status):
                super().__init__(status)
                self.resp = Mock(status=status)
        self.assertEqual(fetch_failure_code(Status(503)), 'message_fetch_failed')
        self.assertEqual(fetch_failure_code(Status(429)), 'message_fetch_failed')
        self.assertEqual(fetch_failure_code(ConnectionError()), 'message_fetch_failed')
        self.assertEqual(fetch_failure_code(Status(404)), 'message_unavailable')
        self.assertEqual(fetch_failure_code(ValueError('odd')), 'message_unavailable')

    def failure_status(self, code, times):
        from src.work_queue import claim_cycle, record_ingestion_failure
        manager, _ = self.connected_manager()
        context = manager.worker_context()
        token, _job = claim_cycle(manager, context)
        for _ in range(times):
            record_ingestion_failure({'code': code, 'email_id': 'm1'}, manager, context, token)
        with manager.transaction() as conn:
            row = dict(conn.execute('SELECT status,attempt_count,next_retry_at FROM ingestion_failures').fetchone())
            conn.execute('DELETE FROM ingestion_failures')
        return row

    def test_temporary_download_failure_keeps_retrying(self):
        row = self.failure_status('message_fetch_failed', 8)
        self.assertEqual((row['status'], row['attempt_count']), ('retry', 8))
        self.assertLessEqual(row['next_retry_at'] - time.time(), 301)

    def test_permanent_download_failure_is_quarantined(self):
        self.assertEqual(self.failure_status('message_unavailable', 3)['status'], 'dead')
        self.assertEqual(self.failure_status('message_parse_failed', 3)['status'], 'dead')

    def test_search_indexing_retries_after_backoff_instead_of_giving_up(self):
        from src.email_search import reconcile_search_index
        from src.database import utc_timestamp
        manager, _ = self.connected_manager()
        context = manager.worker_context()
        from src.db_utils import log_email_to_db
        stamp = utc_timestamp()
        log_email_to_db('idx', 's', 'subject', 'body', Prediction(category='UPDATES', outcome='CLASSIFIED'),
                        Prediction(outcome='UNAVAILABLE'), account_id=A, db_path=self.settings.db_path)
        with manager.transaction() as conn:
            conn.execute("""INSERT INTO email_search_index(account_id,email_id,indexing_state,attempt_count,updated_at)
                            VALUES (?,?,'failed',7,?) ON CONFLICT(account_id,email_id) DO UPDATE SET
                            indexing_state='failed',attempt_count=7,updated_at=excluded.updated_at""", (A, 'idx', stamp))
        embed = Mock(return_value=[[0.1, 0.2]])
        collection = Mock()
        result = reconcile_search_index(manager, context, lambda: collection, embedding_provider=embed, bounded=False)
        self.assertEqual(result['indexed'], 0)   # still inside its backoff window
        embed.assert_not_called()
        with manager.transaction() as conn:
            conn.execute("UPDATE email_search_index SET updated_at='2020-01-01T00:00:00Z'")
        result = reconcile_search_index(manager, context, lambda: collection, embedding_provider=embed, bounded=False)
        self.assertEqual(result['indexed'], 1)


if __name__ == '__main__':
    unittest.main()

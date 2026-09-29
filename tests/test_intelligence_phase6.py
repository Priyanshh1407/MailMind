'''Phase 6 dashboard intelligence, chart, and privacy feature tests.'''
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from api.app import create_app
from src.action_center import (
    action_summary, create_action, update_action_status,
)
from src.config import Settings
from src.database import initialize_database
from src.db_utils import log_email_to_db
from src.prediction import Prediction
from src.token_usage import aggregate_token_usage, record_token_usage


A = 'phase6-intelligence@example.test'
B = 'phase6-private@example.test'
ORIGIN = 'http://localhost:5173'


class EmptyCollection:
    def delete(self, **_kwargs):
        return None


class IntelligencePhaseSixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix='mailmind-intelligence-phase6-')
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            data_dir=Path(self.temp.name),
            action_extraction_enabled=True,
            token_analytics_visible=True,
            explanations_visible=True,
        )
        initialize_database(self.settings.db_path)

    def seed_email(self, account, email_id, body='Synthetic private body'):
        log_email_to_db(
            email_id, 'Sender <sender@example.test>', 'Synthetic subject',
            body,
            Prediction(
                category='IMPORTANT', outcome='CLASSIFIED',
                source='local', model_version='synthetic-local-v1',
            ),
            Prediction(
                category='IMPORTANT', outcome='CLASSIFIED',
                source='local', model_version='synthetic-local-v1',
            ),
            account_id=account, db_path=self.settings.db_path,
        )

    def action(self, email_id, due_at):
        return create_action(
            A, email_id, action_type='approval_required',
            title='Approve the synthetic plan',
            description='Review and approve the controlled plan.',
            evidence='approve the controlled plan',
            due_at=due_at, due_precision='exact_time', confidence='high',
            extraction_source='system', db_path=self.settings.db_path,
        )

    def test_due_soon_summary_is_distinct_from_overdue_and_closed_actions(self):
        for email_id in ('past', 'soon', 'later', 'closed'):
            self.seed_email(A, email_id)
        self.action('past', '2026-09-27T09:00:00Z')
        self.action('soon', '2026-10-02T09:00:00Z')
        self.action('later', '2026-10-12T09:00:00Z')
        closed = self.action('closed', '2026-10-01T09:00:00Z')
        update_action_status(
            A, closed['action_id'], status='completed',
            expected_revision=0, db_path=self.settings.db_path,
        )

        result = action_summary(
            A, now=datetime(2026, 9, 28, 9, tzinfo=timezone.utc),
            db_path=self.settings.db_path,
        )

        self.assertEqual(result['status_counts']['open'], 3)
        self.assertEqual(result['status_counts']['completed'], 1)
        self.assertEqual(result['overdue'], 1)
        self.assertEqual(result['due_soon'], 1)

    def test_daily_token_series_uses_local_day_and_separates_cloud_from_local(self):
        self.seed_email(A, 'token-owner')
        events = (
            ('before', 'gemini', '2026-09-27T18:29:59Z', 100),
            ('cloud', 'gemini', '2026-09-27T18:30:00Z', 15),
            ('local', 'local', '2026-09-28T18:29:59Z', 3),
            ('after', 'groq', '2026-09-28T18:30:00Z', 200),
        )
        for request_id, provider, created_at, tokens in events:
            record_token_usage(
                account_id=A, request_id=request_id, provider=provider,
                model_version='synthetic-v1',
                operation='classification_analysis',
                input_tokens=tokens, output_tokens=0, total_tokens=tokens,
                count_method='provider_reported', outcome='success',
                created_at=datetime.fromisoformat(
                    created_at.replace('Z', '+00:00')),
                db_path=self.settings.db_path,
            )

        result = aggregate_token_usage(
            A, window='day', timezone_name='Asia/Kolkata',
            now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc),
            db_path=self.settings.db_path,
        )

        self.assertEqual(result['start_at'], '2026-09-27T18:30:00.000000Z')
        self.assertEqual(result['end_at'], '2026-09-28T18:30:00.000000Z')
        self.assertEqual(len(result['daily']), 1)
        day = result['daily'][0]
        self.assertEqual(day['date'], '2026-09-28')
        self.assertEqual(day['event_count'], 2)
        self.assertEqual(day['total_tokens'], 18)
        self.assertEqual(day['provider_billed_tokens'], 15)
        self.assertEqual(day['local_processed_tokens'], 3)

    def test_source_email_requires_connection_and_never_crosses_accounts(self):
        private_text = 'PRIVATE_OTHER_ACCOUNT_CONTENT_6B2D'
        self.seed_email(A, 'owned-source', 'Owned synthetic body')
        self.seed_email(B, 'private-source', private_text)
        model = Mock(model_loaded=False, load_reason='missing_checkpoint')
        application = create_app(
            settings=self.settings,
            model_factory=Mock(return_value=model),
            vector_factory=Mock(return_value=EmptyCollection()),
            search_vector_factory=Mock(return_value=EmptyCollection()),
        )
        with TestClient(application, base_url='http://localhost') as client:
            client.headers['Origin'] = ORIGIN
            csrf = client.post('/session').json()['csrf_token']
            client.headers['X-CSRF-Token'] = csrf

            disconnected = client.get(
                '/emails', params={'email_id': 'owned-source'})
            self.assertEqual(disconnected.status_code, 409)

            manager = application.state.accounts
            context, _ = manager.session(
                client.cookies.get('mailmind_session'))
            manager.finish_auth(manager.begin_auth(context), (A, '{}'))

            owned = client.get(
                '/emails', params={'email_id': 'owned-source'})
            blocked = client.get(
                '/emails', params={'email_id': 'private-source'})

        self.assertEqual(owned.status_code, 200)
        self.assertEqual(owned.json()['total'], 1)
        self.assertEqual(owned.json()['emails'][0]['id'], 'owned-source')
        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(blocked.json()['total'], 0)
        self.assertEqual(blocked.json()['emails'], [])
        self.assertNotIn(private_text, json.dumps(blocked.json()))


if __name__ == '__main__':
    unittest.main()

"""The dashboard's "AI limit reached" alert, worked out from the worker's records."""
import os
import tempfile
import unittest
from datetime import datetime, timezone

from src.database import connection, initialize_database
from src.provider_status import ai_status

A, B = 'owner@example.test', 'other@example.test'
NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc).timestamp()


def stamp(seconds_ago):
    return datetime.fromtimestamp(NOW - seconds_ago, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


class AiStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = os.path.join(directory.name, 'mail.db')
        initialize_database(self.db)
        with connection(self.db) as conn:
            for account in (A, B):
                conn.execute('INSERT INTO accounts(account_id) VALUES (?) ON CONFLICT DO NOTHING', (account,))
        self.emails = 0

    def email(self, account=A):
        self.emails += 1
        email_id = f'mail-{self.emails}'
        with connection(self.db) as conn:
            conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,message_type,processing_state,created_at)
                            VALUES (?,?,'S <s@x.example>','Subject','Body','synthetic','pending',?)""",
                         (account, email_id, stamp(3600)))
        return email_id

    def waiting(self, code, retry_in=60, account=A):
        email_id = self.email(account)
        with connection(self.db) as conn:
            conn.execute("""INSERT INTO processing_tasks(account_id,email_id,status,stage,attempt_count,next_retry_at,error_code,created_at,updated_at)
                            VALUES (?,?,'retry','classify',1,?,?,?,?)""",
                         (account, email_id, NOW + retry_in, code, stamp(60), stamp(60)))
        return email_id

    def attempts(self, *rows, account=A):
        email_id = self.email(account)
        with connection(self.db) as conn:
            for outcome, code, seconds_ago in rows:
                conn.execute('INSERT INTO processing_attempts(account_id,email_id,stage,outcome,error_code,created_at) VALUES (?,?,?,?,?,?)',
                             (account, email_id, 'classify', outcome, code, stamp(seconds_ago)))

    def status(self, account=A):
        with connection(self.db) as conn:
            return ai_status(conn, account, now=NOW)

    def test_nothing_waiting_is_ok(self):
        self.attempts(('CLASSIFIED', None, 30))
        self.assertEqual(self.status(), {'state': 'ok', 'reason': None, 'waiting': 0, 'retry_in_seconds': None})

    def test_consecutive_quota_failures_report_the_limit_and_the_next_try(self):
        self.waiting('provider_quota', retry_in=109)
        self.waiting('provider_quota', retry_in=300)
        self.attempts(('CLASSIFIED', None, 600), ('ERROR', 'provider_quota', 90), ('ERROR', 'provider_quota', 60),
                      ('ERROR', 'provider_quota', 30))
        self.assertEqual(self.status(), {'state': 'limited', 'reason': 'quota', 'waiting': 2, 'retry_in_seconds': 109})

    def test_overloaded_providers_are_reported_as_busy(self):
        self.waiting('provider_timeout')
        self.waiting('provider_transient')
        self.attempts(('ERROR', 'provider_timeout', 90), ('ERROR', 'provider_transient', 60), ('ERROR', 'provider_timeout', 30))
        self.assertEqual(self.status()['reason'], 'busy')

    def test_one_failure_or_a_success_after_failures_shows_no_alert(self):
        self.waiting('provider_quota')
        self.attempts(('ERROR', 'provider_quota', 30))
        self.assertEqual(self.status()['state'], 'ok')
        self.attempts(('ERROR', 'provider_quota', 25), ('ERROR', 'provider_quota', 20), ('CLASSIFIED', None, 10))
        self.assertEqual(self.status(), {'state': 'ok', 'reason': None, 'waiting': 1, 'retry_in_seconds': None})

    def test_old_failures_and_other_failure_kinds_show_no_alert(self):
        self.waiting('provider_quota')
        self.attempts(('ERROR', 'provider_quota', 3000), ('ERROR', 'provider_quota', 2900), ('ERROR', 'provider_quota', 2800))
        self.assertEqual(self.status()['state'], 'ok')  # nothing tried for 45 minutes: not "right now"
        self.attempts(('ERROR', 'provider_invalid_request', 30), ('ERROR', 'provider_invalid_request', 20),
                      ('ERROR', 'provider_invalid_request', 10))
        self.assertEqual(self.status()['state'], 'ok')  # a rejected request is not a capacity limit

    def test_another_account_never_shows(self):
        self.waiting('provider_quota', account=B)
        self.attempts(('ERROR', 'provider_quota', 30), ('ERROR', 'provider_quota', 20), ('ERROR', 'provider_quota', 10), account=B)
        self.assertEqual(self.status(A)['state'], 'ok')
        self.assertEqual(self.status(B)['state'], 'limited')


if __name__ == '__main__':
    unittest.main()

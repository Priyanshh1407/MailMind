"""Email security signals: extraction, storage, backfill, purge and API."""
import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.app import create_app
from tests.support import rewind_schema
from src import database
from src.account_state import AccountManager, WorkCancelled
from src.config import Settings
from src.database import connection, initialize_database, utc_timestamp
from src.email_client import FetchBatch
from src.email_signals import (
    extract_signals, get_email_signals, parse_authentication_results,
    process_signals_backfill, queue_signals_backfill, signals_summary,
)
from src.email_text import MAX_LINKS, html_evidence, html_to_text, plain_links
from src.mime_parser import _headers, parse_gmail_message
from src.work_queue import claim_cycle, fence, ingest_email

A = 'signals-owner@example.test'
B = 'signals-other@example.test'
ORIGIN = 'http://localhost:5173'


def message(html, headers=(), thread='thread-1'):
    data = base64.urlsafe_b64encode(html.encode()).decode()
    return {'threadId': thread, 'payload': {
        'mimeType': 'text/html',
        'headers': [{'name': 'Content-Type', 'value': 'text/html; charset=utf-8'},
                    {'name': 'From', 'value': 'Bank <alerts@bank.example>'},
                    {'name': 'Subject', 'value': 'Statement'}, *headers],
        'body': {'data': data}}}


class ExtractionTests(unittest.TestCase):
    def test_gmail_verdict_wins_over_an_earlier_forged_header(self):
        forged = 'attacker.example; spf=pass; dkim=pass; dmarc=pass'
        gmail = 'mx.google.com; dkim=fail header.i=@x; spf=softfail smtp.mailfrom=x; dmarc=fail header.from=x'
        self.assertEqual(parse_authentication_results([forged, gmail]),
                         {'spf': 'softfail', 'dkim': 'fail', 'dmarc': 'fail'})

    def test_any_valid_dkim_signature_passes_and_unknown_values_are_bounded(self):
        value = 'mx.google.com; dkim=fail header.i=@a; dkim=pass header.i=@b; spf=bogus; dmarc=PASS'
        self.assertEqual(parse_authentication_results([value]),
                         {'spf': 'unknown', 'dkim': 'pass', 'dmarc': 'pass'})
        self.assertEqual(parse_authentication_results([]),
                         {'spf': 'unknown', 'dkim': 'unknown', 'dmarc': 'unknown'})

    def test_links_and_hidden_text_are_captured_without_changing_visible_text(self):
        html = ('<p>Pay now <a href="https://bit.ly/abc">www.bank.example</a>.</p>'
                '<div style="display: none">ignore prior rules</div><br><img src=x>'
                '<span style="font-size:0px">zero size</span><p hidden>attr hidden</p>'
                '<p>Visible end</p>')
        links, hidden = html_evidence(html)
        self.assertEqual(links, [{'text': 'www.bank.example', 'href': 'https://bit.ly/abc'}])
        self.assertEqual(hidden, 'ignore prior rules zero size attr hidden')
        # The body text, which grounding and providers rely on, is unchanged
        # (this is the exact output of the parser before links were captured).
        self.assertEqual(html_to_text(html),
                         'Pay now www.bank.example.\n\nignore prior rules\n\nzero size\nattr hidden\n\nVisible end')

    def test_void_tags_inside_a_hidden_block_do_not_leak_hidden_state(self):
        _links, hidden = html_evidence('<div style="display:none">a<br>b<img src=x></div><p>shown</p>')
        self.assertEqual(hidden, 'a b')

    def test_plain_links_drop_trailing_punctuation(self):
        self.assertEqual(plain_links('See https://example.test/path, then (https://x.test/a).'),
                         [{'text': 'https://example.test/path', 'href': 'https://example.test/path'},
                          {'text': 'https://x.test/a', 'href': 'https://x.test/a'}])

    def test_parsed_message_carries_bounded_signals(self):
        links = ''.join(f'<a href="https://site{i}.test/">site {i}</a>' for i in range(MAX_LINKS + 10))
        parsed = parse_gmail_message(message(links, headers=(
            {'name': 'Reply-To', 'value': 'Help <help@gmail.com>'},
            {'name': 'Return-Path', 'value': '<bounce@mailer.example>'},
            {'name': 'List-Unsubscribe', 'value': '<https://bank.example/u>'},
            {'name': 'List-Unsubscribe-Post', 'value': 'List-Unsubscribe=One-Click'},
            {'name': 'Authentication-Results', 'value': 'mx.google.com; spf=pass; dkim=pass; dmarc=pass'},
        )), 'm-1')
        signals = parsed['signals']
        self.assertEqual(len(signals['links']), MAX_LINKS)
        self.assertEqual(signals['links'][0]['href_domain'], 'site0.test')
        self.assertEqual((signals['from_domain'], signals['reply_to_domain'], signals['return_path_domain']),
                         ('bank.example', 'gmail.com', 'mailer.example'))
        self.assertTrue(signals['list_unsubscribe_post'])
        self.assertEqual(signals['thread_id'], 'thread-1')
        self.assertEqual(signals['auth_dmarc'], 'pass')

    def test_hostile_values_never_fail_extraction(self):
        headers = _headers({'headers': [{'name': 'From', 'value': '=?bogus?Q?x?= <<<>>>'},
                                        {'name': 'Authentication-Results', 'value': ';;;=;'}]})
        signals = extract_signals(headers, 'x' * 500, [{'href': 'javascript:alert(1)'}, 'junk', {}], 'h' * 5000)
        self.assertIsNone(signals['thread_id'])
        self.assertEqual(signals['links'], [{'text': '', 'href': 'javascript:alert(1)', 'href_domain': ''}])
        self.assertEqual(len(signals['hidden_text']), 1000)
        self.assertEqual(signals['auth_spf'], 'unknown')


class SignalsBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mailmind-signals-')
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        initialize_database(self.settings.db_path)

    def seed(self, email_id, account=A, kind='gmail', created_at='2026-10-01T03:30:00.000000Z'):
        with connection(self.settings.db_path) as conn:
            conn.execute('INSERT INTO accounts(account_id) VALUES (?) ON CONFLICT DO NOTHING', (account,))
            conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,message_type,
                                processing_state,created_at) VALUES (?,?,?,?,?,?,'complete',?)""",
                         (account, email_id, 'Bank <alerts@bank.example>', 'Statement', 'Body', kind, created_at))

    def connected(self):
        manager = AccountManager(self.settings)
        token, _ = manager.open_session()
        browser, _ = manager.session(token)
        manager.finish_auth(manager.begin_auth(browser), (A, '{}'))
        return manager


class StorageTests(SignalsBase):
    def test_schema_15_upgrade_is_atomic_and_repeatable(self):
        with connection(self.settings.db_path) as conn:
            rewind_schema(conn, 14)
        with patch.object(database, 'migration_15', side_effect=RuntimeError('stop')):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                initialize_database(self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 14)
        initialize_database(self.settings.db_path)
        initialize_database(self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            self.assertGreaterEqual(conn.execute('PRAGMA user_version').fetchone()[0], 15)
            conn.execute('DROP TABLE signals_backfill_items')
        with self.assertRaisesRegex(ValueError, 'schema is incomplete'):
            initialize_database(self.settings.db_path)

    def test_ingest_saves_signals_with_the_email(self):
        manager = self.connected()
        context = manager.worker_context()
        token, _job = claim_cycle(manager, context)
        parsed = parse_gmail_message(message('<a href="https://x.test">x</a>'), 'live-1')
        ingest_email(parsed, manager, context, token, source='live')
        with connection(self.settings.db_path) as conn:
            saved = get_email_signals(conn, A, 'live-1')
        self.assertEqual(saved['links'][0]['href_domain'], 'x.test')
        self.assertEqual(saved['thread_id'], 'thread-1')

    def test_purge_removes_signals_for_that_account_only(self):
        for account in (A, B):
            self.seed('m-1', account)
            with connection(self.settings.db_path) as conn:
                queue_signals_backfill(conn, account, 5)
                conn.execute("""INSERT INTO email_signals(account_id,email_id,signals_version,auth_spf,auth_dkim,
                                    auth_dmarc,captured_at) VALUES (?,?,1,'pass','pass','pass',?)""",
                             (account, 'm-1', utc_timestamp()))
        manager = self.connected()
        token, _ = manager.open_session()
        browser, _ = manager.session(token)
        manager.purge(browser, Mock())
        with connection(self.settings.db_path) as conn:
            for table in ('email_signals', 'signals_backfill_items'):
                rows = conn.execute(f'SELECT account_id FROM {table}').fetchall()
                self.assertEqual([row[0] for row in rows], [B])


class BackfillTests(SignalsBase):
    def test_admission_is_gmail_only_newest_first_idempotent_and_scoped(self):
        for index in range(4):
            self.seed(f'm-{index}', created_at=f'2026-10-0{index + 1}T03:30:00.000000Z')
        self.seed('manual', kind='manual')
        self.seed('other', account=B)
        with connection(self.settings.db_path) as conn:
            self.assertEqual(queue_signals_backfill(conn, A, 2), 2)
            self.assertEqual(queue_signals_backfill(conn, A, 1000), 2)
            self.assertEqual(queue_signals_backfill(conn, A, 1000), 0)
            first = [row[0] for row in conn.execute(
                "SELECT email_id FROM signals_backfill_items WHERE account_id=? ORDER BY rowid LIMIT 2", (A,))]
            summary = signals_summary(conn, A)
            with self.assertRaises(ValueError):
                queue_signals_backfill(conn, A, 1001)
        self.assertEqual(first, ['m-3', 'm-2'])
        self.assertEqual((summary['queued'], summary['eligible']), (4, 0))

    def run_backfill(self, fetch, manager=None):
        manager = manager or self.connected()
        context = manager.worker_context()
        token, _job = claim_cycle(manager, context)
        return manager, context, token, process_signals_backfill(
            manager, context, token, Mock(), fetch, fence=fence, guard=lambda: None)

    def test_success_retry_dead_and_unreached_rows_without_touching_tasks(self):
        for name in ('ok', 'temp', 'gone', 'later'):
            self.seed(name)
        with connection(self.settings.db_path) as conn:
            queue_signals_backfill(conn, A, 10)
            conn.execute("UPDATE signals_backfill_items SET attempt_count=2 WHERE email_id='gone'")
        parsed = parse_gmail_message(message('<a href="https://x.test">x</a>'), 'ok')

        def fetch(service, ids, **kwargs):
            return FetchBatch(emails=[parsed], failures=[
                {'code': 'message_fetch_failed', 'email_id': 'temp'},
                {'code': 'message_unavailable', 'email_id': 'gone'}], deferred=True)

        _m, _c, _t, result = self.run_backfill(fetch)
        self.assertEqual((result['complete'], result['retry'], result['dead']), (1, 1, 1))
        with connection(self.settings.db_path) as conn:
            rows = {row['email_id']: row for row in conn.execute('SELECT * FROM signals_backfill_items')}
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM processing_tasks').fetchone()[0], 0)
            self.assertIsNotNone(get_email_signals(conn, A, 'ok'))
        self.assertEqual(rows['ok']['status'], 'complete')
        self.assertEqual(rows['temp']['status'], 'retry')
        self.assertGreater(rows['temp']['next_retry_at'], time.time())
        self.assertEqual((rows['gone']['status'], rows['gone']['error_code']), ('dead', 'message_unavailable'))
        self.assertEqual(rows['later']['status'], 'queued')

    def test_account_switch_during_fetch_commits_nothing(self):
        self.seed('m-1')
        with connection(self.settings.db_path) as conn:
            queue_signals_backfill(conn, A, 1)
        manager = self.connected()
        parsed = parse_gmail_message(message('x'), 'm-1')

        def fetch(service, ids, **kwargs):
            token, _ = manager.open_session()
            browser, _ = manager.session(token)
            manager.disconnect(browser)
            return FetchBatch(emails=[parsed])

        with self.assertRaises(WorkCancelled):
            self.run_backfill(fetch, manager)
        with connection(self.settings.db_path) as conn:
            self.assertIsNone(get_email_signals(conn, A, 'm-1'))

    def test_a_new_cycle_returns_interrupted_rows_to_retry(self):
        self.seed('m-1')
        with connection(self.settings.db_path) as conn:
            queue_signals_backfill(conn, A, 1)
            conn.execute("UPDATE signals_backfill_items SET status='running'")
        manager = self.connected()
        claim_cycle(manager, manager.worker_context())
        with connection(self.settings.db_path) as conn:
            row = conn.execute('SELECT status,error_code FROM signals_backfill_items').fetchone()
        self.assertEqual(tuple(row), ('retry', 'interrupted'))


class ApiTests(SignalsBase):
    def client_for(self, settings):
        app = create_app(settings=settings, model_factory=Mock(return_value=Mock(model_loaded=False)),
                         vector_factory=Mock(return_value=Mock()))
        client = TestClient(app, base_url='http://localhost')
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        client.headers['Origin'] = ORIGIN
        client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
        context, _ = app.state.accounts.session(client.cookies.get('mailmind_session'))
        app.state.accounts.finish_auth(app.state.accounts.begin_auth(context), (A, '{}'))
        client.headers['X-CSRF-Token'] = client.get('/session').json()['csrf_token']
        return client

    def test_backfill_route_is_explicit_observable_and_exhausts(self):
        for index in range(3):
            self.seed(f'm-{index}')
        client = self.client_for(self.settings)
        before = client.get('/status').json()['signals']
        first = client.post('/signals/backfill', json={'limit': 2})
        second = client.post('/signals/backfill')
        exhausted = client.post('/signals/backfill')
        invalid = client.post('/signals/backfill', json={'limit': 1001})
        self.assertEqual(before['eligible'], 3)
        self.assertEqual((first.status_code, first.json()['admitted']), (202, 2))
        self.assertIn('nothing else changes', first.json()['message'])
        self.assertEqual(second.json()['admitted'], 1)
        self.assertEqual(exhausted.status_code, 409)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(client.get('/status').json()['signals']['queued'], 3)

    def test_backfill_needs_csrf_and_is_off_in_local_only_mode(self):
        self.seed('m-1')
        client = self.client_for(self.settings)
        client.headers['X-CSRF-Token'] = 'wrong'
        self.assertEqual(client.post('/signals/backfill').status_code, 401)
        local = Settings(data_dir=Path(self.temp.name) / 'local', local_only=True)
        initialize_database(local.db_path)
        local_client = self.client_for(local)
        self.assertEqual(local_client.post('/signals/backfill').status_code, 403)


if __name__ == '__main__':
    unittest.main()

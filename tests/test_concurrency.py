"""Reads never wait for writers: the dashboard stays up while the worker holds the database."""
import concurrent.futures
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from api.app import create_app
from src.account_state import AccountManager
from src.config import Settings
from src.database import connection, initialize_database

A = 'concurrency-owner@example.test'
ORIGIN = 'http://localhost:5173'


class ReadGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mailmind-concurrency-')
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name), ledger_enabled=True, risk_analysis_enabled=True,
                                 action_extraction_enabled=True, token_analytics_visible=True)
        initialize_database(self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            conn.execute('INSERT INTO accounts(account_id) VALUES (?)', (A,))
            for index in range(30):
                conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,message_type,processing_state,created_at)
                                VALUES (?,?,'S <s@x.example>','Subject','Body','gmail','complete','2026-10-01T00:00:00.000000Z')""",
                             (A, f'm-{index}'))
        app = create_app(settings=self.settings, model_factory=Mock(return_value=Mock(model_loaded=False)),
                         vector_factory=Mock(return_value=Mock()), search_vector_factory=Mock(side_effect=RuntimeError('none')))
        self.client = TestClient(app, base_url='http://localhost')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.headers['Origin'] = ORIGIN
        self.client.headers['X-CSRF-Token'] = self.client.post('/session').json()['csrf_token']
        context, _ = app.state.accounts.session(self.client.cookies.get('mailmind_session'))
        app.state.accounts.finish_auth(app.state.accounts.begin_auth(context), (A, '{}'))
        self.client.headers['X-CSRF-Token'] = self.client.get('/session').json()['csrf_token']
        self.manager = app.state.accounts

    def test_reads_succeed_while_another_process_holds_the_write_lock(self):
        # Found by load testing: every read took the write lock, so 20 parallel
        # searches queued past SQLite's busy timeout and failed with 503.
        held, release = threading.Event(), threading.Event()

        def writer():
            conn = sqlite3.connect(self.settings.db_path, timeout=10)
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE email_logs SET subject='Changed' WHERE email_id='m-0'")
            held.set()
            release.wait(10)
            conn.rollback()
            conn.close()
        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(held.wait(5))
        try:
            paths = ['/session', '/status', '/telemetry', '/emails?limit=20', '/emails?limit=20&search=Subject',
                     '/actions/summary', '/analytics/tokens?window=day', '/ledger/summary', '/emails/m-1/risk'] * 4
            started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(12) as pool:
                codes = list(pool.map(lambda path: self.client.get(path).status_code, paths))
            elapsed = time.monotonic() - started
        finally:
            release.set()
            thread.join()
        self.assertEqual(set(codes) - {200}, set(), codes)
        self.assertLess(elapsed, 4)  # nothing waited out the 5 s busy timeout

    def test_a_write_inside_a_read_only_guard_fails_loudly(self):
        context = self.manager.worker_context()
        with self.assertRaises(sqlite3.OperationalError):
            with self.manager.guard(context, readonly=True) as conn:
                conn.execute("UPDATE email_logs SET subject='x'")
        with self.manager.guard(context, readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM email_logs').fetchone()[0], 30)

    def test_writes_still_take_the_lock_and_wait_their_turn(self):
        held, release = threading.Event(), threading.Event()

        def writer():
            conn = sqlite3.connect(self.settings.db_path, timeout=10)
            conn.execute('BEGIN IMMEDIATE')
            held.set()
            release.wait(10)
            conn.commit()
            conn.close()
        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(held.wait(5))
        threading.Timer(1, release.set).start()
        started = time.monotonic()
        response = self.client.post('/feedback', json={'email_id': 'm-1', 'label': 'IMPORTANT'})
        thread.join()
        self.assertEqual(response.status_code, 202)
        self.assertGreaterEqual(time.monotonic() - started, 0.9)  # it waited for the writer, then succeeded


if __name__ == '__main__':
    unittest.main()

"""Money ledger: local amounts, placeholder privacy, cloud answers, scanning, storage and API."""
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.app import create_app
from src import database, ledger_store, llm_api
from src.account_state import AccountManager, WorkCancelled
from src.config import Settings
from src.database import connection, initialize_database, utc_timestamp
from src.intelligence_safety import claim_mutation_slot
from src.ledger import (detect_subscriptions, extract_by_rules, find_amounts, is_transaction, masked_text,
                        provider_contents, resolve_cloud_answer, total_by_rules)
from src.ledger_store import LEDGER_CLOUD_INTERVAL, ledger_summary, scan_ledger, subscriptions
from src.llm_api import StructuredUnavailable, call_structured, gemini_request_config
from src.token_usage import record_token_usage
from src.work_queue import claim_cycle, fence
from scripts import evaluate_ledger
from tests.support import rewind_schema

A = 'ledger-owner@example.test'
B = 'ledger-other@example.test'
ORIGIN = 'http://localhost:5173'
RECEIPT = 'Item total: Rs 280\nDelivery fee: Rs 35\nDiscount: -Rs 50\nOrder total: Rs 265\nYou saved Rs 50.'


class AmountTests(unittest.TestCase):
    def test_amounts_in_indian_and_foreign_formats(self):
        found = find_amounts('Paid ₹1,23,456.78, then Rs.250 and INR 99, $12.34 USD and 500 rupees; order 4521.')
        self.assertEqual([(item.minor, item.currency) for item in found],
                         [(12345678, 'INR'), (25000, 'INR'), (9900, 'INR'), (1234, 'USD'), (50000, 'INR')])

    def test_the_total_cue_beats_items_subtotals_savings_and_balances(self):
        text = RECEIPT
        self.assertEqual(total_by_rules(text, find_amounts(text)).minor, 26500)
        debit = 'Rs.2,500.00 has been debited from your account. Available balance: Rs.45,230.00.'
        self.assertEqual(total_by_rules(debit, find_amounts(debit)).minor, 250000)
        shop = 'MRP: ₹2,499\nGrand Total: ₹1,299\nYou saved ₹1,200.'
        self.assertEqual(total_by_rules(shop, find_amounts(shop)).minor, 129900)

    def test_adverts_with_prices_are_not_transactions(self):
        self.assertTrue(is_transaction('Order delivered', RECEIPT))
        self.assertFalse(is_transaction('Big sale', 'Laptops starting at ₹24,990. Up to 80% off. Shop now.'))
        self.assertFalse(is_transaction('Hello', 'No money mentioned here.'))

    def test_rules_extract_kind_merchant_and_renewal(self):
        entry = extract_by_rules('Netflix <info@account.netflix.com>', 'Your membership',
                                 'We charged ₹649 for your monthly plan. Your membership renews on 05 Nov 2026.',
                                 date(2026, 10, 5))
        self.assertEqual((entry['kind'], entry['amount_minor'], entry['merchant'], entry['renews_on'], entry['period']),
                         ('subscription', 64900, 'Netflix', '2026-11-05', 'monthly'))
        refund = extract_by_rules('Amazon.in <a@amazon.in>', 'Refund', 'A refund of ₹499 was initiated.', date(2026, 9, 1))
        self.assertEqual((refund['kind'], refund['merchant']), ('refund', 'Amazon'))


class PrivacyTests(unittest.TestCase):
    def test_amounts_become_numbered_placeholders(self):
        text, amounts = masked_text('Order delivered', RECEIPT)
        self.assertNotIn('265', text)
        self.assertIn('Order total: [AMOUNT_4]', text)
        self.assertEqual([item.key for item in amounts], ['AMOUNT_1', 'AMOUNT_2', 'AMOUNT_3', 'AMOUNT_4', 'AMOUNT_5'])

    def test_no_benchmark_request_contains_an_amount_and_a_planted_leak_is_caught(self):
        rows = evaluate_ledger.load()
        for row in rows:
            contents, _masked, _amounts = provider_contents(row['sender'], row['subject'], row['body'],
                                                            date.fromisoformat(row['received']))
            evaluate_ledger.assert_no_amounts(contents, row)
            self.assertNotIn(row['sender'].split('<')[-1].rstrip('>'), contents)
        row = next(item for item in rows if item['id'] == 'food-0')
        contents, _m, _a = provider_contents(row['sender'], row['subject'], row['body'], date(2026, 7, 1))
        with self.assertRaises(AssertionError):
            evaluate_ledger.assert_no_amounts(contents.replace('[AMOUNT_4]', 'Rs 265'), row)


class CloudAnswerTests(unittest.TestCase):
    def answer(self, **overrides):
        value = {'kind': 'purchase', 'total_ref': 'AMOUNT_4', 'merchant': 'Swiggy', 'renews_on': None,
                 'period': 'none', 'evidence': 'Order total: [AMOUNT_4]'}
        value.update(overrides)
        return value

    def resolve(self, answer, body=RECEIPT, sender='Swiggy <noreply@swiggy.in>'):
        _contents, masked, amounts = provider_contents(sender, 'Your Swiggy order', body, date(2026, 9, 1))
        return resolve_cloud_answer(answer, sender=sender, masked=masked, amounts=amounts,
                                    received=date(2026, 9, 1), source='gemini')

    def test_the_real_amount_is_filled_in_locally(self):
        entry = self.resolve(self.answer())
        self.assertEqual((entry['amount_minor'], entry['currency'], entry['merchant']), (26500, 'INR', 'Swiggy'))
        self.assertEqual(entry['evidence'], 'Order total: Rs 265')

    def test_invalid_answers_are_rejected_or_contained(self):
        with self.assertRaises(ValueError):
            self.resolve(self.answer(total_ref='AMOUNT_9'))
        with self.assertRaises(ValueError):
            self.resolve({'kind': 'purchase'})
        self.assertEqual(self.resolve(self.answer(kind='none')), {'kind': 'none'})
        invented = self.resolve(self.answer(merchant='Evil Corp', evidence='Pay Evil Corp now'))
        self.assertEqual((invented['merchant'], invented['evidence']), ('Swiggy', ''))
        late = self.resolve(self.answer(kind='subscription', renews_on='2031-01-01', period='monthly'))
        self.assertIsNone(late['renews_on'])


class SubscriptionTests(unittest.TestCase):
    def entry(self, merchant, day, amount=64900, kind='purchase'):
        return {'merchant': merchant, 'currency': 'INR', 'kind': kind, 'amount_minor': amount,
                'occurred_at': day + 'T09:00:00.000000Z', 'renews_on': None, 'period': None, 'email_id': merchant + day}

    def test_monthly_and_yearly_series_but_not_irregular_repeats(self):
        entries = [self.entry('Netflix', '2026-07-05'), self.entry('Netflix', '2026-08-04'), self.entry('Netflix', '2026-09-03'),
                   self.entry('Prime', '2025-09-10', 149900), self.entry('Prime', '2026-09-10', 149900),
                   self.entry('Amazon', '2026-07-01', 39900), self.entry('Amazon', '2026-07-10', 49900),
                   self.entry('Amazon', '2026-08-26', 59900)]
        found = {item['merchant']: item for item in detect_subscriptions(entries, date(2026, 9, 20))}
        self.assertEqual(set(found), {'Netflix', 'Prime'})
        self.assertEqual((found['Netflix']['period'], found['Netflix']['renews_on']), ('monthly', '2026-10-03'))
        self.assertEqual(found['Prime']['monthly_minor'], round(149900 / 12))


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mailmind-ledger-')
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name), ledger_enabled=True)
        initialize_database(self.settings.db_path)
        ledger_store._LAST_CLOUD_CALL.clear()

    def seed(self, email_id, body=RECEIPT, account=A, prediction='UPDATES', sender='Swiggy <noreply@swiggy.in>',
             created_at='2026-09-01T08:00:00.000000Z', subject='Your Swiggy order'):
        with connection(self.settings.db_path) as conn:
            conn.execute('INSERT INTO accounts(account_id) VALUES (?) ON CONFLICT DO NOTHING', (account,))
            conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,prediction,message_type,
                                processing_state,created_at) VALUES (?,?,?,?,?,?,'gmail','complete',?)""",
                         (account, email_id, sender, subject, body, prediction, created_at))

    def connected(self, settings=None):
        manager = AccountManager(settings or self.settings)
        token, _ = manager.open_session()
        browser, _ = manager.session(token)
        manager.finish_auth(manager.begin_auth(browser), (A, '{}'))
        return manager

    def scan(self, extract, manager=None, clock=None):
        manager = manager or self.connected()
        context = manager.worker_context()
        token, _job = claim_cycle(manager, context)
        # One clock for the whole test: each call is past the cloud interval.
        if not hasattr(self, 'ticks'):
            self.ticks = iter(range(1000, 10 ** 6, LEDGER_CLOUD_INTERVAL + 1))
        clock = clock or self.ticks.__next__
        return scan_ledger(manager, context, token, fence=fence, guard=lambda: None, extract=extract,
                           monotonic=clock)

    def entries(self):
        with connection(self.settings.db_path) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM ledger_entries ORDER BY email_id')]


class SchemaTests(LedgerBase):
    def test_schema_17_keeps_token_rows_and_accepts_the_new_values(self):
        self.seed('m-1')
        record_token_usage(account_id=A, request_id='before-17', provider='gemini', model_version='m',
                           operation='classification_analysis', count_method='estimated', outcome='success',
                           total_tokens=10, db_path=self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            rewind_schema(conn, 16)
        with patch.object(database, 'migration_17', side_effect=RuntimeError('stop')):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                initialize_database(self.settings.db_path)
        initialize_database(self.settings.db_path)
        initialize_database(self.settings.db_path)
        record_token_usage(account_id=A, request_id='after-17', provider='groq', model_version='m',
                           operation='ledger_extraction', count_method='estimated', outcome='success',
                           total_tokens=5, db_path=self.settings.db_path)
        claim_mutation_slot(A, 'question', 'q', cooldown_seconds=5, db_path=self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM token_usage_events').fetchone()[0], 2)
            indexes = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='token_usage_events'")}
            self.assertTrue({'token_usage_account_time', 'token_usage_operation_time'} <= indexes)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])


class ScanTests(LedgerBase):
    def test_local_only_uses_rules_and_skips_spam_and_adverts(self):
        self.seed('receipt')
        self.seed('spam', prediction='SPAM')
        self.seed('advert', body='Laptops starting at ₹24,990. Up to 80% off. Shop now.')
        counts = self.scan(extract=None)
        rows = self.entries()
        self.assertEqual([(row['email_id'], row['amount_minor'], row['source']) for row in rows],
                         [('receipt', 26500, 'local')])
        self.assertEqual(counts['not_transaction'], 1)
        self.assertEqual(self.scan(extract=None)['scanned'], 0)

    def test_cloud_answer_is_used_and_metered_once_per_cycle(self):
        self.seed('a', created_at='2026-09-02T08:00:00.000000Z')
        self.seed('b', created_at='2026-09-01T08:00:00.000000Z')
        calls = []
        def extract(contents, recorder, guard):
            calls.append(contents)
            self.assertNotIn('265', contents)
            return {'kind': 'purchase', 'total_ref': 'AMOUNT_4', 'merchant': 'Swiggy', 'renews_on': None,
                    'period': 'none', 'evidence': 'Order total: [AMOUNT_4]'}, 'gemini'
        self.scan(extract, clock=lambda: 5000)
        self.assertEqual(len(calls), 1)
        self.assertEqual([(row['email_id'], row['source']) for row in self.entries()], [('a', 'gemini')])

    def test_temporary_failures_retry_then_rules_decide_and_permanent_ones_fall_back_at_once(self):
        self.seed('m-1')
        def temporary(contents, recorder, guard):
            raise StructuredUnavailable('provider_quota', True)
        self.scan(temporary)
        with connection(self.settings.db_path) as conn:
            scan = conn.execute('SELECT status,attempt_count,error_code FROM ledger_scans').fetchone()
            self.assertEqual(tuple(scan), ('retry', 1, 'provider_quota'))
            conn.execute('UPDATE ledger_scans SET next_retry_at=0,attempt_count=2')
        self.scan(temporary)
        self.assertEqual([(row['amount_minor'], row['source']) for row in self.entries()], [(26500, 'local')])
        self.seed('m-2', created_at='2026-09-03T08:00:00.000000Z')
        self.scan(lambda *args: (_ for _ in ()).throw(StructuredUnavailable('provider_auth', False)))
        self.assertEqual(len(self.entries()), 2)

    def test_corrections_survive_a_rescan(self):
        self.seed('m-1')
        self.scan(extract=None)
        with connection(self.settings.db_path) as conn:
            conn.execute("UPDATE ledger_entries SET amount_minor=99900,user_corrected=1")
            conn.execute("UPDATE ledger_scans SET extraction_version='ledger-v0'")
        self.scan(extract=None)
        self.assertEqual(self.entries()[0]['amount_minor'], 99900)

    def test_account_switch_during_extraction_commits_nothing(self):
        self.seed('m-1')
        manager = self.connected()
        def extract(contents, recorder, guard):
            token, _ = manager.open_session()
            browser, _ = manager.session(token)
            manager.disconnect(browser)
            return {'kind': 'purchase', 'total_ref': 'AMOUNT_4', 'merchant': 'Swiggy', 'renews_on': None,
                    'period': 'none', 'evidence': ''}, 'gemini'
        with self.assertRaises(WorkCancelled):
            self.scan(extract, manager)
        self.assertEqual(self.entries(), [])


class StructuredCallTests(unittest.TestCase):
    def test_classification_requests_are_unchanged(self):
        # The recorded evaluation replays only if these bytes never change;
        # the assistant features must use their own request builders.
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]
        self.assertEqual(digest(gemini_request_config('gemini-3.5-flash-lite')), 'd52f59df1152c23a')
        self.assertEqual(digest(llm_api.groq_request_body('openai/gpt-oss-20b', 'x')), 'e02f661c47c7213e')

    def test_invalid_gemini_answer_fails_over_to_groq_and_local_only_never_calls(self):
        settings = Settings(gemini_models=('gemini-3.5-flash-lite',))
        client = Mock()
        client.models.generate_content.return_value = Mock(text='{"kind": 7}', usage_metadata=None)
        groq = Mock()
        groq.json.return_value = {'choices': [{'message': {'content': '{"kind": "ok"}'}}]}
        def validate(value):
            if value.get('kind') != 'ok':
                raise ValueError('bad')
            return value
        with patch.object(llm_api, 'get_client', return_value=client), patch('requests.post', return_value=groq), \
                patch.dict(os.environ, {'GROQ_API_KEY': 'synthetic'}):
            value, source, _model = call_structured('{}', system='s', schema={}, schema_name='t', settings=settings,
                                                    operation='ledger_extraction', validate=validate)
        self.assertEqual((value, source), ({'kind': 'ok'}, 'groq'))
        with self.assertRaises(StructuredUnavailable) as caught:
            call_structured('{}', system='s', schema={}, schema_name='t', settings=Settings(local_only=True),
                            operation='ledger_extraction', validate=validate)
        self.assertEqual(caught.exception.code, 'cloud_disabled_local_only')


class ApiTests(LedgerBase):
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

    def test_summary_entries_correction_and_subscriptions(self):
        self.seed('m-1')
        self.seed('other', account=B)
        self.scan(extract=None)
        client = self.client_for(self.settings)
        summary = client.get('/ledger/summary?month=2026-09').json()
        entries = client.get('/ledger/entries?month=2026-09').json()
        entry = entries['entries'][0]
        corrected = client.patch(f'/ledger/entries/{entry["entry_id"]}', json={'expected_revision': entry['revision'], 'amount_minor': 30000})
        stale = client.patch(f'/ledger/entries/{entry["entry_id"]}', json={'expected_revision': entry['revision'], 'merchant': 'X'})
        empty = client.patch(f'/ledger/entries/{entry["entry_id"]}', json={'expected_revision': 1})
        self.assertEqual(summary['totals']['INR']['spent_minor'], 26500)
        self.assertEqual(summary['merchants'][0]['merchant'], 'Swiggy')
        self.assertEqual(len(summary['trend']), 12)
        self.assertEqual((entries['total'], entry['amount_minor']), (1, 26500))
        self.assertEqual(corrected.status_code, 200)
        self.assertTrue(corrected.json()['entry']['user_corrected'])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(client.get('/ledger/summary?month=2026-09').json()['totals']['INR']['spent_minor'], 30000)
        self.assertEqual(client.get('/ledger/subscriptions').json()['subscriptions'], [])
        self.assertEqual(client.get('/ledger/summary?month=2026-13').status_code, 422)
        self.assertTrue(client.get('/status').json()['ledger']['enabled'])

    def test_switched_off_and_purged(self):
        self.seed('m-1')
        self.scan(extract=None)
        client = self.client_for(Settings(data_dir=self.settings.data_dir))
        self.assertEqual(client.get('/ledger/summary').status_code, 404)
        manager = self.connected()
        token, _ = manager.open_session()
        browser, _ = manager.session(token)
        manager.purge(browser, Mock())
        self.assertEqual(self.entries(), [])
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ledger_scans').fetchone()[0], 0)


class ReproducibilityTests(unittest.TestCase):
    def test_report_replays_byte_for_byte(self):
        committed = evaluate_ledger.REPORT.read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as folder, patch.object(evaluate_ledger, 'REPORT', Path(folder) / 'r.md'):
            evaluate_ledger.write_report(evaluate_ledger.load(), evaluate_ledger.recordings())
            self.assertEqual((Path(folder) / 'r.md').read_text(encoding='utf-8'), committed)

    def test_recordings_contain_no_amounts(self):
        rows = {row['id']: row for row in evaluate_ledger.load()}
        for line in evaluate_ledger.RECORDING.read_text(encoding='utf-8').splitlines():
            entry = json.loads(line)
            for amount in find_amounts(rows[entry['id']]['body']):
                self.assertNotIn(amount.text, line)


if __name__ == '__main__':
    unittest.main()

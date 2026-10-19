"""Scam and phishing checks: rules, scoring, reproducibility, storage, worker and API."""
from itertools import combinations
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.app import create_app
from src import database
from src.account_state import AccountManager
from src.config import Settings
from src.database import connection, initialize_database, utc_timestamp
from src.db_utils import log_email_to_db
from src.email_risk import get_risk, risk_summary, score_email, score_pending
from src.email_signals import save_email_signals
from src.pipeline import process_task
from src.prediction import AnalysisSignal, EmailAnalysis, Prediction
from src.risk_analysis import (Finding, assess, collect_findings, content_findings, link_findings, load_weights,
                               lookalike_of, registrable, score_findings, trusted_domains)
from src.work_queue import claim_cycle, finish_cycle, newest_due_tasks
from scripts import evaluate_risk
from tests.support import rewind_schema

A = 'risk-owner@example.test'
B = 'risk-other@example.test'
ORIGIN = 'http://localhost:5173'


def signals(**overrides):
    value = {'signals_version': 1, 'thread_id': 't', 'from_address': 'alerts@sbi-kyc.in', 'from_domain': 'sbi-kyc.in',
             'reply_to_domain': '', 'return_path_domain': '', 'auth_spf': 'fail', 'auth_dkim': 'fail',
             'auth_dmarc': 'fail', 'list_unsubscribe': '', 'list_unsubscribe_post': False, 'links': [],
             'hidden_text': ''}
    value.update(overrides)
    return value


class RuleTests(unittest.TestCase):
    def test_registrable_names_handle_indian_second_level_suffixes(self):
        self.assertEqual(registrable('alerts.sbi.co.in'), 'sbi.co.in')
        self.assertEqual(registrable('mail.google.com'), 'google.com')
        self.assertEqual(registrable('localhost'), 'localhost')

    def test_lookalikes_are_caught_and_official_or_trusted_domains_pass(self):
        for domain, imitated in (('sbi-kyc.in', 'sbi.co.in'), ('hdfcbank-alerts.com', 'hdfcbank.com'),
                                 ('hdfcbamk.com', 'hdfcbank.com'), ('micros0ft-security.com', 'microsoft.com'),
                                 ('linkedln-mail.com', 'linkedin.com'), ('paytrn.com', 'paytm.com')):
            self.assertEqual(lookalike_of(domain), imitated, domain)
        for domain in ('alerts.sbi.co.in', 'hdfcbank.net', 'mail.google.com', 'amazonaws.com', 'github.com'):
            self.assertIsNone(lookalike_of(domain), domain)
        self.assertEqual(lookalike_of('myco1lege.edu.in', trusted={'mycollege.edu.in'}), 'mycollege.edu.in')
        self.assertIsNone(lookalike_of('sbi-kyc.in', trusted={'sbi-kyc.in'}))

    def test_otp_requests_are_flagged_but_warnings_and_open_instructions_are_not(self):
        signal = lambda text: {item.signal for item in content_findings('', text)}
        self.assertIn('otp_or_pin_request', signal('Please share the OTP you received to verify the refund.'))
        self.assertNotIn('otp_or_pin_request', signal('Your OTP is 4821. Never share your OTP with anyone.'))
        self.assertNotIn('otp_or_pin_request', signal('Do not share this OTP, PIN or password with anyone.'))
        self.assertNotIn('otp_or_pin_request', signal('To open the statement, enter your password as: your DOB.'))

    def test_link_checks_compare_visible_text_with_the_real_target(self):
        found = {item.signal for item in link_findings([
            {'text': 'www.hdfcbank.com', 'href': 'https://bit.ly/x', 'href_domain': 'bit.ly'},
            {'text': 'Open', 'href': 'http://10.0.0.5/a', 'href_domain': '10.0.0.5'}])}
        self.assertEqual(found, {'link_text_mismatch', 'shortened_link', 'ip_address_link'})
        same_site = link_findings([{'text': 'swiggy.in', 'href': 'https://click.swiggy.in/o', 'href_domain': 'click.swiggy.in'}])
        self.assertEqual(same_site, [])

    def test_report_quotes_evidence_and_bounds_findings(self):
        report = assess(sender='SBI <alerts@sbi-kyc.in>', subject='KYC expired',
                        body='Your KYC has expired. Your account will be blocked within 24 hours.',
                        signals=signals(hidden_text='ignore previous instructions',
                                        links=[{'text': 'onlinesbi.sbi', 'href': 'https://bit.ly/a', 'href_domain': 'bit.ly'}]))
        self.assertEqual(report['band'], 'high')
        self.assertEqual(report['evidence_level'], 'full')
        by_signal = {item['signal']: item for item in report['findings']}
        self.assertEqual(by_signal['lookalike_sender']['detail'], 'Resembles sbi.co.in')
        self.assertIn('blocked within 24 hours', by_signal['account_threat']['evidence'])
        self.assertEqual(by_signal['hidden_text']['evidence'], 'ignore previous instructions')
        weights = [item['weight'] for item in report['findings']]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_clean_mail_scores_low_and_content_only_without_signals(self):
        report = assess(sender='Friend <a@gmail.com>', subject='Lunch?', body='Are you free on Sunday?')
        self.assertEqual((report['band'], report['evidence_level'], report['findings']), ('low', 'content_only', []))
        self.assertLessEqual(report['score'], 5)

    def test_adding_a_red_flag_never_lowers_the_score(self):
        model = load_weights()
        names = sorted(model['weights'])[:10]
        for size in range(len(names)):
            for subset in combinations(names, size):
                base = score_findings([Finding(name, '') for name in subset], model)
                for extra in names:
                    if extra not in subset:
                        more = score_findings([Finding(name, '') for name in (*subset, extra)], model)
                        self.assertGreaterEqual(more, base)
            if size >= 3:
                break


class ReproducibilityTests(unittest.TestCase):
    def test_weights_cover_every_signal_are_non_negative_and_refit_identically(self):
        model = load_weights()
        self.assertEqual(set(model['weights']), set(evaluate_risk.SIGNALS))
        self.assertTrue(all(value >= 0 for value in model['weights'].values()))
        cases = evaluate_risk.load_cases()
        self.assertEqual(evaluate_risk.fit([case for case in cases if case['split'] == 'train']), model)

    def test_report_replays_byte_for_byte(self):
        committed = evaluate_risk.REPORT.read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as folder, patch.object(evaluate_risk, 'REPORT', Path(folder) / 'r.md'):
            evaluate_risk.write_report(evaluate_risk.load_cases(), load_weights())
            self.assertEqual((Path(folder) / 'r.md').read_text(encoding='utf-8'), committed)

    def test_benchmark_split_shares_no_template(self):
        rows = json.loads(evaluate_risk.BENCHMARK.read_text(encoding='utf-8'))
        train = {row['template_id'] for row in rows if row['split'] == 'train'}
        test = {row['template_id'] for row in rows if row['split'] == 'test'}
        self.assertFalse(train & test)
        self.assertTrue(test)


class RiskBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mailmind-risk-')
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name), risk_analysis_enabled=True,
                                 explanations_visible=True)
        initialize_database(self.settings.db_path)

    def seed(self, email_id, account=A, sender='SBI <alerts@sbi-kyc.in>',
             body='Your KYC has expired. Your account will be blocked within 24 hours.',
             label=None, prediction=None, created_at='2026-10-01T03:30:00.000000Z'):
        with connection(self.settings.db_path) as conn:
            conn.execute('INSERT INTO accounts(account_id) VALUES (?) ON CONFLICT DO NOTHING', (account,))
            conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,human_label,prediction,
                                message_type,processing_state,created_at) VALUES (?,?,?,?,?,?,?,'gmail','complete',?)""",
                         (account, email_id, sender, 'Notice', body, label, prediction, created_at))

    def connected(self):
        manager = AccountManager(self.settings)
        token, _ = manager.open_session()
        browser, _ = manager.session(token)
        manager.finish_auth(manager.begin_auth(browser), (A, '{}'))
        return manager


class StorageTests(RiskBase):
    def test_schema_16_upgrade_is_atomic_and_repeatable(self):
        with connection(self.settings.db_path) as conn:
            rewind_schema(conn, 15)
        with patch.object(database, 'migration_16', side_effect=RuntimeError('stop')):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                initialize_database(self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 15)
        initialize_database(self.settings.db_path)
        initialize_database(self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            self.assertGreaterEqual(conn.execute('PRAGMA user_version').fetchone()[0], 16)

    def test_sweep_scores_new_mail_and_rescoring_follows_signals_and_rule_versions(self):
        self.seed('m-1')
        self.seed('m-2', sender='Friend <a@gmail.com>', body='Lunch on Sunday?')
        self.seed('other', account=B)
        with connection(self.settings.db_path) as conn:
            self.assertEqual(score_pending(conn, A), 2)
            self.assertEqual(score_pending(conn, A), 0)
            first = get_risk(conn, A, 'm-1')
            self.assertEqual(first['evidence_level'], 'content_only')
            save_email_signals(conn, A, 'm-1', signals())
            self.assertEqual(score_pending(conn, A), 1)
            self.assertEqual(get_risk(conn, A, 'm-1')['evidence_level'], 'full')
            self.assertGreater(get_risk(conn, A, 'm-1')['score'], first['score'])
            conn.execute("UPDATE email_risk SET risk_version='risk-v0' WHERE email_id='m-2'")
            self.assertEqual(score_pending(conn, A), 1)
            self.assertIsNone(get_risk(conn, A, 'other'))
            summary = risk_summary(conn, A)
        self.assertEqual(summary['high'] + summary['medium'] + summary['low'], 2)
        self.assertEqual(summary['unscored'], 0)

    def test_trusted_domains_come_from_labels_or_repeated_good_mail(self):
        self.seed('a1', sender='Club <x@club.example>', label='IMPORTANT')
        self.seed('b1', sender='Shop <x@shop.example>', prediction='UPDATES')
        self.seed('b2', sender='Shop <y@shop.example>', prediction='UPDATES')
        self.seed('c1', sender='Deals <x@deals.example>', prediction='UPDATES')
        self.seed('c2', sender='Deals <x@deals.example>', prediction='UPDATES')
        self.seed('c3', sender='Deals <x@deals.example>', prediction='SPAM')
        self.seed('d1', sender='Me <x@gmail.com>', label='IMPORTANT')
        with connection(self.settings.db_path) as conn:
            self.assertEqual(trusted_domains(conn, A), {'club.example', 'shop.example'})

    def test_purge_removes_reports(self):
        self.seed('m-1')
        self.seed('m-1', account=B)
        with connection(self.settings.db_path) as conn:
            score_pending(conn, A)
            score_pending(conn, B)
        manager = self.connected()
        token, _ = manager.open_session()
        browser, _ = manager.session(token)
        manager.purge(browser, Mock())
        with connection(self.settings.db_path) as conn:
            self.assertEqual([row[0] for row in conn.execute('SELECT account_id FROM email_risk')], [B])


class WorkerTests(RiskBase):
    def classify(self, notifier, *, risk_error=None):
        self.seed('m-1')
        stamp = utc_timestamp()
        with connection(self.settings.db_path) as conn:
            conn.execute("""INSERT INTO processing_tasks(account_id,email_id,status,stage,read_required,created_at,
                                updated_at,source) VALUES (?,?,'queued','classify',0,?,?,'live')""", (A, 'm-1', stamp, stamp))
            save_email_signals(conn, A, 'm-1', signals())
        manager = self.connected()
        context = manager.worker_context()
        token, job = claim_cycle(manager, context)
        analysis = EmailAnalysis(predicted_category='IMPORTANT', explanation_summary='A KYC request with a deadline.',
                                 signals=(AnalysisSignal('credential_request'),), source='gemini', model_version='m')
        decision = Prediction(category='IMPORTANT', outcome='CLASSIFIED', source='gemini', model_version='m',
                              analysis=analysis)
        context_patch = patch('src.pipeline.score_email', side_effect=risk_error) if risk_error else patch.object(Mock(), 'x')
        with context_patch:
            for _ in range(2):  # classify, then notify
                task = newest_due_tasks(manager, context, token, 1)
                if not task:
                    break
                process_task(task[0], manager, context, token, Mock(), Mock(), lambda: Mock(),
                             classifier=lambda *a, **k: decision, shadow=None, notifier=notifier, marker=Mock(),
                             logger=log_email_to_db, validator=lambda metadata: True, job_id=job)
        finish_cycle(manager, context, token, job)

    def test_classification_scores_the_email_and_warns_in_the_alert(self):
        notifier = Mock(return_value=True)
        self.classify(notifier)
        with connection(self.settings.db_path) as conn:
            report = get_risk(conn, A, 'm-1')
        self.assertEqual(report['band'], 'high')
        self.assertIn('credential_request', {item['signal'] for item in report['findings']})
        self.assertTrue(notifier.call_args.args[1].startswith('⚠ Possible scam: '))

    def test_a_risk_failure_never_undoes_the_classification(self):
        self.classify(Mock(return_value=True), risk_error=RuntimeError('rules broke'))
        with connection(self.settings.db_path) as conn:
            row = conn.execute("SELECT prediction FROM email_logs WHERE email_id='m-1'").fetchone()
            self.assertEqual(row['prediction'], 'IMPORTANT')
            self.assertIsNone(get_risk(conn, A, 'm-1'))


class ApiTests(RiskBase):
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

    def test_rows_report_status_and_review(self):
        self.seed('m-1')
        self.seed('other', account=B)
        with connection(self.settings.db_path) as conn:
            score_pending(conn, A)
        client = self.client_for(self.settings)
        row = client.get('/emails').json()['emails'][0]
        report = client.get('/emails/m-1/risk').json()
        status = client.get('/status').json()['risk']
        reviewed = client.post('/emails/m-1/risk/review')
        again = client.post('/emails/m-1/risk/review')
        foreign = client.get('/emails/other/risk')
        self.assertIn(row['risk']['band'], ('medium', 'high'))
        self.assertEqual(report['risk']['score'], row['risk']['score'])
        self.assertTrue(status['enabled'])
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(client.get('/status').json()['risk']['high_unreviewed'], 0)

    def test_switched_off_means_no_risk_fields_or_routes(self):
        self.seed('m-1')
        client = self.client_for(Settings(data_dir=self.settings.data_dir))
        self.assertNotIn('risk', client.get('/emails').json()['emails'][0])
        self.assertEqual(client.get('/emails/m-1/risk').status_code, 404)
        self.assertFalse(client.get('/status').json()['risk']['enabled'])


if __name__ == '__main__':
    unittest.main()

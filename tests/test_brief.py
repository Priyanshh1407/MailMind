"""Daily brief: local candidates, the three agents' verification, fallback, proposals, API and replay."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.app import create_app
from src import database
from src.account_state import AccountManager
from src.chief_of_staff import fallback_brief, gather, multi_agent_brief, single_agent_brief
from src.config import Settings
from src.database import connection, initialize_database
from scripts import evaluate_brief
from scripts.evaluate_agent import ACCOUNT, benchmark_database, load as load_mailbox
from tests.support import rewind_schema

ORIGIN = 'http://localhost:5173'
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


class AgentBase(unittest.TestCase):
    def setUp(self):
        self.mailbox, _ = load_mailbox()
        self.database = benchmark_database(self.mailbox)
        self.tools = self.database.__enter__()
        self.addCleanup(self.database.__exit__, None, None, None)

    def candidates(self):
        with self.tools.read_db() as conn:
            return gather(conn, ACCOUNT, now=NOW, ledger=True, risk=True)


class GatherTests(AgentBase):
    def test_candidates_are_local_recent_and_never_spam(self):
        candidates = self.candidates()
        refs = {item.ref for item in candidates}
        self.assertIn('mail:mom', refs)
        self.assertIn('mail:github', refs)
        self.assertIn('action:1', refs)  # the Q3 report task, due within the week
        self.assertFalse(any(ref.startswith('mail:inject') for ref in refs))
        self.assertTrue(all(item.kind in ('new_mail', 'action', 'overdue', 'risk', 'renewal', 'waiting') for item in candidates))

    def test_the_fallback_needs_no_model(self):
        brief = fallback_brief(self.candidates(), 'provider_quota')
        self.assertEqual(brief['mode'], 'fallback')
        # Each line keeps what the dashboard needs to show it and open its email.
        line = brief['sections'][0]['sentences'][0]
        self.assertTrue({'title', 'detail', 'when', 'email_id', 'message_id'} <= set(line))
        self.assertTrue(brief['sections'])
        self.assertEqual(brief['proposals'], [])


class PipelineTests(AgentBase):
    def scripted(self, *, facts, writer):
        calls = []
        def call(system, schema, name, contents):
            calls.append((name, json.loads(contents)))
            if name == 'mailmind_brief_triage':
                return {'items': [{'ref': 'action:1', 'why': 'due soon'}, {'ref': 'mail:mom', 'why': 'family'},
                                  {'ref': 'invented:ref', 'why': 'made up'}]}
            if name == 'mailmind_brief_research':
                return {'facts': facts.pop(0)}
            return writer
        return call, calls

    def test_only_verified_facts_reach_sentences_and_only_valid_proposals_survive(self):
        call, calls = self.scripted(
            facts=[[{'text': 'Priya needs the Q3 report by Friday 5 pm.', 'quote': 'send me the Q3 report by Friday 5 pm'},
                    {'text': 'Invented fact.', 'quote': 'this quote is not in the email'}],
                   [{'text': 'Mom wants a call when you land.', 'quote': 'Call me when you land in Delhi'}]],
            writer={'sections': [{'title': 'Needs you today', 'sentences': [
                {'text': 'Send Priya the Q3 report by Friday 5 pm.', 'fact_ids': ['f1']},
                {'text': 'Call Mom after landing.', 'fact_ids': ['f2']},
                {'text': 'An unsupported claim.', 'fact_ids': ['f9']}]}],
                'proposals': [{'type': 'complete_action', 'ref': 'action:1', 'label': 'Mark the Q3 task done'},
                              {'type': 'mark_risky_reviewed', 'ref': 'action:1', 'label': 'Wrong kind'},
                              {'type': 'complete_action', 'ref': 'action:99', 'label': 'Not given'}]})
        brief, run = multi_agent_brief(self.candidates(), self.tools.read_db, ACCOUNT, call=call)
        sentences = brief['sections'][0]['sentences']
        self.assertEqual([item['text'] for item in sentences], ['Send Priya the Q3 report by Friday 5 pm.', 'Call Mom after landing.'])
        self.assertEqual(brief['dropped_sentences'], 1)
        self.assertEqual(sentences[0]['citations'][0]['email_id'], 'manager')
        self.assertEqual([(item['type'], item['action_id']) for item in brief['proposals']], [('complete_action', 1)])
        self.assertEqual(len(run.facts), 2)
        self.assertNotIn('body', json.dumps(calls[0][1]))  # triage sees titles only
        self.assertEqual([agent['agent'] for agent in run.trace], ['triage', 'research', 'research', 'writer'])

    def test_triage_choosing_nothing_raises_so_the_fallback_is_used(self):
        with self.assertRaises(ValueError):
            multi_agent_brief(self.candidates(), self.tools.read_db, ACCOUNT, call=lambda *args: {'items': []})

    def test_single_agent_ablation_uses_the_same_verification(self):
        answer = {'sections': [{'title': 'Today', 'sentences': [
            {'text': 'Review PR 42.', 'email_id': 'github', 'quote': 'requested your review on pull request #42'},
            {'text': 'Fake.', 'email_id': 'github', 'quote': 'nothing like this'}]}]}
        brief, _ = single_agent_brief(self.candidates(), self.tools.read_db, ACCOUNT, call=lambda *args: answer)
        self.assertEqual(([item['text'] for item in brief['sections'][0]['sentences']], brief['dropped_sentences']), (['Review PR 42.'], 1))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mailmind-brief-')
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name), daily_brief_enabled=True, daily_brief_time='00:00')
        initialize_database(self.settings.db_path)
        self.account = 'brief-owner@example.test'
        with connection(self.settings.db_path) as conn:
            conn.execute('INSERT INTO accounts(account_id) VALUES (?)', (self.account,))
            stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,prediction,message_type,processing_state,created_at)
                            VALUES (?,'m1','Boss <boss@acme.example>','Q3 report','Please send the Q3 report by Friday.','IMPORTANT','gmail','complete',?)""",
                         (self.account, stamp))
            conn.execute("""INSERT INTO email_actions(account_id,email_id,fingerprint,action_type,title,description,evidence,due_at,
                                due_precision,confidence,extraction_source,created_at,updated_at)
                            VALUES (?,'m1',?,'reply_required','Send the Q3 report','','send the Q3 report',?,'exact_time','high','system',?,?)""",
                         (self.account, '0' * 64, stamp, stamp, stamp))

    def client(self, settings=None):
        app = create_app(settings=settings or self.settings, model_factory=Mock(return_value=Mock(model_loaded=False)),
                         vector_factory=Mock(return_value=Mock()))
        client = TestClient(app, base_url='http://localhost')
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        client.headers['Origin'] = ORIGIN
        client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
        context, _ = app.state.accounts.session(client.cookies.get('mailmind_session'))
        app.state.accounts.finish_auth(app.state.accounts.begin_auth(context), (self.account, '{}'))
        client.headers['X-CSRF-Token'] = client.get('/session').json()['csrf_token']
        return client

    def wait(self, client):
        for _ in range(100):
            value = client.get('/briefs/today').json()['brief']
            if value and value['status'] != 'running':
                return value
            time.sleep(0.05)
        self.fail('brief never finished')

    def test_providers_down_still_gives_a_brief_and_proposals_run_only_when_approved(self):
        from src.llm_api import StructuredUnavailable
        with patch('src.llm_api.call_structured', side_effect=StructuredUnavailable('provider_quota', True)):
            client = self.client()
            brief = self.wait(client)
        self.assertEqual((brief['status'], brief['brief']['mode']), ('fallback', 'fallback'))
        self.assertIn('Send the Q3 report', json.dumps(brief['brief']))

        def answers(contents, *, system, schema, schema_name, **kwargs):
            if schema_name == 'mailmind_brief_triage':
                return {'items': [{'ref': 'action:1', 'why': 'due'}]}, 'gemini', 'm'
            if schema_name == 'mailmind_brief_research':
                return {'facts': [{'text': 'The Q3 report is due Friday.', 'quote': 'send the Q3 report by Friday'}]}, 'gemini', 'm'
            return {'sections': [{'title': 'Today', 'sentences': [{'text': 'Send the Q3 report by Friday.', 'fact_ids': ['f1']}]}],
                    'proposals': [{'type': 'complete_action', 'ref': 'action:1', 'label': 'Mark it done'}]}, 'gemini', 'm'
        with patch('src.llm_api.call_structured', side_effect=answers):
            self.assertEqual(client.post('/briefs').status_code, 202)
            brief = self.wait(client)
        self.assertEqual(brief['status'], 'ready')
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM email_actions').fetchone()[0], 'open')
        approved = client.post(f'/briefs/{brief["brief_id"]}/proposals/0/approve')
        again = client.post(f'/briefs/{brief["brief_id"]}/proposals/0/approve')
        self.assertEqual((approved.status_code, again.status_code), (200, 409))
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM email_actions').fetchone()[0], 'completed')
        self.assertEqual(client.post('/briefs').status_code, 429)

    def test_config_switch_purge_and_schema(self):
        with self.assertRaises(ValueError):
            Settings(daily_brief_time='25:00')
        with self.assertRaises(ValueError):
            Settings(brief_telegram_enabled=True)
        self.assertEqual(self.client(Settings(data_dir=self.settings.data_dir)).get('/briefs/today').status_code, 404)
        with connection(self.settings.db_path) as conn:
            conn.execute("""INSERT INTO daily_briefs(account_id,brief_date,status,created_at,updated_at)
                            VALUES (?,'2026-10-12','fallback','2026-10-12T00:00:00.000000Z','2026-10-12T00:00:00.000000Z')""", (self.account,))
        manager = AccountManager(self.settings)
        token, _ = manager.open_session()
        browser, _ = manager.session(token)
        manager.finish_auth(manager.begin_auth(browser), (self.account, '{}'))
        browser, _ = manager.session(token)
        manager.purge(browser, Mock())
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_briefs').fetchone()[0], 0)
            rewind_schema(conn, 19)
        with patch.object(database, 'migration_20', side_effect=RuntimeError('stop')):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                initialize_database(self.settings.db_path)
        initialize_database(self.settings.db_path)


class ReproducibilityTests(unittest.TestCase):
    def test_report_replays_byte_for_byte(self):
        committed = evaluate_brief.REPORT.read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as folder, patch.object(evaluate_brief, 'REPORT', Path(folder) / 'r.md'):
            evaluate_brief.write_report(evaluate_brief.replay(evaluate_brief.recordings()))
            self.assertEqual((Path(folder) / 'r.md').read_text(encoding='utf-8'), committed)


if __name__ == '__main__':
    unittest.main()

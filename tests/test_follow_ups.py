"""Follow-ups: masking, style, checks, revision, Gmail reads (never sends), API and replay."""
import base64
from datetime import date
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
from src.config import Settings
from src.database import connection, initialize_database
from src.follow_ups import Masker, build_request, check_draft, draft, learn_style, prepare_thread
from scripts import evaluate_follow_ups
from tests.support import FakeRequest, rewind_schema

A = 'arjun.nair@example.test'
ORIGIN = 'http://localhost:5173'
ME = f'Arjun Nair <{A}>'
THEM = 'Priya Menon <priya.menon@zohocorp.com>'
DAY = 86400000
THREAD = [{'from': THEM, 'to': ME, 'date_ms': 1789000000000, 'body': 'Thanks, we will review applications this week.'},
          {'from': ME, 'to': THEM, 'date_ms': 1789000000000 + 2 * DAY,
           'body': 'Dear Ms. Menon,\n\nI applied for the Backend Engineer role (Job ID BE-2041) on 12 September and paid the Rs 500 fee. '
                   'My portfolio is at https://arjun.example/work.\n\nRegards,\nArjun Nair'}]
STYLE = {'greeting': 'Dear', 'signoff': 'Regards', 'signature': 'Arjun Nair', 'typical_words': 90, 'spelling': 'british', 'samples': 3}


def answer(body=None, greeting='Dear {{recipient_name}},', closing='Regards', quote='Backend Engineer role'):
    body = body or ('I am writing about my application for the Backend Engineer role, Job ID BE-2041, which I submitted on '
                    '12 September. I remain very interested in the position and would be glad to provide any further '
                    'information that would help your review. Could you let me know whether the application has been '
                    'shortlisted, and what the next step in the process would be?')
    return {'greeting': greeting, 'body_paragraphs': [body], 'closing': closing,
            'references': [{'message_index': 1, 'quote': quote}]}


class MaskingTests(unittest.TestCase):
    def test_names_amounts_links_and_addresses_are_masked_and_restored_locally(self):
        thread, summary, masker, names, info = prepare_thread(THREAD, A)
        text = thread[-1]['masked']
        for secret in ('Menon', 'Arjun', 'Nair', 'Rs 500', 'https://arjun.example/work', 'priya.menon'):
            self.assertNotIn(secret, text)
        self.assertIn('Dear {{recipient_name}},', text)
        self.assertEqual(names['{{recipient_name}}'], 'Ms. Menon')
        self.assertEqual(masker.restore('Fee [AMOUNT_1] at [LINK_1], {{my_name}}', names), 'Fee Rs 500 at https://arjun.example/work, Arjun')
        self.assertTrue(info['waiting'])
        self.assertEqual(summary[-1]['from'], 'You')

    def test_team_mailboxes_and_unnamed_greetings_are_handled_like_a_person_would(self):
        team = [{'from': ME, 'to': 'Sales Desk <sales@brightpack.in>', 'date_ms': 1, 'body': 'Dear Sir,\n\nCould you share a quote?'}]
        self.assertEqual(prepare_thread(team, A)[3]['{{recipient_name}}'], 'Sir')
        plain = [{'from': ME, 'to': 'HR Team <hr@acme.example>', 'date_ms': 1, 'body': 'Hi,\n\nCould you send the letter?'}]
        names = prepare_thread(plain, A)[3]
        self.assertEqual(names['{{recipient_name}}'], '')
        self.assertIn('"greeting": "Hi,"', build_request(prepare_thread(plain, A)[0], {**STYLE, 'greeting': 'Hi'},
                                                          'standard', today=date(2026, 10, 1), named=False))

    def test_masker_reuses_one_placeholder_for_a_repeated_value(self):
        masker = Masker({})
        self.assertEqual(masker.mask('Pay Rs 500 now. Rs 500 is due.'), 'Pay [AMOUNT_1] now. [AMOUNT_1] is due.')


class StyleTests(unittest.TestCase):
    def test_greeting_signoff_signature_and_spelling_come_from_sent_mail(self):
        style = learn_style(['Hi Neha,\n\nI have organised the files.\n\nThanks,\nArjun\n\nOn Mon, Neha wrote:\n> old text',
                             'Hi Rahul,\n\nCan we talk at 4?\n\nThanks,\nArjun'])
        self.assertEqual((style['greeting'], style['signoff'], style['signature'], style['spelling']),
                         ('Hi', 'Thanks', 'Arjun', 'british'))
        self.assertEqual(learn_style([])['greeting'], 'Dear')


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.thread = prepare_thread(THREAD, A)[0]

    def issues(self, value):
        return check_draft(value, self.thread, STYLE, 'standard')

    def test_a_good_draft_passes(self):
        self.assertEqual(self.issues(answer()), [])

    def test_ai_tells_invented_facts_and_style_mismatches_are_caught(self):
        stock = answer(body=answer()['body_paragraphs'][0].replace('I am writing about', 'I hope this email finds you well. Just circling back on'))
        self.assertTrue(any('stock phrase' in issue for issue in self.issues(stock)))
        self.assertTrue(any('exclamation' in issue for issue in self.issues(answer(body=answer()['body_paragraphs'][0] + ' Thanks!'))))
        invented = answer(body=answer()['body_paragraphs'][0].replace('12 September', '19 September'))
        self.assertTrue(any('"19"' in issue for issue in self.issues(invented)))
        friday = answer(body=answer()['body_paragraphs'][0] + ' I can call on Friday.')
        self.assertTrue(any('Friday' in issue for issue in self.issues(friday)))
        self.assertTrue(any('Start with exactly' in issue for issue in self.issues(answer(greeting='Hi {{recipient_name}},'))))
        self.assertTrue(any('End with' in issue for issue in self.issues(answer(closing='Cheers'))))
        self.assertTrue(any('exact quote' in issue for issue in self.issues(answer(quote='a quote that is not there'))))
        self.assertTrue(any('words' in issue for issue in self.issues(answer(body='Any update?'))))

    def test_one_revision_then_problems_are_shown_not_hidden(self):
        thread, _summary, masker, names, _info = prepare_thread(THREAD, A)
        calls = []
        def fixed(contents):
            calls.append(json.loads(contents))
            return answer(body='Any update? Thanks!') if len(calls) == 1 else answer()
        result = draft(thread, STYLE, 'standard', masker, names, call=fixed, today=date(2026, 10, 1))
        self.assertEqual((result['revised'], result['issues']), (True, []))
        self.assertIn('fix_these_problems', calls[1]['revise'])
        self.assertTrue(result['text'].startswith('Dear Ms. Menon,'))
        self.assertTrue(result['text'].endswith('Regards,\nArjun Nair'))
        stubborn = draft(thread, STYLE, 'standard', masker, names, call=lambda contents: answer(body='Any update?'),
                         today=date(2026, 10, 1))
        self.assertTrue(stubborn['issues'])


class FakeGmail:
    """Read methods only. Anything that could send mail fails the test."""

    def __init__(self):
        sent = {'id': 'sent-1', 'threadId': 'thread-1', 'internalDate': str(THREAD[-1]['date_ms']), 'snippet': 'I applied for the role',
                'payload': {'headers': [{'name': 'To', 'value': THEM}, {'name': 'Subject', 'value': 'Application'}]}}
        def full(message, index):
            return {'id': f'm-{index}', 'threadId': 'thread-1', 'internalDate': str(message['date_ms']), 'payload': {
                'mimeType': 'text/plain', 'headers': [{'name': 'From', 'value': message['from']}, {'name': 'To', 'value': message['to']},
                                                      {'name': 'Subject', 'value': 'Application'}],
                'body': {'data': base64.urlsafe_b64encode(message['body'].encode()).decode()}}}
        self.sent, self.thread = sent, {'id': 'thread-1', 'messages': [full(item, index) for index, item in enumerate(THREAD)]}

    def users(self):
        return self

    def messages(self):
        return self

    def threads(self):
        return self

    def list(self, **kwargs):
        return FakeRequest({'messages': [{'id': 'sent-1'}]})

    def get(self, **kwargs):
        if kwargs.get('id') == 'thread-1':
            return FakeRequest(self.thread)
        return FakeRequest(self.sent if kwargs.get('format') == 'metadata' else self.thread['messages'][-1])

    def __getattr__(self, name):
        raise AssertionError(f'MailMind must never call Gmail {name}()')


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mailmind-follow-')
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name), follow_ups_enabled=True)
        initialize_database(self.settings.db_path)

    def client(self, settings=None):
        app = create_app(settings=settings or self.settings, model_factory=Mock(return_value=Mock(model_loaded=False)),
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

    def wait(self, read, done):
        for _ in range(100):
            value = read()
            if done(value):
                return value
            time.sleep(0.05)
        self.fail('job never finished')

    def test_sent_list_draft_copy_and_never_send(self):
        gmail = FakeGmail()
        with patch('src.email_client.refresh_gmail', return_value=gmail), \
                patch('src.llm_api.call_structured', side_effect=lambda *a, **k: (answer(), 'gemini', 'm')):
            client = self.client()
            self.assertEqual(client.post('/follow-ups/sent/refresh').status_code, 202)
            page = self.wait(lambda: client.get('/follow-ups/sent').json(), lambda value: not value['refreshing'])
            item = page['messages'][0]
            self.assertEqual((item['reply_state'], item['subject']), ('waiting', 'Application'))
            created = client.post('/follow-ups/drafts', json={'message_id': 'sent-1'})
            result = self.wait(lambda: client.get(f'/follow-ups/drafts/{created.json()["follow_up_id"]}').json(),
                               lambda value: value['status'] != 'running')
        self.assertEqual(result['status'], 'drafted')
        self.assertTrue(result['text'].startswith('Dear Ms. Menon,'))
        self.assertEqual(result['summary'][-1]['from'], 'You')
        self.assertEqual(client.patch(f'/follow-ups/drafts/{result["follow_up_id"]}', json={'status': 'copied'}).status_code, 200)
        self.assertEqual(client.post('/follow-ups/drafts', json={'message_id': 'unknown'}).status_code, 404)

    def test_switched_off_local_only_purge_and_schema(self):
        self.assertEqual(self.client(Settings(data_dir=self.settings.data_dir)).get('/follow-ups/sent').status_code, 404)
        local = Settings(data_dir=Path(self.temp.name) / 'local', local_only=True, follow_ups_enabled=True)
        initialize_database(local.db_path)
        self.assertEqual(self.client(local).get('/follow-ups/sent').status_code, 403)
        with connection(self.settings.db_path) as conn:
            conn.execute('INSERT INTO accounts(account_id) VALUES (?) ON CONFLICT DO NOTHING', (A,))
            conn.execute("""INSERT INTO sent_messages(account_id,message_id,thread_id,sent_at,fetched_at)
                            VALUES (?,?,?,1,'2026-10-01T00:00:00.000000Z')""", (A, 'sent-1', 'thread-1'))
        manager = AccountManager(self.settings)
        token, _ = manager.open_session()
        browser, _ = manager.session(token)
        manager.finish_auth(manager.begin_auth(browser), (A, '{}'))
        browser, _ = manager.session(token)
        manager.purge(browser, Mock())
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sent_messages').fetchone()[0], 0)
            rewind_schema(conn, 18)
        with patch.object(database, 'migration_19', side_effect=RuntimeError('stop')):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                initialize_database(self.settings.db_path)
        initialize_database(self.settings.db_path)


class ReproducibilityTests(unittest.TestCase):
    def test_every_benchmark_request_is_private(self):
        for row in evaluate_follow_ups.load():
            thread, _summary, _masker, names, _info = prepare_thread(row['thread'], row['account'])
            contents = build_request(thread, learn_style(row['style_samples']), 'standard', today=date(2026, 10, 1),
                                     named=bool(names['{{recipient_name}}']))
            evaluate_follow_ups.assert_private(contents, row)

    def test_report_replays_byte_for_byte(self):
        committed = evaluate_follow_ups.REPORT.read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as folder, patch.object(evaluate_follow_ups, 'REPORT', Path(folder) / 'r.md'):
            rows = evaluate_follow_ups.load()
            evaluate_follow_ups.write_report(rows, evaluate_follow_ups.replay(rows, evaluate_follow_ups.recordings()))
            self.assertEqual((Path(folder) / 'r.md').read_text(encoding='utf-8'), committed)


if __name__ == '__main__':
    unittest.main()

from src.prediction import Prediction, LABEL2ID, ID2LABEL
from src.email_client import FetchBatch
"""Offline account boundaries and cancellation tests. All data is synthetic."""
from pathlib import Path
from contextlib import contextmanager
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from api.app import create_app
from src.config import Settings, LEGACY_ACCOUNT
from src.database import connection
from src.account_state import AccountManager, AccessDenied, WorkCancelled
from src.db_utils import log_email_to_db, get_email
from src.vector_db import search_similar_emails, get_knn_prediction, add_email_to_vector_db

ORIGIN = 'http://localhost:5173'
A, B = 'a@example.test', 'b@example.test'


class FakeCollection:
    def __init__(self):
        self.rows = {}
        self.fail_delete = False
        self.queries = []

    def upsert(self, ids, documents, metadatas, embeddings=None):
        for key, doc, meta in zip(ids, documents, metadatas):
            self.rows[key] = (doc, meta)

    def query(self, query_texts, n_results, where):
        self.queries.append(where)
        rows = [row for row in self.rows.values() if row[1] and row[1].get('account_id') == where['account_id']][:n_results]
        return {'documents': [[r[0] for r in rows]], 'metadatas': [[r[1] for r in rows]], 'distances': [[0.1]*len(rows)]}

    def get(self, include):
        return {'ids': list(self.rows), 'metadatas': [r[1] for r in self.rows.values()]}

    def delete(self, where=None, ids=None):
        if self.fail_delete:
            raise RuntimeError('synthetic-private-provider-detail')
        for key in list(self.rows):
            if (ids is not None and key in ids) or (where and self.rows[key][1] and self.rows[key][1].get('account_id') == where['account_id']):
                del self.rows[key]


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mailmind-phase2-')
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.collection = FakeCollection()
        self.model = Mock(model_loaded=True)
        self.model.predict.return_value = Prediction(category='IMPORTANT',outcome='CLASSIFIED')
        self.oauth = Mock(return_value=(A, '{}'))
        self.app = create_app(settings=self.settings, model_factory=Mock(return_value=self.model),
                              vector_factory=Mock(return_value=self.collection), oauth_factory=self.oauth)
        self.client = TestClient(self.app, base_url='http://localhost')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.manager = self.app.state.accounts

    def open_session(self, client=None):
        client = client or self.client
        response = client.post('/session', headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,200)
        client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':response.json()['csrf_token']})
        return response

    def connect(self, account=A):
        if not self.client.cookies.get('mailmind_session'):
            self.open_session()
        self.oauth.return_value = (account,'{}')
        response=self.client.post('/authenticate')
        self.assertEqual(response.status_code,202)
        self.app.state.auth_futures[response.json()['job_id']].result(timeout=5)
        return self.manager.worker_context()

    def seed(self, account, key='same'):
        log_email_to_db(key,'synthetic sender',account+' subject','canonical body','SPAM','SPAM',account_id=account,db_path=self.settings.db_path)
        add_email_to_vector_db(key,account+' subject','canonical body','IMPORTANT',account_id=account,collection=self.collection)

    def test_private_routes_require_local_session_not_token_file(self):
        self.settings.legacy_token_path.write_text('{}')
        for method,path,body in [('GET','/emails',None),('GET','/user',None),('GET','/status',None),('GET','/session',None),
                                 ('POST','/predict',{'subject':'s','body':'b'}),('POST','/feedback',{'email_id':'x','label':'SPAM'}),
                                 ('POST','/logout',None),('POST','/disconnect',None),('POST','/authenticate',None),
                                 ('DELETE','/account-data',None),('DELETE','/legacy-data',{'confirmation':'DELETE_UNASSIGNED_DATA'})]:
            with self.subTest(path=path):
                response=self.client.request(method,path,json=body,headers={'Origin':ORIGIN})
                self.assertEqual(response.status_code,401)
        self.assertEqual(self.client.get('/').status_code,200)
        self.oauth.assert_not_called()

    def test_loopback_session_cookie_flags_and_no_raw_token_in_database(self):
        response=self.open_session()
        cookie=response.headers['set-cookie'].lower()
        self.assertIn('httponly',cookie)
        self.assertIn('samesite=strict',cookie)
        token=self.client.cookies.get('mailmind_session')
        with connection(self.settings.db_path) as conn:
            self.assertNotEqual(conn.execute('SELECT token_hash FROM local_sessions').fetchone()[0],token)
        self.assertEqual(self.client.get('/session').headers['cache-control'],'no-store')

    def test_local_session_can_only_open_from_the_dashboard_origin(self):
        self.assertEqual(self.client.post('/session').status_code,403)
        self.assertEqual(self.client.post('/session',headers={'Origin':'http://evil.example'}).status_code,403)
        self.assertEqual(self.client.post('/session',headers={'Origin':ORIGIN}).status_code,200)

    def test_origin_csrf_and_host_are_checked(self):
        self.connect()
        self.assertEqual(self.client.post('/logout',headers={'X-CSRF-Token':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/logout',headers={'Origin':'http://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/emails',headers={'Host':'evil.example'}).status_code,400)
        response=self.client.options('/feedback',headers={'Origin':'http://evil.example','Access-Control-Request-Method':'POST'})
        self.assertNotIn('access-control-allow-origin',response.headers)
        response=self.client.options('/feedback',headers={'Origin':ORIGIN,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'X-CSRF-Token'})
        self.assertEqual(response.headers['access-control-allow-origin'],ORIGIN)

    def test_expired_session_is_rejected(self):
        self.open_session()
        with connection(self.settings.db_path) as conn:
            conn.execute('UPDATE local_sessions SET expires_at=0')
        self.assertEqual(self.client.get('/session').status_code,401)

    def test_reads_and_feedback_are_owned_and_text_is_server_side(self):
        self.connect(B)
        self.seed(A,'a-only')
        self.seed(B)
        self.seed(LEGACY_ACCOUNT,'legacy')
        self.assertEqual([r['account_id'] for r in self.client.get('/emails').json()['emails']],[B])
        for key in ('a-only','legacy','missing'):
            self.assertEqual(self.client.post('/feedback',json={'email_id':key,'label':'IMPORTANT'}).status_code,404)
        response=self.client.post('/feedback',json={'email_id':'same','subject':'forged','body':'forged','label':'UPDATES'})
        self.assertEqual(response.status_code,202)
        self.assertEqual(self.client.post('/feedback/reconcile').json()['indexed'],1)
        rows=[r for r in self.collection.rows.values() if r[1]['account_id']==B]
        self.assertIn('canonical body',rows[0][0])
        self.assertNotIn('forged',rows[0][0])
        self.assertIsNone(get_email('a-only',account_id=A,db_path=self.settings.db_path)['human_label'])
        self.assertEqual(self.client.post('/feedback',json={'email_id':'same','label':'IGNORE'}).status_code,422)

    def test_vector_queries_filter_accounts_and_drop_unowned_results(self):
        for account in (A,B,LEGACY_ACCOUNT):
            self.seed(account)
        result=search_similar_emails('s','b',account_id=B,collection=self.collection)
        self.assertEqual(len(result),1)
        self.assertIn(B,result[0]['text'])
        self.assertEqual(self.collection.queries[-1],{'account_id':B})
        self.assertEqual(search_similar_emails('s','b',collection=self.collection),[])
        malicious=Mock()
        malicious.query.return_value={'documents':[['private A']], 'metadatas':[[{'account_id':A,'label':'IMPORTANT'}]],'distances':[[0.1]]}
        self.assertEqual(search_similar_emails('s','b',account_id=B,collection=malicious),[])
        self.assertIsNone(get_knn_prediction('s','b',account_id='empty@example.test',collection=self.collection))

    def test_switch_account_invalidates_old_context_and_other_browser(self):
        old=self.connect(A)
        token,csrf=self.manager.open_session()
        self.seed(A)
        self.seed(B)
        self.connect(B)
        with self.assertRaises(WorkCancelled):
            with self.manager.guard(old):
                self.fail('Old worker accepted')
        with self.assertRaises(AccessDenied):
            self.manager.session(token)
        self.assertEqual(self.client.get('/emails').json()['emails'][0]['account_id'],B)

    def test_logout_stops_work_but_keeps_mail_and_credentials(self):
        old=self.connect()
        self.seed(A)
        credential=self.manager.credential_path(A)
        self.assertEqual(self.client.post('/logout').status_code,200)
        self.assertIsNone(self.manager.worker_context())
        # AUTH-03: the login is parked (kept 24 h for a silent reconnect), not in use.
        self.assertFalse(credential.exists())
        self.assertTrue(self.manager.parked_credential_path(A).exists())
        self.assertIsNotNone(get_email('same',account_id=A,db_path=self.settings.db_path))
        with self.assertRaises(WorkCancelled):
            with AccountManager(self.settings).guard(old):
                self.fail('Old work survived logout')
        self.assertEqual(self.client.get('/emails').status_code,401)

    def test_late_oauth_cannot_save_after_logout(self):
        self.open_session()
        context,_=self.manager.session(self.client.cookies.get('mailmind_session'))
        attempt=self.manager.begin_auth(context)
        self.assertEqual(self.client.post('/logout').status_code,200)
        with self.assertRaises(WorkCancelled):
            self.manager.finish_auth(attempt,(A,'{}'))
        self.assertFalse(self.manager.credential_path(A).exists())
        self.assertIsNone(self.manager.worker_context())

    def test_disconnect_removes_only_current_credentials(self):
        self.connect(B)
        b_path=self.manager.credential_path(B)
        self.connect(A)
        self.seed(A)
        self.assertEqual(self.client.post('/disconnect').status_code,200)
        self.assertFalse(self.manager.credential_path(A).exists())
        self.assertTrue(b_path.exists())
        self.assertIsNotNone(get_email('same',account_id=A,db_path=self.settings.db_path))
        self.assertEqual(self.client.get('/session').status_code,200)
        self.assertIsNone(self.manager.worker_context())

    def test_purge_deletes_current_stores_preserves_other_and_legacy(self):
        self.connect()
        for account in (A,B,LEGACY_ACCOUNT):
            self.seed(account)
        self.assertEqual(self.client.delete('/account-data').status_code,200)
        self.assertIsNone(get_email('same',account_id=A,db_path=self.settings.db_path))
        self.assertIsNotNone(get_email('same',account_id=B,db_path=self.settings.db_path))
        self.assertIsNotNone(get_email('same',db_path=self.settings.db_path))
        self.assertFalse(any(r[1]['account_id']==A for r in self.collection.rows.values()))
        self.assertFalse(self.manager.credential_path(A).exists())
        self.assertIsNone(self.manager.worker_context())

    def test_failed_purge_is_paused_and_retryable(self):
        self.connect()
        self.seed(A)
        self.collection.fail_delete=True
        with self.assertLogs('mailmind',level='ERROR') as captured:
            response=self.client.delete('/account-data')
        self.assertEqual(response.status_code,503)
        self.assertNotIn('synthetic-private',response.text+' '.join(captured.output))
        self.assertTrue(self.client.get('/session').json()['purge_pending'])
        self.assertIsNone(self.manager.worker_context())
        self.assertEqual(self.client.post('/authenticate').status_code,409)
        self.collection.fail_delete=False
        self.assertEqual(self.client.delete('/account-data').status_code,200)
        self.assertFalse(self.client.get('/session').json()['purge_pending'])

    def test_legacy_purge_is_explicit_and_does_not_delete_owned_data(self):
        self.connect()
        self.seed(A)
        self.seed(LEGACY_ACCOUNT)
        self.collection.rows['old-no-owner']=('synthetic old text',None)
        self.settings.legacy_token_path.write_text('{}')
        with connection(self.settings.db_path) as conn:
            conn.execute('CREATE TABLE email_logs_legacy_v0(body TEXT)')
            conn.execute("INSERT INTO email_logs_legacy_v0 VALUES ('synthetic archive')")
        self.assertEqual(self.client.delete('/legacy-data').status_code,422)
        self.assertEqual(self.client.request('DELETE','/legacy-data',json={'confirmation':'DELETE_UNASSIGNED_DATA'}).status_code,200)
        self.assertEqual(len(self.collection.rows),1)
        self.assertIsNotNone(get_email('same',account_id=A,db_path=self.settings.db_path))
        self.assertFalse(self.settings.legacy_token_path.exists())
        with connection(self.settings.db_path) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='email_logs_legacy_v0'").fetchone())

    def test_restart_revokes_sessions_and_fences_old_work_but_keeps_google_connected(self):
        before=self.connect()
        token=self.client.cookies.get('mailmind_session')
        self.seed(A)
        with connection(self.settings.db_path) as conn:
            conn.execute("INSERT INTO worker_health(account_id,last_error_at,last_error_code) VALUES (?,?,?) ON CONFLICT(account_id) DO UPDATE SET last_error_at=excluded.last_error_at,last_error_code=excluded.last_error_code",(A,'2026-01-01T00:00:00Z','gmail_unavailable'))
            conn.execute("UPDATE runtime_state SET last_error_at=?,last_error_code=?",('2026-01-01T00:00:00Z','gmail_unavailable'))
        self.manager.restart()
        with self.assertRaises(AccessDenied):
            self.manager.session(token)
        # RESILIENCE-A: a restart no longer forces Connect Google again. Work from
        # before the restart is still fenced out by the new generation.
        after=self.manager.worker_context()
        self.assertIsNotNone(after)
        self.assertEqual((after.account_id,after.generation),(before.account_id,before.generation+1))
        with self.assertRaises(WorkCancelled):
            with self.manager.guard(before):
                pass
        self.assertIsNotNone(get_email('same',account_id=A,db_path=self.settings.db_path))
        with connection(self.settings.db_path) as conn:
            health=conn.execute('SELECT last_error_at,last_error_code FROM worker_health WHERE account_id=?',(A,)).fetchone()
            runtime=conn.execute('SELECT last_error_at,last_error_code FROM runtime_state').fetchone()
        self.assertEqual((health['last_error_at'],health['last_error_code']),(None,None))
        self.assertEqual((runtime['last_error_at'],runtime['last_error_code']),(None,None))

    def test_side_effect_lock_coordinates_independent_manager_instances(self):
        context=self.connect()
        other=AccountManager(self.settings)
        entered,release,stopped=threading.Event(),threading.Event(),threading.Event()
        errors=[]
        def effect():
            try:
                with other.guard(context):
                    entered.set()
                    release.wait(2)
            except Exception as error:
                errors.append(error)
        def stop():
            try:
                other.logout(context)
                stopped.set()
            except Exception as error:
                errors.append(error)
        worker=threading.Thread(target=effect)
        worker.start()
        self.assertTrue(entered.wait(2))
        stopper=threading.Thread(target=stop)
        stopper.start()
        self.assertFalse(stopped.wait(0.05))
        release.set()
        worker.join(2)
        stopper.join(2)
        self.assertEqual(errors,[])
        self.assertTrue(stopped.is_set())
        with self.assertRaises(WorkCancelled):
            with other.guard(context):
                pass

    def test_disconnected_worker_never_launches_google(self):
        from src import main
        with patch.object(main,'refresh_gmail') as refresh, patch('src.email_client.authenticate_gmail') as interactive:
            main._run_agent(settings=self.settings)
        refresh.assert_not_called()
        interactive.assert_not_called()

    def test_refresh_failure_never_opens_browser_or_recreates_token(self):
        from src import email_client
        context=self.connect()
        creds=Mock(valid=False,expired=True,refresh_token='synthetic')
        creds.refresh.side_effect=RuntimeError('synthetic-secret')
        path=self.manager.credential_path(A)
        original=path.read_text()
        with patch.object(email_client.Credentials,'from_authorized_user_file',return_value=creds), patch.object(email_client,'authenticate_gmail') as interactive, self.assertLogs('mailmind',level='ERROR'):
            self.assertIsNone(email_client.refresh_gmail(self.manager,context))
        interactive.assert_not_called()
        self.assertEqual(path.read_text(),original)

    def test_credential_refresh_is_bounded_before_gmail_profile_access(self):
        from src import email_client
        context=self.connect()
        creds=Mock(valid=False,expired=True,refresh_token='synthetic')
        with patch.object(email_client.Credentials,'from_authorized_user_file',return_value=creds), \
             patch.object(email_client.GMAIL_CREDENTIAL_CALLS,'run',side_effect=TimeoutError) as bounded, \
             patch.object(email_client,'build_gmail') as build, self.assertLogs('mailmind',level='ERROR'), \
             self.assertRaises(email_client.GmailUnavailable) as unavailable:
            email_client.refresh_gmail(self.manager,context)
        # A refresh that times out is a network problem, not a rejected login (NET-01).
        self.assertEqual(unavailable.exception.code,'network_unavailable')
        bounded.assert_called_once()
        self.assertEqual(bounded.call_args.args[1],self.settings.provider_timeout_seconds)
        build.assert_not_called()

    def test_logout_between_worker_steps_blocks_all_later_effects(self):
        from src import main
        self.connect()
        email={'id':'synthetic-worker','sender':'s','subject':'s','body':'b','body_snippet':'b'}
        def shadow(*args,**kwargs):
            return Prediction(category='IMPORTANT',outcome='CLASSIFIED'),None
        # Stop immediately after the classification lock is released.
        original_guard=self.manager.guard
        classified=False
        stopped=False
        def classify(*args, **kwargs):
            nonlocal classified
            classified=True
            return Prediction(category='IMPORTANT',outcome='CLASSIFIED')
        @contextmanager
        def guarded(context, **kwargs):
            nonlocal stopped
            with original_guard(context,**kwargs) as conn:
                yield conn
            if classified and not stopped:
                stopped=True
                self.manager.logout(context)
        with patch.object(main,'refresh_gmail',return_value=Mock()), patch.object(main,'get_unread_emails',return_value=FetchBatch(emails=[email])), \
             patch.object(main,'classify_email',side_effect=classify), patch.object(main,'shadow_evaluate_email',side_effect=shadow) as local, \
             patch.object(main,'log_email_to_db') as log, patch.object(main,'send_telegram_alert') as alert, patch.object(main,'mark_as_read') as read, \
             patch.object(self.manager,'guard',side_effect=guarded):
            main._run_agent(settings=self.settings,manager=self.manager,model=self.model,collection=self.collection)
        local.assert_not_called()
        log.assert_not_called()
        alert.assert_not_called()
        read.assert_not_called()
        self.assertIsNone(self.manager.worker_context())

    def test_logout_after_notification_blocks_gmail_mark_read(self):
        from src import main
        self.connect()
        notified=False
        stopped=False
        original_guard=self.manager.guard
        def notify(*args,**kwargs):
            nonlocal notified
            notified=True
            return True
        @contextmanager
        def guarded(context, **kwargs):
            nonlocal stopped
            with original_guard(context,**kwargs) as conn:
                yield conn
            if notified and not stopped:
                stopped=True
                self.manager.logout(context)
        email={'id':'synthetic-after-alert','sender':'s','subject':'s','body':'b','body_snippet':'b'}
        with patch.object(main,'refresh_gmail',return_value=Mock()), patch.object(main,'get_unread_emails',return_value=FetchBatch(emails=[email])), \
             patch.object(main,'classify_email',return_value=Prediction(category='IMPORTANT',outcome='CLASSIFIED')), patch.object(main,'send_telegram_alert',side_effect=notify), \
             patch.object(main,'mark_as_read') as read, patch.object(self.manager,'guard',side_effect=guarded):
            main._run_agent(settings=self.settings,manager=self.manager,model=self.model,collection=self.collection)
        read.assert_not_called()
        self.assertIsNotNone(get_email(email['id'],account_id=A,db_path=self.settings.db_path))

    def test_stale_refresh_cannot_recreate_deleted_credentials(self):
        from src import email_client
        context=self.connect()
        self.client.post('/disconnect')
        with patch.object(email_client.Credentials,'from_authorized_user_file') as loader:
            with self.assertRaises(WorkCancelled):
                email_client.refresh_gmail(self.manager,context)
        loader.assert_not_called()
        self.assertFalse(self.manager.credential_path(A).exists())

    def test_interactive_candidate_has_no_token_or_profile_side_effects(self):
        from src import email_client
        service=Mock()
        service.users.return_value.getProfile.return_value.execute.return_value={'emailAddress':A}
        flow=Mock();credentials=Mock()
        credentials.to_json.return_value='{}'
        with patch.object(email_client.InstalledAppFlow,'from_client_secrets_file',return_value=flow):
            with patch.object(email_client,'_run_oauth_with_closing_tab',return_value=credentials) as oauth:
                with patch.object(email_client,'build',return_value=service):
                    self.assertEqual(email_client.authenticate_gmail(),(A,'{}'))
        oauth.assert_called_once_with(flow)
        self.assertFalse(self.manager.credential_path(A).exists())
        self.assertFalse((self.settings.data_dir/'user_profile.json').exists())

    def test_failed_vector_startup_also_leaves_purge_pending(self):
        self.connect()
        def unavailable():
            raise RuntimeError('synthetic failure')
        context,_=self.manager.session(self.client.cookies.get('mailmind_session'))
        with self.assertRaises(RuntimeError):
            self.manager.purge(context,unavailable)
        self.assertIsNone(self.manager.worker_context())
        self.assertTrue(self.client.get('/session').json()['purge_pending'])

    def test_request_bounds_and_unicode_wrong_code(self):
        self.assertEqual(self.client.post('/session').status_code,403)
        self.connect()
        self.assertEqual(self.client.get('/emails?limit=0').status_code,422)
        self.assertEqual(self.client.get('/emails?limit=201').status_code,422)
        self.assertEqual(self.client.post('/predict',json={'subject':'s','body':'x'*100001}).status_code,422)

    def test_overlapping_worker_does_not_clear_running_cycle_flag(self):
        from src import main
        self.connect()
        with self.manager.transaction() as conn:
            conn.execute("UPDATE runtime_state SET is_polling=1,worker_token='synthetic-owner',lease_until=?",(__import__('time').time()+90,))
        with patch.object(main,'refresh_gmail') as refresh:
            main._run_agent(settings=self.settings)
        refresh.assert_not_called()
        self.assertTrue(self.client.get('/status').json()['is_polling'])

    def test_background_credential_failure_pauses_without_local_logout(self):
        from src import main
        self.connect()
        with patch.object(main,'refresh_gmail',return_value=None), patch.object(main,'get_unread_emails') as fetch:
            main._run_agent(settings=self.settings)
        fetch.assert_not_called()
        self.assertIsNone(self.manager.worker_context())
        response=self.client.get('/session')
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json()['connected'])

    def test_real_chroma_account_filter_and_delete_with_synthetic_embeddings(self):
        # No model download: supply tiny synthetic embeddings directly.
        # Windows releases Chroma's mapped files only when its process exits.
        code = '''
import sys
import chromadb
from chromadb.config import Settings as ChromaSettings
client=chromadb.PersistentClient(path=sys.argv[1],settings=ChromaSettings(anonymized_telemetry=False))
target=client.create_collection('synthetic-accounts',embedding_function=None)
target.upsert(ids=['a','b','legacy'],documents=['synthetic A','synthetic B','synthetic legacy'],
              embeddings=[[1.,0.],[1.,0.],[1.,0.]],
              metadatas=[{'account_id':'a@example.test','label':'IMPORTANT'},
                         {'account_id':'b@example.test','label':'SPAM'},{'label':'UPDATES'}])
result=target.query(query_embeddings=[[1.,0.]],n_results=3,where={'account_id':'b@example.test'})
assert result['ids']==[['b']]
target.delete(where={'account_id':'a@example.test'})
assert set(target.get(include=['metadatas'])['ids'])=={'b','legacy'}
client.delete_collection('synthetic-accounts')
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.settings.data_dir/'test-chroma')],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_purge_removes_histories_and_only_its_own_jobs(self):
        self.connect()
        self.seed(A)
        self.seed(B)
        with connection(self.settings.db_path) as conn:
            for account in (A,B):
                conn.execute("INSERT INTO worker_jobs(account_id,status,created_at,updated_at) VALUES (?,'queued','synthetic-time','synthetic-time')",(account,))
                conn.execute("INSERT INTO feedback_history(account_id,email_id,label,created_at) VALUES (?,'same','SPAM','synthetic-time')",(account,))
                conn.execute("INSERT INTO notification_attempts(account_id,email_id,status,created_at,updated_at) VALUES (?,'same','queued','synthetic-time','synthetic-time')",(account,))
        self.assertEqual(self.client.delete('/account-data').status_code,200)
        with connection(self.settings.db_path) as conn:
            for table in ('prediction_attempts','feedback_history','notification_attempts','worker_jobs'):
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {table} WHERE account_id=?',(A,)).fetchone()[0],0)
                self.assertGreater(conn.execute(f'SELECT count(*) FROM {table} WHERE account_id=?',(B,)).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT status FROM worker_jobs WHERE account_id=?',(B,)).fetchone()[0],'queued')

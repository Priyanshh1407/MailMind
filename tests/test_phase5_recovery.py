"""Offline recovery checks: fake providers, bounded waits, temporary SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import requests
import httpx
from fastapi.testclient import TestClient
from src import main, llm_api, notifier
from src.config import Settings
from src.database import connection,initialize_database,utc_timestamp,_migration_1,_migration_2,_migration_3,_migration_4,_migration_5
from src.account_state import AccountManager,WorkCancelled
from src.prediction import Prediction
from src.notifier import Delivery
from src.work_queue import enqueue_cycle,claim_cycle,fence,finish_cycle
from src.provider_policy import provider_failure,RequestBudget,BoundedCalls,retry_delay
from api.app import create_app
from scripts.wait_ready import wait_for_api
from tests.test_phase2_security import FakeCollection
from tests.test_phase3_ingestion import mailbox

A='phase5@example.test'
ORIGIN='http://localhost:5173'

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mailmind-phase5-')
        self.addCleanup(self.temp.cleanup)
        self.settings=Settings(data_dir=Path(self.temp.name),access_key='synthetic-phase5-pairing-code',auto_mark_read=True)
        self.collection=FakeCollection()
        self.model=Mock(model_loaded=False)
        self.model.predict.return_value=Prediction(reason='missing_checkpoint')
        self.app=create_app(settings=self.settings,model_factory=Mock(return_value=self.model),vector_factory=Mock(return_value=self.collection))
        self.client=TestClient(self.app,base_url='http://localhost')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.client.headers['Origin']=ORIGIN
        csrf=self.client.post('/session',json={'code':self.settings.access_key}).json()['csrf_token']
        self.client.headers['X-CSRF-Token']=csrf
        self.manager=self.app.state.accounts
        context,_=self.manager.session(self.client.cookies.get('mailmind_session'))
        self.manager.finish_auth(self.manager.begin_auth(context),(A,'{}'))
        self.context=self.manager.worker_context()
        self.service=mailbox(1)
        self.addCleanup(patch.stopall)
        patch.object(socket,'create_connection',side_effect=AssertionError('Network forbidden')).start()
        patch.object(socket,'getaddrinfo',side_effect=AssertionError('DNS forbidden')).start()

    def run_cycle(self,category='IMPORTANT',delivery=None,marker=None,classifier=None,manager=None,service=None):
        cloud=classifier or Mock(return_value=Prediction(category=category,outcome='CLASSIFIED',source='gemini',model_version='synthetic-v1',elapsed_ms=12))
        alert=Mock(return_value=delivery or Delivery('sent',message_id='42')) if delivery is None else Mock(return_value=delivery)
        with patch.object(main,'refresh_gmail',return_value=service or self.service),patch.object(main,'classify_email',cloud),patch.object(main,'send_telegram_alert',alert):
            if marker is None:
                result=main._run_agent(settings=self.settings,manager=manager or self.manager,model=self.model,collection=self.collection)
            else:
                with patch.object(main,'mark_as_read',marker):
                    result=main._run_agent(settings=self.settings,manager=manager or self.manager,model=self.model,collection=self.collection)
        return result,cloud,alert

    def task(self):
        with connection(self.settings.db_path) as conn:
            return dict(conn.execute('SELECT * FROM processing_tasks WHERE account_id=?',(A,)).fetchone())

    def outbox(self):
        with connection(self.settings.db_path) as conn:
            return dict(conn.execute('SELECT * FROM notification_outbox WHERE account_id=?',(A,)).fetchone())

    def due(self):
        with connection(self.settings.db_path) as conn:
            conn.execute('UPDATE processing_tasks SET next_retry_at=0 WHERE account_id=?',(A,))
            conn.execute('UPDATE notification_outbox SET next_retry_at=0 WHERE account_id=?',(A,))

    def test_success_records_delivery_before_read(self):
        marker=Mock(side_effect=lambda *args:self.outbox()['status']=='sent')
        self.run_cycle(marker=marker)
        self.assertEqual(self.outbox()['provider_message_id'],'42')
        self.assertEqual(self.task()['status'],'complete')
        marker.assert_called_once()
        rows=self.client.get('/emails').json()['emails']
        self.assertEqual(rows[0]['notification']['status'],'sent')
        self.assertEqual(rows[0]['prediction'],'IMPORTANT')

    def test_failed_alert_stays_unread_and_retryable(self):
        marker=Mock()
        result,_,_=self.run_cycle(delivery=Delivery('retry','notification_transient'),marker=marker)
        self.assertEqual(self.service.unread,['synthetic-0'])
        self.assertEqual((self.task()['stage'],self.task()['status']),('notify','retry'))
        self.assertEqual(self.outbox()['status'],'retry')
        self.assertEqual(result['processing_status'],'partial')
        marker.assert_not_called()

    def test_retry_alert_does_not_reclassify(self):
        self.run_cycle(delivery=Delivery('retry','notification_transient'))
        self.due()
        _,cloud,alert=self.run_cycle()
        cloud.assert_not_called()
        alert.assert_called_once()
        self.assertEqual(self.service.unread,[])
        self.assertEqual(self.task()['status'],'complete')
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM prediction_attempts').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_attempts').fetchone()[0],2)

    def test_repeated_cycle_never_resends_completed_alert(self):
        self.run_cycle()
        _,cloud,alert=self.run_cycle()
        cloud.assert_not_called()
        alert.assert_not_called()

    def test_mark_read_failure_retries_without_resending(self):
        marker=Mock(return_value=False)
        self.run_cycle(marker=marker)
        self.assertEqual(self.task()['stage'],'mark_read')
        self.assertEqual(self.outbox()['status'],'sent')
        self.due()
        _,cloud,alert=self.run_cycle()
        cloud.assert_not_called()
        alert.assert_not_called()
        self.assertEqual(self.service.unread,[])

    def test_ambiguous_mark_read_ack_is_safe_to_repeat(self):
        def lost_ack(service,identity):
            from src.email_client import mark_as_read
            mark_as_read(service,identity)
            return False
        self.run_cycle(marker=lost_ack)
        self.assertEqual(self.service.unread,[])
        self.due()
        _,cloud,alert=self.run_cycle(marker=Mock(return_value=True))
        cloud.assert_not_called()
        alert.assert_not_called()
        self.assertEqual(self.task()['status'],'complete')

    def test_unknown_delivery_is_not_automatically_resent(self):
        self.run_cycle(delivery=Delivery('unknown','provider_timeout'))
        self.assertEqual(self.outbox()['status'],'unknown')
        self.assertEqual(self.service.unread,['synthetic-0'])
        self.due()
        _,_,alert=self.run_cycle()
        alert.assert_not_called()
        self.assertEqual(self.task()['status'],'dead')

    def test_user_confirmation_finishes_read_without_resending(self):
        self.run_cycle(delivery=Delivery('unknown','provider_timeout'))
        response=self.client.post('/notifications/synthetic-0/resolve',json={'action':'confirmed_sent'})
        self.assertEqual(response.status_code,200)
        _,cloud,alert=self.run_cycle()
        cloud.assert_not_called()
        alert.assert_not_called()
        self.assertEqual(self.service.unread,[])
        self.assertEqual(self.outbox()['error_code'],'user_confirmed_sent')

    def test_explicit_unknown_retry_is_audited(self):
        self.run_cycle(delivery=Delivery('unknown','provider_timeout'))
        response=self.client.post('/notifications/synthetic-0/resolve',json={'action':'retry'})
        self.assertIn('duplicate',response.json()['message'])
        self.run_cycle()
        self.assertEqual(self.outbox()['status'],'sent')
        with connection(self.settings.db_path) as conn:
            self.assertTrue(conn.execute("SELECT 1 FROM processing_attempts WHERE error_code='user_resolution'").fetchone())

    def test_missing_configuration_does_not_mark_read(self):
        marker=Mock()
        self.run_cycle(delivery=Delivery('blocked','notification_unconfigured'),marker=marker)
        self.assertEqual(self.outbox()['status'],'blocked')
        marker.assert_not_called()
        self.assertEqual(self.service.unread,['synthetic-0'])

    def test_default_policy_preserves_unread_but_deduplicates(self):
        self.settings=replace(self.settings,auto_mark_read=False)
        self.manager.settings=self.settings
        self.run_cycle()
        self.assertEqual(self.service.unread,['synthetic-0'])
        self.assertEqual(self.task()['status'],'complete')
        _,cloud,alert=self.run_cycle()
        cloud.assert_not_called()
        alert.assert_not_called()

    def test_updates_do_not_need_notification(self):
        _,_,alert=self.run_cycle(category='UPDATES')
        alert.assert_not_called()
        self.assertEqual(self.service.unread,[])
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0],0)

    def test_read_policy_can_be_disabled_before_retry(self):
        self.run_cycle(marker=Mock(return_value=False))
        self.settings=replace(self.settings,auto_mark_read=False)
        self.manager.settings=self.settings
        self.due()
        marker=Mock()
        self.run_cycle(marker=marker)
        marker.assert_not_called()
        self.assertEqual(self.task()['status'],'complete')
        self.assertEqual(self.service.unread,['synthetic-0'])

    def test_pending_delivery_survives_worker_recreation(self):
        self.run_cycle(delivery=Delivery('retry','notification_transient'))
        self.due()
        other=AccountManager(self.settings)
        _,cloud,_=self.run_cycle(manager=other)
        cloud.assert_not_called()
        self.assertEqual(self.task()['status'],'complete')

    def test_saved_body_is_processed_even_if_inbox_is_now_empty(self):
        self.run_cycle(marker=Mock(return_value=False))
        self.due()
        self.run_cycle(service=mailbox(0),marker=Mock(return_value=True))
        self.assertEqual(self.task()['status'],'complete')

    def test_live_worker_lease_cannot_be_stolen(self):
        owner=claim_cycle(self.manager,self.context)
        self.assertIsNotNone(owner)
        self.assertIsNone(claim_cycle(AccountManager(self.settings),self.context))
        finish_cycle(self.manager,self.context,*owner)

    def test_expired_owner_cannot_commit_or_clear_new_owner(self):
        old=claim_cycle(self.manager,self.context)
        with connection(self.settings.db_path) as conn:
            conn.execute('UPDATE runtime_state SET lease_until=0')
        new=claim_cycle(AccountManager(self.settings),self.context)
        with self.assertRaises(WorkCancelled):
            with self.manager.guard(self.context) as conn:
                fence(self.manager,self.context,old[0],conn)
        finish_cycle(self.manager,self.context,*old)
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT worker_token FROM runtime_state').fetchone()[0],new[0])
        finish_cycle(self.manager,self.context,*new)

    def test_simultaneous_claims_have_one_winner(self):
        with ThreadPoolExecutor(2) as executor:
            results=list(executor.map(lambda _:claim_cycle(AccountManager(self.settings),self.context),range(2)))
        self.assertEqual(sum(result is not None for result in results),1)
        finish_cycle(self.manager,self.context,*next(result for result in results if result))

    def test_queued_processing_job_is_trackable_and_deduplicated(self):
        first=self.client.post('/process')
        second=self.client.post('/process')
        self.assertEqual(first.status_code,202)
        self.assertEqual(first.json()['job_id'],second.json()['job_id'])
        self.assertEqual(self.client.get('/jobs/'+str(first.json()['job_id'])).json()['status'],'queued')
        self.run_cycle()
        self.assertEqual(self.client.get('/jobs/'+str(first.json()['job_id'])).json()['status'],'complete')

    def test_recovery_endpoints_reject_unknown_ids_and_bad_actions(self):
        self.assertEqual(self.client.get('/jobs/not-owned').status_code,404)
        self.assertEqual(self.client.post('/notifications/missing/resolve',json={'action':'retry'}).status_code,404)
        self.assertEqual(self.client.post('/tasks/missing/retry').status_code,404)
        self.assertEqual(self.client.post('/notifications/x/resolve',json={'action':'anything'}).status_code,422)

    def test_alert_resolution_rejects_already_sent(self):
        self.run_cycle()
        self.assertEqual(self.client.post('/notifications/synthetic-0/resolve',json={'action':'retry'}).status_code,409)
        self.assertEqual(self.client.post('/tasks/synthetic-0/retry').status_code,409)

    def test_generic_task_retry_cannot_blindly_repeat_unknown_alert(self):
        self.run_cycle(delivery=Delivery('unknown','provider_timeout'))
        self.assertEqual(self.client.post('/tasks/synthetic-0/retry').status_code,409)

    def test_notification_retries_are_bounded(self):
        for _ in range(self.settings.max_processing_attempts):
            self.due()
            self.run_cycle(delivery=Delivery('retry','notification_transient'))
        self.assertEqual((self.task()['status'],self.outbox()['status']),('dead','dead'))
        _,_,alert=self.run_cycle()
        alert.assert_not_called()

    def test_one_bad_classification_does_not_block_good_message(self):
        self.service=mailbox(2)
        cloud=Mock(side_effect=[Prediction(outcome='ERROR',reason='invalid_provider_output'),Prediction(category='UPDATES',outcome='CLASSIFIED')])
        self.run_cycle(classifier=cloud)
        self.assertEqual(self.service.unread,['synthetic-0'])
        with connection(self.settings.db_path) as conn:
            rows={row['email_id']:row['status'] for row in conn.execute('SELECT email_id,status FROM processing_tasks')}
        self.assertEqual(rows,{'synthetic-0':'dead','synthetic-1':'complete'})

    def test_transient_classification_has_backoff_and_attempt_limit(self):
        cloud=Mock(return_value=Prediction(outcome='ERROR',reason='provider_transient'))
        self.run_cycle(classifier=cloud)
        self.assertGreater(self.task()['next_retry_at'],time.time())
        for _ in range(2):
            self.due()
            self.run_cycle(classifier=cloud)
        self.assertEqual(self.task()['status'],'dead')
        self.assertEqual(self.task()['attempt_count'],3)

    def test_dead_classification_can_be_explicitly_retried(self):
        self.run_cycle(classifier=Mock(return_value=Prediction(outcome='ERROR',reason='invalid_provider_output')))
        self.assertEqual(self.client.post('/tasks/synthetic-0/retry').status_code,200)
        self.run_cycle(category='UPDATES')
        self.assertEqual(self.task()['status'],'complete')

    def test_parse_failures_are_identified_and_quarantined(self):
        self.service=mailbox(2)
        self.service.items['synthetic-0']['payload']['body']['data']='a'
        self.run_cycle(category='UPDATES')
        for _ in range(2):
            with connection(self.settings.db_path) as conn:
                conn.execute('UPDATE ingestion_failures SET next_retry_at=0')
            self.run_cycle(category='UPDATES')
        with connection(self.settings.db_path) as conn:
            row=conn.execute('SELECT * FROM ingestion_failures').fetchone()
            self.assertEqual((row['email_id'],row['status'],row['attempt_count']),('synthetic-0','dead',3))
        self.assertEqual(self.service.unread,['synthetic-0'])
        failure=self.client.get('/status').json()['ingestion_failures'][0]
        self.assertEqual((failure['email_id'],failure['status'],failure['attempt_count']),('synthetic-0','dead',3))
        self.assertEqual(self.client.post('/tasks/synthetic-0/retry').status_code,200)
        self.assertEqual(self.client.get('/status').json()['ingestion_failures'],[])

    def test_crashed_send_becomes_unknown_instead_of_resending(self):
        self.run_cycle(delivery=Delivery('retry','notification_transient'))
        with connection(self.settings.db_path) as conn:
            conn.execute("UPDATE processing_tasks SET status='running',next_retry_at=0")
            conn.execute("UPDATE notification_outbox SET status='sending'")
            conn.execute("UPDATE runtime_state SET worker_token='dead-process',lease_until=0,is_polling=1")
        _,_,alert=self.run_cycle()
        alert.assert_not_called()
        self.assertEqual(self.outbox()['status'],'unknown')
        self.assertEqual(self.service.unread,['synthetic-0'])

    def test_slow_prediction_does_not_block_status_or_logout(self):
        entered,release=threading.Event(),threading.Event()
        def slow(*args,**kwargs):
            entered.set()
            release.wait(3)
            return Prediction(category='IMPORTANT',outcome='CLASSIFIED')
        executor=ThreadPoolExecutor(1)
        with patch.object(main,'refresh_gmail',return_value=self.service),patch.object(main,'classify_email',side_effect=slow),patch.object(main,'send_telegram_alert') as alert,patch.object(main,'mark_as_read') as marker:
            future=executor.submit(main._run_agent,settings=self.settings,manager=self.manager,model=self.model,collection=self.collection)
            try:
                self.assertTrue(entered.wait(2))
                start=time.monotonic()
                self.assertEqual(self.client.get('/status').status_code,200)
                self.assertEqual(self.client.post('/logout').status_code,200)
                self.assertLess(time.monotonic()-start,1)
            finally:
                release.set()
                result=future.result(timeout=3)
                executor.shutdown(wait=True)
            self.assertEqual(result['status'],'cancelled')
            alert.assert_not_called()
            marker.assert_not_called()

    def test_health_and_job_error_are_persisted(self):
        self.run_cycle(delivery=Delivery('retry','notification_transient'))
        status=self.client.get('/status').json()
        self.assertIsNotNone(status['worker']['heartbeat_at'])
        self.assertEqual(status['worker']['last_error_code'],'processing_incomplete')
        self.assertEqual(status['notification_counts']['retry'],1)
        self.assertFalse(status['is_polling'])

    def test_finally_clears_owner_when_fetch_raises(self):
        with patch.object(main,'refresh_gmail',return_value=self.service),patch.object(main,'get_unread_emails',side_effect=RuntimeError('synthetic secret')):
            with self.assertRaises(RuntimeError):
                main._run_agent(settings=self.settings,manager=self.manager,model=self.model,collection=self.collection)
        self.assertFalse(self.client.get('/status').json()['is_polling'])
        with connection(self.settings.db_path) as conn:
            self.assertIsNone(conn.execute('SELECT worker_token FROM runtime_state').fetchone()[0])

    def test_account_purge_cascades_tasks_and_outbox(self):
        self.run_cycle(delivery=Delivery('retry','notification_transient'))
        self.assertEqual(self.client.delete('/account-data').status_code,200)
        with connection(self.settings.db_path) as conn:
            for table in ('processing_tasks','processing_attempts','notification_outbox','ingestion_failures','worker_health'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_other_account_cannot_resolve_alert_or_read_job(self):
        result,_,_=self.run_cycle(delivery=Delivery('unknown','provider_timeout'))
        context,_=self.manager.session(self.client.cookies.get('mailmind_session'))
        self.manager.finish_auth(self.manager.begin_auth(context),('other@example.test','{}'))
        self.assertEqual(self.client.post('/notifications/synthetic-0/resolve',json={'action':'retry'}).status_code,404)
        self.assertEqual(self.client.get('/jobs/'+str(result['job_id'])).status_code,404)

    def test_version_five_migration_preserves_saved_history(self):
        path=self.settings.data_dir/'version5.db'
        with connection(path) as conn:
            for migration in (_migration_1,_migration_2,_migration_3,_migration_4,_migration_5):
                migration(conn)
            conn.execute('PRAGMA user_version=5')
            conn.execute("INSERT INTO email_logs(email_id,body,prediction,created_at) VALUES ('old','original body','IMPORTANT',?)",(utc_timestamp(),))
            conn.execute("INSERT INTO feedback_history(account_id,email_id,label,created_at) VALUES ('legacy-unassigned','old','UPDATES',?)",(utc_timestamp(),))
        initialize_database(path)
        initialize_database(path)
        with connection(path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],7)
            self.assertEqual(tuple(conn.execute('SELECT body,prediction FROM email_logs').fetchone()),('original body','IMPORTANT'))
            self.assertEqual(conn.execute('SELECT label FROM feedback_history').fetchone()[0],'UPDATES')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM processing_tasks').fetchone()[0],0)

    def test_index_timeout_keeps_purge_pending_until_actual_write_finishes(self):
        from src.db_utils import log_email_to_db
        log_email_to_db('synthetic-index','s','s','b','SPAM','SPAM',account_id=A,db_path=self.settings.db_path)
        release,entered,ended=threading.Event(),threading.Event(),threading.Event()
        original=self.collection.upsert
        def slow_upsert(*args,**kwargs):
            entered.set()
            release.wait(10)
            try:
                return original(*args,**kwargs)
            finally:
                ended.set()
        with patch.object(self.collection,'upsert',side_effect=slow_upsert):
            try:
                response=self.client.post('/feedback',json={'email_id':'synthetic-index','label':'IMPORTANT'})
                self.assertEqual(response.status_code,202)
                self.assertFalse(entered.is_set())
                with ThreadPoolExecutor(max_workers=1) as executor:
                    reconcile=executor.submit(self.client.post,'/feedback/reconcile')
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(reconcile.result(timeout=3).status_code,200)
                start=time.monotonic()
                self.assertEqual(self.client.get('/status').status_code,200)
                self.assertLess(time.monotonic()-start,1)
                self.assertEqual(self.client.delete('/account-data').status_code,503)
                self.assertTrue(self.client.get('/status').json()['purge_pending'])
            finally:
                release.set()
                self.assertTrue(ended.wait(2))
        self.assertEqual(self.client.delete('/account-data').status_code,200)
        self.assertEqual(self.collection.rows,{})


class AuthenticationJobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mailmind-phase5-auth-')
        self.addCleanup(self.temp.cleanup)
        self.settings=Settings(data_dir=Path(self.temp.name),access_key='synthetic-background-pairing-key')
        self.release,self.entered=threading.Event(),threading.Event()
        def sign_in():
            self.entered.set()
            self.release.wait(3)
            return A,'{}'
        self.oauth=Mock(side_effect=sign_in)
        self.app=create_app(settings=self.settings,model_factory=Mock(return_value=Mock(model_loaded=False)),
            vector_factory=Mock(return_value=FakeCollection()),oauth_factory=self.oauth)
        self.client=TestClient(self.app,base_url='http://localhost')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.addCleanup(self.finish)
        self.pair()

    def finish(self):
        self.release.set()
        for future in list(self.app.state.auth_futures.values()):
            if not future.cancelled():
                future.result(timeout=4)

    def pair(self):
        result=self.client.post('/session',json={'code':self.settings.access_key},headers={'Origin':ORIGIN})
        self.client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':result.json()['csrf_token']})

    def test_sign_in_returns_trackable_job_without_waiting_for_google(self):
        with patch.object(main,'_run_agent') as processing:
            start=time.monotonic()
            response=self.client.post('/authenticate')
            self.assertLess(time.monotonic()-start,1)
            self.assertEqual(response.status_code,202)
            job_id=response.json()['job_id']
            self.assertEqual(self.client.get('/jobs/'+job_id).json()['status'],'running')
            self.assertTrue(self.client.get('/status').json()['auth_in_progress'])
            self.finish()
            job=self.client.get('/jobs/'+job_id).json()
            self.assertEqual(job['status'],'complete')
            self.assertIsNotNone(job['processing_job_id'])
            self.assertEqual(self.client.get('/jobs/'+str(job['processing_job_id'])).json()['status'],'queued')
            processing.assert_not_called()

    def test_logout_during_google_flow_discards_late_credentials(self):
        response=self.client.post('/authenticate')
        self.assertTrue(self.entered.wait(1))
        start=time.monotonic()
        self.assertEqual(self.client.post('/logout').status_code,200)
        self.assertLess(time.monotonic()-start,1)
        self.finish()
        manager=self.app.state.accounts
        self.assertFalse(manager.credential_path(A).exists())
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM auth_jobs WHERE job_id=?',(response.json()['job_id'],)).fetchone()[0],'cancelled')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM worker_jobs').fetchone()[0],0)

    def test_failed_sign_in_has_safe_persisted_status(self):
        self.oauth.side_effect=RuntimeError('synthetic private email/token')
        response=self.client.post('/authenticate')
        self.finish()
        job=self.client.get('/jobs/'+response.json()['job_id']).json()
        self.assertEqual((job['status'],job['error_code']),('failed','sign_in_failed'))
        self.assertNotIn('private',json.dumps(job))
        self.assertFalse(self.client.get('/status').json()['auth_in_progress'])

    def test_auth_jobs_are_not_visible_after_session_change(self):
        response=self.client.post('/authenticate')
        self.client.post('/logout')
        self.pair()
        self.assertEqual(self.client.get('/jobs/'+response.json()['job_id']).status_code,404)
        self.finish()


class ProviderAndReadinessTests(unittest.TestCase):
    def test_typed_failures_ignore_error_wording(self):
        error=RuntimeError('secret quota 429')
        self.assertEqual(provider_failure(error).code,'provider_failed')
        for status,code,retry in [(429,'provider_quota',True),(503,'provider_transient',True),(401,'provider_auth',False),(400,'provider_invalid_request',False)]:
            error.code=status
            failure=provider_failure(error)
            self.assertEqual((failure.code,failure.retryable),(code,retry))

    def test_timeout_types_distinguish_ambiguous_delivery(self):
        self.assertFalse(provider_failure(requests.exceptions.ConnectTimeout()).ambiguous)
        self.assertTrue(provider_failure(requests.exceptions.ReadTimeout()).ambiguous)
        self.assertTrue(provider_failure(httpx.ReadTimeout('synthetic')).ambiguous)

    def test_request_budget_expires_and_caps_timeout(self):
        now=[10.]
        budget=RequestBudget(3,clock=lambda:now[0])
        self.assertEqual(budget.timeout(5),3)
        now[0]=13.
        with self.assertRaises(TimeoutError):
            budget.timeout(5)

    def test_retry_delay_is_bounded_exponential(self):
        self.assertEqual([retry_delay(n) for n in (1,2,3,20)],[5,10,20,300])

    def test_fixed_pool_bounds_wait_and_does_not_spawn_more_workers(self):
        pool=BoundedCalls(1)
        release,ended=threading.Event(),threading.Event()
        def blocked():
            release.wait(2)
            ended.set()
        try:
            start=time.monotonic()
            with self.assertRaises(TimeoutError):pool.run(blocked,0.02)
            with self.assertRaises(TimeoutError):pool.run(lambda:None,0.02)
            self.assertLess(time.monotonic()-start,0.5)
        finally:
            release.set()
            self.assertTrue(ended.wait(1))

    def test_plain_text_telegram_accepts_special_characters(self):
        response=Mock(status_code=200)
        response.json.return_value={'ok':True,'result':{'message_id':123}}
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'123456:synthetic-token','TELEGRAM_CHAT_ID':'123'},clear=True),patch.object(notifier.requests,'post',return_value=response) as post:
            result=notifier.send_telegram_alert('A_*[]','Subject [link](fake)','<tag> *text*')
        self.assertEqual(result.status,'sent')
        self.assertNotIn('parse_mode',post.call_args.kwargs['json'])
        self.assertIn('[SENDER]',post.call_args.kwargs['json']['text'])
        self.assertNotIn('A_*[]',post.call_args.kwargs['json']['text'])

    def test_http_success_without_acknowledgement_is_unknown(self):
        response=Mock(status_code=200)
        response.json.return_value={'ok':True,'result':{}}
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'123456:synthetic-token','TELEGRAM_CHAT_ID':'123'},clear=True),patch.object(notifier.requests,'post',return_value=response):
            self.assertEqual(notifier.send_telegram_alert('s','s','s').status,'unknown')

    def test_telegram_timeout_is_unknown_and_quota_has_retry_after(self):
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'123456:synthetic-token','TELEGRAM_CHAT_ID':'123'},clear=True),patch.object(notifier.requests,'post',side_effect=requests.exceptions.ReadTimeout('synthetic')):
            self.assertEqual(notifier.send_telegram_alert('s','s','s').status,'unknown')
        response=Mock(status_code=429)
        response.json.return_value={'ok':False,'error_code':429,'parameters':{'retry_after':30}}
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'123456:synthetic-token','TELEGRAM_CHAT_ID':'123'},clear=True),patch.object(notifier.requests,'post',return_value=response):
            result=notifier.send_telegram_alert('s','s','s')
        self.assertEqual((result.status,result.retry_after),('retry',30))

    def test_unconfigured_telegram_makes_no_request(self):
        with patch.dict(os.environ,{},clear=True),patch.object(notifier.requests,'post') as post:
            self.assertEqual(notifier.send_telegram_alert('s','s','s').status,'blocked')
        post.assert_not_called()

    def test_permanent_cloud_error_does_not_failover(self):
        client=Mock()
        error=RuntimeError('synthetic private text')
        error.code=401
        client.models.generate_content.side_effect=error
        with patch.object(llm_api,'get_client',return_value=client):
            result=llm_api.classify_email('s','s','b')
        self.assertEqual(result.reason,'provider_auth')
        self.assertEqual(client.models.generate_content.call_count,1)

    def test_transient_cloud_failure_uses_one_bounded_fallback(self):
        client=Mock()
        error=RuntimeError('synthetic')
        error.code=503
        client.models.generate_content.side_effect=[error,Mock(text='{"category":"UPDATES"}')]
        with patch.object(llm_api,'get_client',return_value=client):
            result=llm_api.classify_email('s','s','b')
        self.assertEqual(result.model_version,'gemini-3.5-flash-lite')
        for call in client.models.generate_content.call_args_list:
            self.assertNotIn('http_options',call.kwargs['config'])

    def test_readiness_retries_until_local_api_is_ready(self):
        now=[0.]
        response=Mock(status=200)
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=None)
        response.read.return_value=b'{"message":"MailMind API is online.","model_loaded":false}'
        opener=Mock(side_effect=[OSError('not ready'),response])
        ready=wait_for_api(timeout=2,opener=opener,clock=lambda:now[0],sleeper=lambda delay:now.__setitem__(0,now[0]+delay))
        self.assertTrue(ready)
        self.assertEqual(opener.call_count,2)

    def test_readiness_failure_has_a_deadline(self):
        now=[0.]
        result=wait_for_api(timeout=1,opener=Mock(side_effect=OSError()),clock=lambda:now[0],sleeper=lambda delay:now.__setitem__(0,now[0]+delay))
        self.assertFalse(result)
        self.assertEqual(now[0],1)

    def test_readiness_rejects_unrelated_server_and_nonlocal_url(self):
        now=[0.]
        response=Mock(status=200)
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=None)
        response.read.return_value=b'{"message":"Other server"}'
        self.assertFalse(wait_for_api(timeout=1,opener=Mock(return_value=response),clock=lambda:now[0],sleeper=lambda delay:now.__setitem__(0,now[0]+delay)))
        with self.assertRaises(ValueError):wait_for_api('https://example.test/')

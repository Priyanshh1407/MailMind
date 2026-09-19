"""Account-safe dashboard queries and feedback undo; synthetic data only."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from api.app import create_app
from src.config import Settings
from src.database import connection, initialize_database, utc_timestamp
from src.db_utils import log_email_to_db
from src.feedback import current_vector, reconcile_feedback
from src.prediction import Prediction
from src import database
from tests.test_phase2_security import FakeCollection

A, B = 'phase6@example.test', 'other-phase6@example.test'

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mailmind-phase6-')
        self.addCleanup(self.temp.cleanup)
        self.settings=Settings(data_dir=Path(self.temp.name),access_key='synthetic-phase6-pairing-code')
        self.collection=FakeCollection()
        self.model=Mock(model_loaded=False,model_version='legacy-binary')
        self.app=create_app(settings=self.settings,model_factory=Mock(return_value=self.model),vector_factory=Mock(return_value=self.collection))
        self.client=TestClient(self.app,base_url='http://localhost')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.client.headers['Origin']='http://localhost:5173'
        csrf=self.client.post('/session',json={'code':self.settings.access_key}).json()['csrf_token']
        self.client.headers['X-CSRF-Token']=csrf
        self.manager=self.app.state.accounts
        self.connect(A)

    def connect(self,account):
        context,_=self.manager.session(self.client.cookies.get('mailmind_session'))
        self.manager.finish_auth(self.manager.begin_auth(context),(account,'{}'))

    def mail(self,identity='mail-0',category='IMPORTANT',account=A,subject=None,body='Synthetic body',elapsed=25):
        log_email_to_db(identity,'Sender <synthetic@example.test>',subject or identity,body,
            Prediction(category=category,outcome='CLASSIFIED' if category else 'ERROR',source='synthetic',elapsed_ms=elapsed),
            Prediction(category='SPAM',outcome='CLASSIFIED'),account_id=account,db_path=self.settings.db_path)

    def feedback(self,identity='mail-0',label='UPDATES',expected=None):
        payload={'email_id':identity,'label':label}
        if expected is not None: payload['expected_revision_id']=expected
        return self.client.post('/feedback',json=payload)

    def test_pagination_counts_all_rows_not_the_current_page(self):
        for i in range(25): self.mail(f'mail-{i:02d}')
        first=self.client.get('/emails?limit=20').json()
        second=self.client.get('/emails?limit=20&offset=20').json()
        self.assertEqual((len(first['emails']),first['total'],first['has_more']),(20,25,True))
        self.assertEqual((len(second['emails']),second['total'],second['has_more']),(5,25,False))
        self.assertFalse({row['id'] for row in first['emails']} & {row['id'] for row in second['emails']})
        self.assertEqual(self.client.get('/telemetry').json()['totals']['saved'],25)

    def test_equal_timestamps_have_stable_page_order(self):
        for i in range(6): self.mail(f'tie-{i}')
        with connection(self.settings.db_path) as conn: conn.execute("UPDATE email_logs SET created_at='2026-09-18T00:00:00.000000Z'")
        self.assertEqual([e['id'] for e in self.client.get('/emails?limit=2&offset=2').json()['emails']],['tie-2','tie-3'])

    def test_search_sender_subject_and_body(self):
        self.mail(subject='Budget review',body='A blue bicycle')
        for search in ['budget','BICYCLE','synthetic@example.test']:
            self.assertEqual(self.client.get('/emails',params={'search':search}).json()['total'],1)
        self.assertEqual(self.client.get('/emails?search=missing').json()['total'],0)

    def test_search_wildcards_are_literal(self):
        self.mail('literal',subject='100% progress_underway')
        self.mail('other',subject='1000 progressXunderway')
        for search in ['%','_']:
            rows=self.client.get('/emails',params={'search':search}).json()['emails']
            self.assertEqual([row['id'] for row in rows],['literal'])

    def test_search_quotes_are_parameters(self):
        self.mail()
        self.assertEqual(self.client.get('/emails',params={'search':"' OR 1=1 --"}).json()['total'],0)
        self.assertEqual(self.client.get('/emails').json()['total'],1)

    def test_category_filter_uses_current_feedback(self):
        self.mail()
        self.feedback()
        self.assertEqual(self.client.get('/emails?category=IMPORTANT').json()['total'],0)
        row=self.client.get('/emails?category=UPDATES').json()['emails'][0]
        self.assertEqual((row['prediction'],row['effective_category']),('IMPORTANT','UPDATES'))

    def test_needs_review_does_not_restore_an_old_category(self):
        self.mail()
        self.mail(category=None)
        row=self.client.get('/emails?category=NEEDS_REVIEW').json()['emails'][0]
        self.assertEqual(row['prediction'],'IMPORTANT')
        self.assertIsNone(row['effective_category'])
        self.assertEqual(self.client.get('/emails?category=IMPORTANT').json()['total'],0)

    def test_bad_query_parameters_are_rejected(self):
        for query in ['limit=0','limit=201','offset=-1','offset=1000001','category=BLOCKED','search='+('a'*201)]:
            self.assertEqual(self.client.get('/emails?'+query).status_code,422)

    def test_totals_separate_saved_completed_feedback_and_corrections(self):
        self.mail('confirmed'); self.mail('corrected'); self.mail('no-original',category=None)
        self.feedback('confirmed','IMPORTANT'); self.feedback('corrected','SPAM'); self.feedback('no-original','UPDATES')
        with connection(self.settings.db_path) as conn: conn.execute("UPDATE email_logs SET processing_state='complete' WHERE email_id='confirmed'")
        totals=self.client.get('/telemetry').json()['totals']
        self.assertEqual(totals,{'saved':3,'completed':1,'labelled':3,'corrected':1,'confirmed':1,'feedback_events':3,'prediction_attempts':3})

    def test_measured_latency_uses_actual_samples(self):
        self.mail('one',elapsed=20); self.mail('two',elapsed=40)
        timing=self.client.get('/telemetry').json()['classification_timing']
        self.assertEqual((timing['mean_ms'],timing['sample_count']),(30,2))

    def test_no_measurements_report_null_not_a_fake_number(self):
        self.assertEqual(self.client.get('/telemetry').json()['classification_timing']['mean_ms'],None)

    def test_latency_window_is_bounded_to_fifty_attempts(self):
        for i in range(52): self.mail(str(i),elapsed=i)
        timing=self.client.get('/telemetry').json()['classification_timing']
        self.assertEqual((timing['sample_count'],timing['mean_ms']),(50,26.5))

    def test_bad_timing_metadata_is_ignored(self):
        self.mail()
        for value in [True,-1,float('nan'),'25',None]:
            with connection(self.settings.db_path) as conn: conn.execute('UPDATE prediction_attempts SET metadata=?',(json.dumps({'elapsed_ms':value}),))
            self.assertEqual(self.client.get('/telemetry').json()['classification_timing']['sample_count'],0)

    def test_provider_configuration_does_not_claim_online(self):
        with patch.dict('os.environ',{'GEMINI_API_KEY':'synthetic','GROQ_API_KEY':''}):
            data=self.client.get('/telemetry').json()
        self.assertEqual(data['providers'],{'gemini':'configured_unverified','groq':'unconfigured'})
        self.assertEqual(data['local_model'],{'ready':False,'status':'unavailable','version':'legacy-binary','evaluation_scope':None})

    def test_model_ready_flag_comes_from_the_loaded_service(self):
        self.model.model_loaded=True
        self.assertTrue(self.client.get('/telemetry').json()['local_model']['ready'])

    def test_feedback_can_be_edited_without_changing_predictions(self):
        self.mail()
        first=self.feedback(expected=0).json()
        second=self.feedback(label='SPAM',expected=first['revision_id']).json()
        self.assertGreater(second['revision_id'],first['revision_id'])
        row=self.client.get('/emails').json()['emails'][0]
        self.assertEqual((row['prediction'],row['local_prediction'],row['effective_category']),('IMPORTANT','SPAM','SPAM'))
        self.assertEqual(len(self.client.get('/emails/mail-0/history').json()['feedback']),2)

    def test_stale_feedback_edit_is_rejected(self):
        self.mail(); self.feedback(expected=0)
        self.assertEqual(self.feedback(label='SPAM',expected=0).status_code,409)
        self.assertEqual(self.client.get('/emails').json()['emails'][0]['human_label'],'UPDATES')

    def test_undo_restores_latest_prediction_and_keeps_history(self):
        self.mail(); revision=self.feedback().json()['revision_id']
        response=self.client.delete('/feedback/mail-0',params={'expected_revision_id':revision})
        self.assertEqual(response.status_code,202)
        row=self.client.get('/emails').json()['emails'][0]
        self.assertEqual((row['human_label'],row['effective_category']),(None,'IMPORTANT'))
        history=self.client.get('/emails/mail-0/history').json()['feedback']
        self.assertEqual([entry['label'] for entry in history],[None,'UPDATES'])
        self.assertEqual(self.client.get('/telemetry').json()['totals']['labelled'],0)

    def test_undo_removes_only_that_messages_vector(self):
        self.mail('one');self.mail('two');self.feedback('one');self.feedback('two')
        self.client.post('/feedback/reconcile')
        self.client.delete('/feedback/one')
        self.client.post('/feedback/reconcile')
        self.assertEqual([meta['email_id'] for _,meta in self.collection.rows.values()],['two'])

    def test_failed_undo_index_cleanup_is_saved_and_retryable(self):
        self.mail();self.feedback();self.client.post('/feedback/reconcile')
        oldmeta=next(iter(self.collection.rows.values()))[1]
        self.collection.fail_delete=True
        self.assertEqual(self.client.delete('/feedback/mail-0').status_code,202)
        self.client.post('/feedback/reconcile')
        self.assertFalse(current_vector(oldmeta,self.settings.db_path))
        self.assertIsNone(self.client.get('/emails').json()['emails'][0]['human_label'])
        self.collection.fail_delete=False
        reconcile_feedback(self.manager,self.manager.worker_context(),lambda:self.collection)
        self.assertEqual(self.collection.rows,{})

    def test_stale_undo_is_rejected(self):
        self.mail();first=self.feedback().json()['revision_id'];self.feedback(label='SPAM')
        self.assertEqual(self.client.delete('/feedback/mail-0',params={'expected_revision_id':first}).status_code,409)

    def test_undo_without_feedback_and_missing_mail_are_distinct(self):
        self.mail()
        self.assertEqual(self.client.delete('/feedback/mail-0').status_code,409)
        self.assertEqual(self.client.delete('/feedback/missing').status_code,404)

    def test_can_add_feedback_after_undo(self):
        self.mail();self.feedback();revision=self.client.delete('/feedback/mail-0').json()['revision_id']
        self.assertEqual(self.feedback(label='SPAM',expected=revision).status_code,202)
        self.assertEqual(self.client.get('/emails').json()['emails'][0]['human_label'],'SPAM')

    def test_history_contains_saved_processing_steps(self):
        self.mail()
        with connection(self.settings.db_path) as conn:
            conn.execute("INSERT INTO processing_attempts(account_id,email_id,stage,outcome,created_at) VALUES (?,?,'notify','sent',?)",(A,'mail-0',utc_timestamp()))
        self.assertEqual(self.client.get('/emails/mail-0/history').json()['processing'][0]['outcome'],'sent')

    def test_other_account_is_excluded_from_totals_search_and_history(self):
        self.mail('a');self.mail('b',account=B)
        self.assertEqual(self.client.get('/telemetry').json()['totals']['saved'],1)
        self.assertEqual(self.client.get('/emails?search=b').json()['total'],1) # Synthetic body matches both, but only own row returned.
        self.assertEqual(self.client.get('/emails/b/history').status_code,404)
        self.assertEqual(self.client.delete('/feedback/b').status_code,404)
        self.connect(B)
        self.assertEqual([e['id'] for e in self.client.get('/emails').json()['emails']],['b'])
        self.assertEqual(self.client.get('/emails/a/history').status_code,404)

    def test_private_telemetry_and_history_require_session(self):
        self.mail();self.client.post('/logout')
        for route in ['/telemetry','/emails/mail-0/history','/emails']:
            self.assertEqual(self.client.get(route).status_code,401)

    def test_undo_requires_csrf(self):
        self.mail();self.feedback();self.client.headers['X-CSRF-Token']='wrong'
        self.assertEqual(self.client.delete('/feedback/mail-0').status_code,401)

    def test_disconnected_telemetry_is_available_but_mail_requires_connection(self):
        self.mail();self.client.post('/disconnect')
        self.assertEqual(self.client.get('/telemetry').status_code,200)
        self.assertEqual(self.client.get('/emails').status_code,409)

    def test_schema_six_migration_preserves_revision_ids_and_index_state(self):
        path=self.settings.data_dir/'old6.db'
        with connection(path) as conn:
            for migration in [database._migration_1,database._migration_2,database._migration_3,database._migration_4,database._migration_5,database._migration_6]: migration(conn)
            conn.execute('PRAGMA user_version=6')
            conn.execute("INSERT INTO email_logs(email_id,created_at) VALUES ('old',?)",(utc_timestamp(),))
            conn.execute("INSERT INTO feedback_history(revision_id,account_id,email_id,label,indexing_state,created_at,attempt_count) VALUES (77,'legacy-unassigned','old','SPAM','failed',?,2)",(utc_timestamp(),))
        initialize_database(path);initialize_database(path)
        with connection(path) as conn:
            row=conn.execute('SELECT revision_id,label,indexing_state,attempt_count FROM feedback_history').fetchone()
            self.assertEqual(tuple(row),(77,'SPAM','failed',2))
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],7)
            self.assertIsNone(conn.execute('PRAGMA foreign_key_check').fetchone())

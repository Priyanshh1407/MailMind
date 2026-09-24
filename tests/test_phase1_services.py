from src.prediction import Prediction, LABEL2ID, ID2LABEL
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.config import Settings
from src.logging_utils import log_event
from src.llm_api import create_cloud_client
from src.vector_db import add_email_to_vector_db
from tests.support import ROOT


class ServiceTests(unittest.TestCase):
    def test_worker_boundary_suppresses_provider_exception_text(self):
        from src import main
        with patch.object(main,'_run_agent',side_effect=RuntimeError('synthetic-secret')), self.assertLogs('mailmind',level='ERROR') as captured:
            with self.assertRaisesRegex(RuntimeError,'Email processing failed') as failure:
                main.run_agent()
        self.assertTrue(failure.exception.__suppress_context__)
        self.assertNotIn('synthetic-secret',' '.join(captured.output))

    def test_oauth_boundary_suppresses_credential_exception_text(self):
        from src import email_client
        with patch.object(email_client,'_authenticate_gmail',side_effect=RuntimeError('synthetic-secret')), self.assertLogs('mailmind',level='ERROR') as captured:
            self.assertIsNone(email_client.authenticate_gmail())
        self.assertNotIn('synthetic-secret',' '.join(captured.output))

    def test_oauth_completion_page_attempts_to_close_its_tab(self):
        from src.email_client import _ClosingOAuthCallback
        callback=_ClosingOAuthCallback();response={}
        environ={'wsgi.url_scheme':'http','SERVER_NAME':'127.0.0.1','SERVER_PORT':'12345',
                 'REQUEST_METHOD':'GET','SCRIPT_NAME':'','PATH_INFO':'/','QUERY_STRING':'code=synthetic',
                 'SERVER_PROTOCOL':'HTTP/1.1','HTTP_HOST':'127.0.0.1:12345'}
        body=b''.join(callback(environ,lambda status,headers:response.update(status=status,headers=dict(headers))))
        self.assertEqual(response['status'],'200 OK')
        self.assertEqual(response['headers']['Content-Type'],'text/html; charset=utf-8')
        self.assertEqual(response['headers']['Cache-Control'],'no-store')
        self.assertIn(b'window.close()',body)
        self.assertIn(b'window.location.replace(target)',body)
        self.assertIn(b'http://127.0.0.1:5173/',body)
        self.assertEqual(response['headers']['Referrer-Policy'],'no-referrer')
        self.assertEqual(callback.last_request_uri,'http://127.0.0.1:12345/?code=synthetic')

    def test_imports_need_no_provider_key_model_or_vector_dependency(self):
        code = '''
import sys, importlib.abc, importlib, pathlib, os, socket
class BlockHeavy(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in ('torch','transformers','chromadb','dotenv') or fullname == 'google.genai':
   raise AssertionError('Unexpected runtime import: ' + fullname)
sys.meta_path.insert(0, BlockHeavy())
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Network forbidden'))
socket.getaddrinfo = socket.create_connection
os.environ.pop('GEMINI_API_KEY',None)
for name in ('src.config','src.database','src.setup_db','src.db_utils','src.vector_db','src.local_llm','src.llm_api','api.app'):
 importlib.import_module(name)
assert not pathlib.Path(os.environ['MAILMIND_DATA_DIR']).exists()
print('SIDE_EFFECT_FREE_IMPORTS_OK')
'''
        with tempfile.TemporaryDirectory() as temp:
            environment = dict(os.environ, PYTHONPATH=str(ROOT), MAILMIND_DATA_DIR=str(Path(temp)/'must-not-exist'))
            result = subprocess.run([sys.executable,'-c',code],cwd=temp,env=environment,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('SIDE_EFFECT_FREE_IMPORTS_OK',result.stdout)

    def test_cloud_factory_requires_key_only_when_invoked(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ValueError,'not configured'):
                create_cloud_client()

    def test_cloud_factory_accepts_injected_key_without_request(self):
        fake=Mock()
        with patch.dict(sys.modules,{'google.genai':fake}):
            from google import genai
            with patch.object(genai,'Client',return_value='synthetic-client') as factory:
                self.assertEqual(create_cloud_client('synthetic-key'),'synthetic-client')
                factory.assert_called_once_with(api_key='synthetic-key', http_options={'client_args':{'timeout':5.0},'retry_options':{'attempts':1}})

    def test_vector_ids_are_account_namespaced(self):
        target=Mock()
        for account in ('account-a','account-b'):
            add_email_to_vector_db('same-id','synthetic subject','synthetic body','IMPORTANT',account_id=account,collection=target)
        calls=target.upsert.call_args_list
        self.assertNotEqual(calls[0].kwargs['ids'],calls[1].kwargs['ids'])
        self.assertEqual(calls[0].kwargs['metadatas'][0]['account_id'],'account-a')

    def test_model_factory_forces_local_assets(self):
        from src.local_llm import MailMindModel
        fake=Mock()
        fake.AutoModelForSequenceClassification.from_pretrained.return_value.config = Mock(num_labels=3,id2label=ID2LABEL,label2id=LABEL2ID)
        with patch.dict(sys.modules,{'transformers':fake}):
            model=MailMindModel('/synthetic/model',vector_service=Mock())
        self.assertTrue(model.model_loaded)
        fake.AutoTokenizer.from_pretrained.assert_called_once_with('/synthetic/model',local_files_only=True,token=False)
        fake.AutoModelForSequenceClassification.from_pretrained.assert_called_once_with('/synthetic/model',local_files_only=True,token=False)

    def test_error_events_exclude_sensitive_exception_text(self):
        with self.assertLogs('mailmind',level='ERROR') as captured:
            log_event('synthetic_failure',error=RuntimeError('Bearer synthetic-secret https://host/private?token=synthetic-value'))
        joined=' '.join(captured.output)
        self.assertNotIn('synthetic-secret',joined)
        self.assertNotIn('https://',joined)
        self.assertEqual(json.loads(captured.records[0].message),{'event':'synthetic_failure','error_type':'RuntimeError'})


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mailmind-api-')
        self.addCleanup(self.temp.cleanup)
        self.settings=Settings(data_dir=Path(self.temp.name),model_path=Path(self.temp.name)/'model')

    def connect(self, client, application):
        result=client.post('/session',headers={'Origin':'http://localhost:5173'})
        client.headers.update({'Origin':'http://localhost:5173','X-CSRF-Token':result.json()['csrf_token']})
        manager=application.state.accounts
        context,_=manager.session(client.cookies.get('mailmind_session'))
        manager.finish_auth(manager.begin_auth(context),('current@example.test','{}'))

    def test_lifespan_initializes_database_not_cloud_or_vectors(self):
        from fastapi.testclient import TestClient
        from api.app import create_app
        fake_model=Mock(model_loaded=True)
        fake_model.predict.return_value=Prediction(category='IMPORTANT',outcome='CLASSIFIED')
        model_factory=Mock(return_value=fake_model)
        vector_factory=Mock(side_effect=AssertionError('Unexpected vector initialization'))
        application=create_app(settings=self.settings,model_factory=model_factory,vector_factory=vector_factory)
        self.assertFalse(self.settings.db_path.exists())
        with patch.dict(os.environ,{},clear=True), TestClient(application,base_url='http://localhost') as client:
            self.connect(client,application)
            self.assertEqual(client.get('/').json()['model_loaded'],True)
            self.assertTrue(self.settings.db_path.exists())
            response=client.post('/predict',json={'subject':'synthetic','body':'synthetic'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['category'],'IMPORTANT')
        model_factory.assert_called_once()
        vector_factory.assert_not_called()
        self.assertIsNone(application.state.model)

    def test_normal_mode_api_loads_the_three_class_shadow_checkpoint(self):
        from fastapi.testclient import TestClient
        from api.app import create_app
        settings=Settings(data_dir=Path(self.temp.name),
                          model_path=Path(self.temp.name)/'legacy-binary',
                          shadow_model_path=Path(self.temp.name)/'three-class-shadow')
        model=Mock(model_loaded=True,model_version='synthetic-three-class',
                   training_scope='private_user_approved_inbox',load_reason='ready')
        with patch('api.app.DeferredMailMindModel',return_value=model) as factory:
            application=create_app(settings=settings)
            with TestClient(application,base_url='http://localhost'):
                factory.assert_called_once_with(model_path=settings.shadow_model_path,
                                                settings=settings)

    def test_request_failures_are_generic_and_redacted(self):
        from fastapi.testclient import TestClient
        from api.app import create_app
        model=Mock(model_loaded=True)
        model.predict.side_effect=RuntimeError('synthetic-secret-email-and-token')
        application=create_app(settings=self.settings,model_factory=Mock(return_value=model))
        with TestClient(application,base_url='http://localhost') as client, self.assertLogs('mailmind',level='ERROR') as captured:
            self.connect(client,application)
            response=client.post('/predict',json={'subject':'synthetic','body':'synthetic'},headers={'Origin':'http://localhost:5173'})
        self.assertEqual(response.status_code,500)
        self.assertEqual(response.headers['access-control-allow-origin'],'http://localhost:5173')
        self.assertNotIn('synthetic-secret',response.text)
        self.assertNotIn('synthetic-secret',' '.join(captured.output))

    def test_configured_vector_service_is_lazy_and_shared(self):
        from fastapi.testclient import TestClient
        from api.app import create_app
        target=Mock()
        target.query.return_value={'documents':[[]],'metadatas':[[]],'distances':[[]]}
        vector_factory=Mock(return_value=target)
        class FakeModel:
            model_loaded=True
            def __init__(self, model_path, vector_service):
                self.vector_service=vector_service
            def predict(self, subject, body, *, account_id=None):
                return self.vector_service.get_knn_prediction(subject,body,account_id=account_id) or Prediction(category='IMPORTANT',outcome='CLASSIFIED')
        application=create_app(settings=self.settings,model_factory=FakeModel,vector_factory=vector_factory)
        with TestClient(application,base_url='http://localhost') as client:
            self.connect(client,application)
            vector_factory.assert_not_called()
            for _ in range(2):
                self.assertEqual(client.post('/predict',json={'subject':'synthetic','body':'synthetic'}).status_code,200)
        vector_factory.assert_called_once_with(self.settings.data_dir/'chroma_db')

    def test_migrated_legacy_feedback_does_not_invent_account_ownership(self):
        from fastapi.testclient import TestClient
        from api.app import create_app
        from src.config import LEGACY_ACCOUNT
        from src.db_utils import log_email_to_db, get_email
        target=Mock()
        application=create_app(settings=self.settings,model_factory=Mock(return_value=Mock(model_loaded=True)),vector_factory=Mock(return_value=target))
        with TestClient(application,base_url='http://localhost') as client:
            self.connect(client,application)
            log_email_to_db('synthetic-legacy','sender','subject','body','SPAM','SPAM',db_path=self.settings.db_path)
            (self.settings.data_dir/'user_profile.json').write_text(json.dumps({'email':'current@example.test'}),encoding='utf-8')
            response=client.post('/feedback',json={'email_id':'synthetic-legacy','subject':'subject','body':'body','label':'IMPORTANT'})
            self.assertEqual(response.status_code,404)
            row=get_email('synthetic-legacy',db_path=self.settings.db_path)
            self.assertEqual(row['account_id'],LEGACY_ACCOUNT)
            self.assertIsNone(row['human_label'])
        target.upsert.assert_not_called()

    def test_startup_failure_does_not_leak_exception_content(self):
        from fastapi.testclient import TestClient
        from api.app import create_app
        application=create_app(settings=self.settings,model_factory=Mock(side_effect=RuntimeError('synthetic-secret')))
        with self.assertLogs('mailmind',level='ERROR') as captured:
            with self.assertRaisesRegex(RuntimeError,'MailMind initialization') as failure:
                with TestClient(application):
                    pass
        self.assertNotIn('synthetic-secret',str(failure.exception))
        self.assertNotIn('synthetic-secret',' '.join(captured.output))

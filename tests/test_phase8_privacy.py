"""Synthetic privacy, external-payload and network-disabled routing checks."""
import json,os,socket,tempfile,unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock,patch
from src.privacy import redact,external_email
from src.config import Settings
from src import llm_api,main
from src.notifier import send_telegram_alert
from src.local_llm import MailMindModel
from src.prediction import Prediction
from src.database import initialize_database,connection
from src.db_utils import log_email_to_db
from src.offline_assets import CLASSIFIER_FILES,EMBEDDING_FILES,file_hash,verify_role
from scripts.sanitize_data import process_and_save
from scripts.prepare_offline_assets import prepare
import tests.test_phase5_recovery as recovery_support
from src.work_queue import claim_cycle,ingest_email,finish_cycle

class PrivacyTests(unittest.TestCase):
    def setUp(self):
        with llm_api._model_cooldown_lock:
            llm_api._model_cooldowns.clear()

    def test_currency_alternatives_and_decimal(self):
        for text in ['Rs. 5,000.25','Rs 5000','₹5000','$50.20','INR 5000','USD 50']:
            self.assertEqual(redact(text),'[AMOUNT]')
    def test_whole_email_and_vpa_are_distinct(self):
        self.assertEqual(redact('person@example.test user@okicici'),'[EMAIL] [UPI_ID]')
    def test_identifiers_and_links_are_masked(self):
        result=redact('ABCDE1234F 123456789012 +91 98765 43210 https://example.test/secret?token=private')
        for value in ['ABCDE1234F','123456789012','98765','secret','private']:self.assertNotIn(value,result)
    def test_non_strings_are_empty(self):
        for value in [None,123,float('nan'),{},[]]:self.assertEqual(redact(value),'')
    def test_sender_is_omitted_and_fields_masked(self):
        payload=external_email('Personal Name <person@example.test>','Rs. 5000','contact person@example.test')
        self.assertEqual(payload,{'sender':'[SENDER]','subject':'[AMOUNT]','body':'contact [EMAIL]'})
    def test_non_sensitive_words_and_boundaries_survive(self):
        self.assertEqual(redact('Interview tomorrow. Please reply.'),'Interview tomorrow. Please reply.')
        self.assertEqual(redact('XABCDE1234FY'),'XABCDE1234FY')
    def test_redaction_is_idempotent(self):
        value=redact('person@example.test Rs. 5000 user@bank')
        self.assertEqual(redact(value),value)
    def test_cloud_captures_only_minimized_email_and_precedents(self):
        fake=Mock();fake.models.generate_content.return_value.text='{"category":"UPDATES"}'
        examples=[{'email_id':str(index),'text':'person@example.test Rs. 5000','label':'UPDATES'} for index in range(3)]
        with patch.object(llm_api,'get_client',return_value=fake),patch.object(llm_api.vector_db,'search_similar_emails',return_value=examples):
            result=llm_api.classify_email('Personal Name','person@example.test','Rs. 5000',account_id='synthetic@example.test',settings=Settings())
        self.assertEqual(result.category,'UPDATES')
        contents=fake.models.generate_content.call_args.kwargs['contents']
        for value in ['Personal Name','person@example.test','5000']:self.assertNotIn(value,contents)
        self.assertEqual(len(json.loads(contents)['precedents']),3)
    def test_cloud_local_only_returns_before_prompt_retrieval_or_keys(self):
        with patch.object(llm_api,'get_client') as client,patch.object(llm_api.vector_db,'search_similar_emails') as retrieval,patch.object(llm_api.json,'dumps',side_effect=AssertionError('No prompt')):
            result=llm_api.classify_email('private','private','private',settings=Settings(local_only=True))
        client.assert_not_called();retrieval.assert_not_called();self.assertEqual(result.reason,'cloud_disabled_local_only')
    def test_telegram_captured_payload_is_minimized(self):
        fake=Mock(status_code=200);fake.json.return_value={'ok':True,'result':{'message_id':123}}
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'123:synthetic','TELEGRAM_CHAT_ID':'456'}),patch('src.notifier.requests.post',return_value=fake) as post:
            send_telegram_alert('Private Name','person@example.test','Rs. 5000',settings=Settings())
        text=post.call_args.kwargs['json']['text']
        for value in ['Private Name','person@example.test','5000']:self.assertNotIn(value,text)
    def test_telegram_local_only_does_not_read_keys_or_send(self):
        with patch('src.notifier.os.getenv',side_effect=AssertionError('No keys')),patch('src.notifier.requests.post') as post:
            result=send_telegram_alert('sender','subject','body',settings=Settings(local_only=True))
        post.assert_not_called();self.assertEqual(result.code,'notification_disabled_local_only')
    def test_local_mode_skips_environment_file(self):
        with patch.dict(os.environ,{'MAILMIND_LOCAL_ONLY':'true'}),patch('dotenv.load_dotenv',side_effect=AssertionError('No file')):
            self.assertTrue(Settings.from_environment(load_file=True).local_only)
    def test_local_flag_in_loaded_file_controls_routing(self):
        with patch.dict(os.environ,{'MAILMIND_LOCAL_ONLY':'false'}),patch('dotenv.load_dotenv',side_effect=lambda *args,**kwargs:os.environ.update({'MAILMIND_LOCAL_ONLY':'true'})):
            self.assertTrue(Settings.from_environment(load_file=True).local_only)
    def test_cloud_client_factory_is_blocked_in_local_environment(self):
        with patch.dict(os.environ,{'MAILMIND_LOCAL_ONLY':'true'}):
            with self.assertRaises(ValueError):llm_api.create_cloud_client('synthetic-key')
            with self.assertRaises(ValueError):llm_api.get_client()
    def test_invalid_local_mode_fails_closed(self):
        with patch.dict(os.environ,{'MAILMIND_LOCAL_ONLY':'tru'}):
            with self.assertRaises(ValueError):Settings.from_environment(load_file=True)
    def test_frontend_contains_no_external_resources_or_avatar_seeds(self):
        files=[Path('frontend/index.html'),Path('frontend/src/index.css'),*Path('frontend/src/components').glob('*.jsx')]
        for path in files:
            for host in ['fonts.googleapis.com','fonts.gstatic.com','dicebear','gravatar','ui-avatars']:
                self.assertNotIn(host,path.read_text(encoding='utf-8'))

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.db=self.root/'db.sqlite';initialize_database(self.db)
        for account in ['a@example.test','b@example.test']:
            log_email_to_db('private-id','Private Name','Contact person@example.test','Rs. 5000', 'UPDATES','UPDATES',human_label='UPDATES',account_id=account,db_path=self.db)
    def test_export_is_account_scoped_with_only_three_masked_fields(self):
        target=self.root/'nested'/'output.csv';self.assertEqual(process_and_save(db_path=self.db,account_id='a@example.test',output=target),1)
        text=target.read_text();self.assertTrue(text.startswith('subject,body,human_label'))
        for value in ['private-id','Private Name','person@example.test','5000','a@example.test','b@example.test']:self.assertNotIn(value,text)
    def test_export_neutralizes_spreadsheet_formulas(self):
        with connection(self.db) as conn:
            conn.execute("UPDATE email_logs SET subject='=1+1' WHERE account_id=?",('a@example.test',))
        target=self.root/'formula.csv';process_and_save(db_path=self.db,account_id='a@example.test',output=target)
        self.assertIn("'=1+1",target.read_text())
    def test_export_refuses_unassigned_account(self):
        with self.assertRaises(ValueError):process_and_save(db_path=self.db,account_id='legacy-unassigned',output=self.root/'out.csv')
    def test_export_preserves_existing_output(self):
        target=self.root/'out.csv';target.write_text('preserve')
        with self.assertRaises(ValueError):process_and_save(db_path=self.db,account_id='a@example.test',output=target)
        self.assertEqual(target.read_text(),'preserve')

class AssetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for role,names in [('classifier',CLASSIFIER_FILES),('embedding',EMBEDDING_FILES)]:
            folder=self.root/role;folder.mkdir()
            for name in names:(folder/name).write_text('synthetic-asset')
        self.manifest=prepare(self.root/'classifier',self.root/'embedding',self.root/'packaged')
    def test_asset_packaging_hashes_and_verifies_both_roles(self):
        for role in ['classifier','embedding']:self.assertTrue(verify_role(self.manifest,role).is_dir())
    def test_missing_and_tampered_assets_are_rejected(self):
        (self.root/'packaged/classifier/config.json').write_text('tampered')
        with self.assertRaises(ValueError):verify_role(self.manifest,'classifier')
        (self.root/'packaged/embedding/model.onnx').unlink()
        with self.assertRaises(ValueError):verify_role(self.manifest,'embedding')
    def test_checkpoint_path_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):verify_role(self.manifest,'classifier',self.root/'other')
    def test_manifest_path_traversal_is_rejected(self):
        payload=json.loads(self.manifest.read_text());payload['assets']['classifier']['files']['../outside']='bad';self.manifest.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):verify_role(self.manifest,'classifier')
    def test_absent_manifest_does_not_load_or_download_classifier(self):
        with patch.dict(os.environ,{'HF_HUB_OFFLINE':'0','HF_HUB_DISABLE_TELEMETRY':'0'}),patch('src.offline_assets.verify_role',side_effect=ValueError('Missing assets')):
            model=MailMindModel(self.root/'classifier',settings=Settings(local_only=True))
            self.assertEqual(os.environ['HF_HUB_OFFLINE'],'1')
            self.assertEqual(os.environ['HF_HUB_DISABLE_TELEMETRY'],'1')
        self.assertFalse(model.model_loaded)
    def test_packager_never_overwrites_assets(self):
        with self.assertRaises(ValueError):prepare(self.root/'classifier',self.root/'embedding',self.root/'packaged')

class LocalFlowTests(unittest.TestCase):
    setUp=recovery_support.RecoveryTests.setUp
    def local_cycle(self,category):
        self.settings=replace(self.settings,local_only=True);self.manager.settings=self.settings
        self.model.predict.return_value=Prediction(category=category,outcome='CLASSIFIED',source='local',model_version='synthetic-test')
        token,job=claim_cycle(self.manager,self.context)
        ingest_email({'id':'synthetic-saved','sender':'sender','subject':'Synthetic subject','body':'Synthetic body'},self.manager,self.context,token)
        finish_cycle(self.manager,self.context,token,job)
        with patch.object(main,'refresh_gmail',side_effect=AssertionError('No Gmail')) as gmail,patch.object(main,'classify_email',side_effect=AssertionError('No cloud')) as cloud,patch.object(main,'send_telegram_alert',side_effect=AssertionError('No Telegram')) as alert,patch.object(main,'mark_as_read',side_effect=AssertionError('No write')) as marker:
            result=main._run_agent(settings=self.settings,manager=self.manager,model=self.model,collection=self.collection)
        for mock in [gmail,cloud,alert,marker]:mock.assert_not_called()
        return result
    def test_saved_update_completes_locally_with_network_blocked(self):
        result=self.local_cycle('UPDATES');self.assertEqual(result['processed_count'],1);self.assertEqual(result['network'],'disabled')
    def test_saved_urgent_message_blocks_alert_without_marking_read(self):
        result=self.local_cycle('IMPORTANT');self.assertEqual(result['processed_count'],0)
        with connection(self.settings.db_path) as conn:
            outbox=conn.execute('SELECT * FROM notification_outbox').fetchone()
        self.assertEqual(outbox['status'],'blocked');self.assertEqual(outbox['error_code'],'notification_disabled_local_only')
    def test_api_disables_authentication_and_inbox_queue(self):
        self.app.state.settings=replace(self.settings,local_only=True)
        self.assertEqual(self.client.post('/authenticate').status_code,403)
        self.assertEqual(self.client.post('/inbox/sync').status_code,403)
        data=self.client.get('/telemetry').json();self.assertTrue(data['mode']['local_only']);self.assertEqual(data['providers']['gemini'],'disabled_local_only')
    def test_local_predict_works_for_paired_disconnected_account(self):
        self.app.state.settings=replace(self.settings,local_only=True)
        self.manager.pause(self.context)
        self.model.predict.return_value=Prediction(category='UPDATES',outcome='CLASSIFIED')
        self.assertEqual(self.client.post('/predict',json={'subject':'Synthetic','body':'Body'}).status_code,200)

    def test_local_mode_preserves_already_sent_alerts(self):
        self.settings=replace(self.settings,local_only=True);self.manager.settings=self.settings
        token,job=claim_cycle(self.manager,self.context)
        ingest_email({'id':'sent-before-offline','sender':'sender','subject':'Synthetic','body':'Synthetic'},self.manager,self.context,token)
        finish_cycle(self.manager,self.context,token,job)
        with connection(self.settings.db_path) as conn:
            conn.execute("UPDATE processing_tasks SET stage='notify',category='IMPORTANT'")
            conn.execute("INSERT INTO notification_outbox(account_id,email_id,status,created_at,updated_at) VALUES (?,?,?,'2026-09-19T00:00:00Z','2026-09-19T00:00:00Z')",(self.context.account_id,'sent-before-offline','sent'))
        with patch.object(main,'refresh_gmail',side_effect=AssertionError('No Gmail')),patch.object(main,'send_telegram_alert',side_effect=AssertionError('No Telegram')),patch.object(main,'mark_as_read',side_effect=AssertionError('No read')):
            result=main._run_agent(settings=self.settings,manager=self.manager,model=self.model,collection=self.collection)
        with connection(self.settings.db_path) as conn:self.assertEqual(conn.execute('SELECT status FROM notification_outbox').fetchone()['status'],'sent')
        self.assertEqual(result['processed_count'],1)

    def test_local_mode_preserves_unknown_delivery(self):
        self.settings=replace(self.settings,local_only=True);self.manager.settings=self.settings
        token,job=claim_cycle(self.manager,self.context)
        ingest_email({'id':'unknown-before-offline','sender':'sender','subject':'Synthetic','body':'Synthetic'},self.manager,self.context,token)
        finish_cycle(self.manager,self.context,token,job)
        with connection(self.settings.db_path) as conn:
            conn.execute("UPDATE processing_tasks SET stage='notify',category='IMPORTANT'")
            conn.execute("INSERT INTO notification_outbox(account_id,email_id,status,created_at,updated_at) VALUES (?,?,?,'2026-09-19T00:00:00Z','2026-09-19T00:00:00Z')",(self.context.account_id,'unknown-before-offline','unknown'))
        with patch.object(main,'refresh_gmail',side_effect=AssertionError('No Gmail')),patch.object(main,'send_telegram_alert',side_effect=AssertionError('No Telegram')),patch.object(main,'mark_as_read',side_effect=AssertionError('No read')):
            result=main._run_agent(settings=self.settings,manager=self.manager,model=self.model,collection=self.collection)
        with connection(self.settings.db_path) as conn:self.assertEqual(conn.execute('SELECT status FROM notification_outbox').fetchone()['status'],'unknown')
        self.assertEqual(result['processed_count'],0)

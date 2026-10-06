from src.prediction import Prediction, LABEL2ID, ID2LABEL
"""Real parser and worker checks with fake mail, providers, and temporary state."""
import base64
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch
import pandas as pd
from fastapi.testclient import TestClient
from api.app import create_app
from src.account_state import AccountManager, WorkCancelled
from src.config import Settings
from src.database import SCHEMA_VERSION, connection, initialize_database, _migration_1, _migration_2, _migration_3, utc_timestamp
from src.db_utils import get_email, get_ingestion_state, log_email_to_db, save_ingestion_state
from src.email_client import get_unread_emails, get_new_emails, FetchBatch
from src.email_text import format_email_text, normalize_text, html_to_text, prepare_training_text, MAX_BODY_CHARS
from src.mime_parser import parse_body, parse_gmail_message, parse_raw_email, MessageParseError, MAX_PARTS, MAX_DECODED_BYTES
from tests.support import FakeRequest, FakeGmail, fixtures, load_function

A, B = 'a@example.test', 'b@example.test'


def leaf(text='Good body', mime='text/plain', charset='utf-8', **extra):
    return {'mimeType':mime, 'headers':[{'name':'Content-Type','value':f'{mime}; charset={charset}'}],
            'body':{'data':base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip('=')}, **extra}


class Mailbox:
    """Mutable unread list: page offsets can shift after mark-read."""
    def __init__(self, messages):
        self.items = dict(messages)
        self.unread = list(messages)
        self.lists, self.gets, self.inlines = [], [], []
        self.list_error = None
        self.detail_errors = set()
        self.inline_data = {}
        self._attachments = False

    def users(self): return self
    def messages(self): return self
    def attachments(self):
        self._attachments = True
        return self

    def list(self, **kwargs):
        self.lists.append(kwargs)
        if self.list_error:
            raise self.list_error
        start=int(kwargs.get('pageToken',0))
        count=kwargs['maxResults']
        ids=self.unread[start:start+count]
        result={'messages':[{'id':key} for key in ids]}
        if start+count < len(self.unread):
            result['nextPageToken']=str(start+count)
        return FakeRequest(result)

    def get(self, **kwargs):
        if self._attachments:
            self._attachments=False
            self.inlines.append(kwargs)
            return FakeRequest({'data':self.inline_data[kwargs['id']]})
        self.gets.append(kwargs)
        if kwargs['id'] in self.detail_errors:
            raise RuntimeError('synthetic-private-provider-detail')
        return FakeRequest(self.items[kwargs['id']])

    def modify(self, **kwargs):
        if kwargs['id'] in self.unread:
            self.unread.remove(kwargs['id'])
        return FakeRequest({})


def mailbox(count):
    return Mailbox({f'synthetic-{i}':{'payload':leaf(f'Good message {i}')} for i in range(count)})


class ParserTests(unittest.TestCase):
    def test_all_staged_fixtures_have_real_outcomes(self):
        result=get_unread_emails(FakeGmail(fixtures()),max_results=20)
        self.assertEqual(result.status,'partial')
        self.assertEqual(len(result.emails),6)
        self.assertEqual(result.failures,[{'code':'malformed_base64','email_id':'synthetic-malformed'}])
        rows={r['id']:r for r in result.emails}
        self.assertIn('Interview tomorrow',rows['synthetic-nested']['body'])
        self.assertNotIn('<',rows['synthetic-html']['body'])
        self.assertIn('café',rows['synthetic-encoded']['body'])
        self.assertIn('URGENT',rows['synthetic-long']['body'])
        self.assertNotIn('URGENT',rows['synthetic-long']['body_snippet'])

    def test_nested_alternatives_prefer_plain_and_skip_file_attachment(self):
        part={'mimeType':'multipart/mixed','parts':[leaf('ATTACHMENT SECRET',filename='notes.txt'),
              {'mimeType':'multipart/alternative','parts':[leaf('<p>HTML duplicate</p>','text/html'),leaf('The real body')]}]}
        self.assertEqual(parse_body(part).text,'The real body')

    def test_attachment_disposition_and_named_parts_are_skipped(self):
        for header in [('Content-Disposition','attachment'),('Content-Type','text/plain; name=secret.txt')]:
            bad=leaf('ATTACHMENT SECRET')
            bad['headers'].append({'name':header[0],'value':header[1]})
            self.assertEqual(parse_body({'mimeType':'multipart/mixed','parts':[bad,leaf('real')]}).text,'real')

    def test_forwarded_email_file_is_not_primary_body(self):
        part={'mimeType':'multipart/mixed','parts':[{'mimeType':'message/rfc822','parts':[leaf('forwarded secret')]},leaf('outer body')]}
        self.assertEqual(parse_body(part).text,'outer body')

    def test_mixed_inline_sections_are_kept(self):
        result=parse_body({'mimeType':'multipart/mixed','parts':[leaf('Intro'),leaf('<p>Urgent next step</p>','text/html')]})
        self.assertIn('Intro',result.text)
        self.assertIn('Urgent next step',result.text)

    def test_related_html_root_is_not_replaced_by_text_resource(self):
        result=parse_body({'mimeType':'multipart/related','parts':[leaf('<p>Actual body</p>','text/html'),leaf('resource text')]})
        self.assertEqual(result.text,'Actual body')

    def test_html_entities_blocks_and_hidden_content(self):
        result=html_to_text('<head><title>Hidden</title></head><p>A &amp; B</p><script>SECRET</script><style>SECRET</style><div>Next<br>line</div><img src="https://example.test/track">')
        self.assertEqual(result,'A & B\n\nNext\nline')
        self.assertNotIn('SECRET',result)

    def test_malformed_html_can_still_supply_readable_text(self):
        self.assertEqual(html_to_text('<div>Hello <b>world'),'Hello world')

    def test_unpadded_base64_and_declared_charset(self):
        self.assertEqual(parse_body(leaf('café','text/plain','iso-8859-1')).text,'café')
        self.assertEqual(parse_body(leaf('short')).text,'short')

    def test_unknown_charset_and_invalid_bytes_have_warnings(self):
        part=leaf('Good text')
        part['headers'][0]['value']='text/plain; charset=unknown-synthetic-codec'
        self.assertIn('unknown_charset',parse_body(part).warnings)
        part['headers'][0]['value']='text/plain; charset=utf-8'
        part['body']['data']=base64.urlsafe_b64encode(b'Good\xfftext').decode()
        result=parse_body(part)
        self.assertIn('decode_replacement',result.warnings)
        self.assertIn('\ufffd',result.text)

    def test_bad_plain_part_falls_back_to_valid_html(self):
        bad=leaf()
        bad['body']['data']='a'
        result=parse_body({'mimeType':'multipart/alternative','parts':[bad,leaf('<p>Recovered</p>','text/html')]})
        self.assertEqual(result.text,'Recovered')
        self.assertIn('malformed_base64',result.warnings)

    def test_short_selected_plain_body_is_not_marked_truncated_by_unused_html(self):
        result=parse_body({'mimeType':'multipart/alternative','parts':[leaf('x'*(MAX_BODY_CHARS+50),'text/html'),leaf('short plain')]})
        self.assertEqual(result.text,'short plain')
        self.assertFalse(result.truncated)

    def test_missing_or_empty_body_is_not_classified_as_good_mail(self):
        for part in ({'mimeType':'text/plain','body':{}},leaf(''),leaf('  \r\n ')):
            with self.subTest(part=part), self.assertRaises(MessageParseError):
                parse_body(part)

    def test_malformed_leaf_does_not_lose_valid_sibling(self):
        bad={'mimeType':'text/plain','body':['invalid']}
        result=parse_body({'mimeType':'multipart/mixed','parts':[bad,leaf('valid sibling')]})
        self.assertEqual(result.text,'valid sibling')
        self.assertIn('malformed_body',result.warnings)

    def test_encoded_headers_are_decoded_and_normalized(self):
        part=leaf()
        part['headers'] += [{'name':'Subject','value':'=?utf-8?b?SW50ZXJ2aWV3IOKckw==?='},
                            {'name':'From','value':'=?utf-8?q?Jos=C3=A9?= <person@example.test>'}]
        result=parse_gmail_message({'payload':part},'synthetic')
        self.assertEqual(result['subject'],'Interview ✓')
        self.assertIn('José',result['sender'])

    def test_character_and_byte_limits_are_visible(self):
        for mime in ('text/plain','text/html'):
            result=parse_body(leaf('x'*(MAX_BODY_CHARS+50),mime))
            self.assertEqual(len(result.text),MAX_BODY_CHARS)
            self.assertTrue(result.truncated)
        result=parse_body(leaf('x'*(MAX_DECODED_BYTES+50)))
        self.assertIn('body_byte_limit',result.warnings)

    def test_part_and_depth_limits_stop_excess_work(self):
        part={'mimeType':'multipart/mixed','parts':[leaf('good')]*(MAX_PARTS+5)}
        result=parse_body(part)
        self.assertTrue(result.truncated)
        self.assertIn('mime_limit',result.warnings)
        nested=leaf('deep')
        for _ in range(25): nested={'mimeType':'multipart/mixed','parts':[nested]}
        with self.assertRaises(MessageParseError): parse_body(nested)

    def test_training_raw_mime_uses_the_same_reader(self):
        raw='Subject: =?utf-8?q?Interview?=\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary=x\n\n--x\nContent-Type: text/html; charset=utf-8\n\n<p>duplicate</p>\n--x\nContent-Type: text/plain; charset=utf-8\n\n  Confirm   tomorrow.\n--x--\n'
        result=parse_raw_email(raw)
        self.assertEqual(result['subject'],'Interview')
        self.assertEqual(result['body'],'Confirm tomorrow.')

    def test_training_and_inference_format_match(self):
        frame=pd.DataFrame({'subject':['  Interview\r\n reminder',None],'body':['  Café\t tomorrow\r\n\r\nNext',None]})
        output=prepare_training_text(frame)
        self.assertEqual(output['combined_text'].tolist(),[format_email_text('  Interview\r\n reminder','  Café\t tomorrow\r\n\r\nNext'),format_email_text('','')])
        self.assertEqual(normalize_text(normalize_text('A\r\nB')),'A\nB')

    def test_related_start_identifies_a_root_that_is_not_first(self):
        body=leaf('<p>The selected body</p>','text/html')
        body['headers'].append({'name':'Content-ID','value':'<root>'})
        payload={'mimeType':'multipart/related','headers':[{'name':'Content-Type','value':'multipart/related; start="<root>"'}],
                 'parts':[leaf('not the root'),body]}
        self.assertEqual(parse_body(payload).text,'The selected body')

    def test_vector_documents_and_queries_use_same_format(self):
        from src.vector_db import add_email_to_vector_db, search_similar_emails
        target=Mock()
        target.query.return_value={'documents':[[]],'metadatas':[[]],'distances':[[]]}
        subject,body=' Interview\r\n reminder','Confirm\t tomorrow'
        add_email_to_vector_db('synthetic',subject,body,'IMPORTANT',account_id=A,collection=target)
        search_similar_emails(subject,body,account_id=A,collection=target)
        expected=format_email_text(subject,body)
        self.assertEqual(target.upsert.call_args.kwargs['documents'],[expected])
        self.assertEqual(target.query.call_args.kwargs['query_texts'],[expected])

    def test_cloud_prompt_uses_shared_text_and_keeps_late_content(self):
        from src import llm_api
        client=Mock()
        client.models.generate_content.return_value.text='{"category":"IMPORTANT"}'
        body='Intro. '*70+'URGENT tomorrow'
        with patch.object(llm_api,'get_client',return_value=client):
            self.assertEqual(llm_api.classify_email('s',' Interview ',body).category,'IMPORTANT')
        self.assertIn(format_email_text(' Interview ',body),client.models.generate_content.call_args.kwargs['contents'])

    def test_local_tokenizer_receives_shared_full_text_with_explicit_token_cap(self):
        from src.local_llm import MailMindModel
        from src.email_text import MODEL_MAX_TOKENS
        model=object.__new__(MailMindModel)
        model.model_loaded=True
        model.model_version='synthetic-three-class'
        model.vector_service=Mock()
        model.vector_service.get_knn_prediction.return_value=None
        model.tokenizer=Mock(return_value={'input_ids':'synthetic'})
        model.model=Mock()
        model.id2label={0:'SPAM',1:'IMPORTANT'}
        torch=MagicMock()
        functional=MagicMock()
        functional.softmax.return_value[0][1].item.return_value=0.9
        torch.argmax.return_value.item.return_value=1
        torch.nn.functional=functional
        body='Intro. '*70+'URGENT tomorrow'
        with patch.dict(sys.modules,{'torch':torch,'torch.nn':torch.nn,'torch.nn.functional':functional}):
            self.assertEqual(model.predict(' Interview ',body,account_id=A).category,'IMPORTANT')
        self.assertEqual(model.tokenizer.call_args.args[0],format_email_text(' Interview ',body))
        self.assertEqual(model.tokenizer.call_args.kwargs['max_length'],MODEL_MAX_TOKENS)

    def test_all_training_preparation_paths_use_shared_format_and_token_limit(self):
        from src.email_text import MODEL_MAX_TOKENS
        for path,name in [('scripts/train_pipeline.py','prepare_data'),('scripts/train_stage1_base.py','prepare_data'),
                          ('scripts/train_stage2_personal.py','prepare_personal_data')]:
            with self.subTest(path=path):
                frame=pd.DataFrame({'subject':[' Interview\r\n reminder'],'body':['Confirm\t tomorrow'],'human_label':['IMPORTANT']})
                pandas=Mock()
                pandas.read_csv.return_value=frame
                dataset=Mock()
                captured=[]
                def create(frame):
                    captured.extend(frame['combined_text'].tolist())
                    return dataset
                factory=Mock()
                factory.from_pandas.side_effect=create
                tokenizer=Mock()
                auto=Mock()
                auto.from_pretrained.return_value=tokenizer
                dataset.map.side_effect=lambda fn,**kwargs: fn({'combined_text':captured})
                fn=load_function(path,name,{'pd':pandas,'Dataset':factory,'AutoTokenizer':auto,'load_training_frame':lambda **kwargs: frame,
                                            'prepare_training_frame':__import__('src.training_data',fromlist=['prepare_training_frame']).prepare_training_frame,'MODEL_MAX_TOKENS':MODEL_MAX_TOKENS})
                fn()
                self.assertEqual(captured,[format_email_text(' Interview\r\n reminder','Confirm\t tomorrow')])
                self.assertEqual(tokenizer.call_args.kwargs['max_length'],MODEL_MAX_TOKENS)


class FetchTests(unittest.TestCase):
    def test_history_sync_returns_only_new_unique_inbox_messages(self):
        class HistoryMailbox(Mailbox):
            def history(self): return self
            def list(self, **kwargs):
                if 'startHistoryId' in kwargs:
                    return FakeRequest({'historyId':'12','history':[
                        {'messagesAdded':[{'message':{'id':'synthetic-1'}},
                                           {'message':{'id':'synthetic-1'}},
                                           {'message':{'id':'synthetic-2'}}]}]})
                return super().list(**kwargs)
        service=HistoryMailbox({f'synthetic-{i}':{'payload':leaf(f'Live {i}')} for i in range(3)})
        result=get_new_emails(service,'10',exclude_ids={'synthetic-2'})
        self.assertEqual([row['id'] for row in result.emails],['synthetic-1'])
        self.assertEqual(result.history_id,'12')

    def test_expired_history_cursor_is_reported_without_guessing_a_cursor(self):
        class ExpiredMailbox(Mailbox):
            def history(self): return self
            def list(self, **kwargs):
                error=RuntimeError('synthetic expired history')
                error.resp=Mock(status=404)
                raise error
        result=get_new_emails(ExpiredMailbox({}),'10')
        self.assertTrue(result.history_expired)
        self.assertTrue(result.listing_error)
        self.assertEqual(result.emails,[])

    def test_history_cursor_does_not_advance_past_queue_capacity(self):
        class OverflowMailbox(Mailbox):
            def history(self): return self
            def list(self, **kwargs):
                if 'startHistoryId' in kwargs:
                    return FakeRequest({'historyId':'13','history':[
                        {'messagesAdded':[{'message':{'id':'synthetic-0'}},
                                           {'message':{'id':'synthetic-1'}},
                                           {'message':{'id':'synthetic-2'}}]}]})
                return super().list(**kwargs)
        service=OverflowMailbox({f'synthetic-{i}':{'payload':leaf(f'Live {i}')} for i in range(3)})
        first=get_new_emails(service,'10',max_results=1)
        self.assertEqual([row['id'] for row in first.emails],['synthetic-0'])
        self.assertEqual(first.history_id,'10')
        self.assertTrue(first.has_more)
        second=get_new_emails(service,'10',max_results=1,exclude_ids={'synthetic-0'})
        self.assertEqual([row['id'] for row in second.emails],['synthetic-1'])
        self.assertEqual(second.history_id,'10')

    def test_empty_inbox_and_failed_listing_are_different(self):
        self.assertEqual(get_unread_emails(mailbox(0)).status,'empty')
        service=mailbox(0)
        service.list_error=RuntimeError('synthetic-secret')
        with self.assertLogs('mailmind',level='ERROR') as logs:
            result=get_unread_emails(service)
        self.assertEqual(result.status,'error')
        self.assertTrue(result.listing_error)
        self.assertNotIn('synthetic-secret',' '.join(logs.output))

    def test_detail_failure_keeps_the_other_messages(self):
        service=mailbox(3)
        service.detail_errors.add('synthetic-1')
        result=get_unread_emails(service)
        self.assertEqual([r['id'] for r in result.emails],['synthetic-0','synthetic-2'])
        self.assertEqual(result.status,'partial')
        self.assertEqual(result.summary()['failed_count'],1)

    def test_pagination_and_batch_cap(self):
        service=mailbox(8)
        result=get_unread_emails(service,max_results=5,page_size=2,max_pages=5)
        self.assertEqual(len(result.emails),5)
        self.assertEqual([r['maxResults'] for r in service.lists],[2,2,1])
        self.assertEqual(result.next_page_token,'5')
        self.assertTrue(result.has_more)

    def test_page_cap_returns_a_continuation(self):
        service=mailbox(8)
        result=get_unread_emails(service,max_results=8,page_size=2,max_pages=2)
        self.assertEqual(len(result.emails),4)
        self.assertEqual(result.pages,2)
        self.assertEqual(result.next_page_token,'4')

    def test_later_page_failure_preserves_earlier_success(self):
        service=mailbox(4)
        original=service.list
        def listing(**kwargs):
            if kwargs.get('pageToken'): raise RuntimeError('synthetic error')
            return original(**kwargs)
        service.list=listing
        result=get_unread_emails(service,page_size=2)
        self.assertEqual(result.status,'partial')
        self.assertEqual(len(result.emails),2)
        self.assertEqual(result.next_page_token,'2')

    def test_invalid_cursor_is_reset(self):
        service=mailbox(1)
        error=RuntimeError('synthetic invalid cursor')
        error.resp=Mock(status=400)
        service.list_error=error
        result=get_unread_emails(service,page_token='stale-token')
        self.assertEqual(result.status,'error')
        self.assertIsNone(result.next_page_token)

    def test_exhausted_cursor_rechecks_first_page_if_budget_allows(self):
        result=get_unread_emails(mailbox(2),page_token='100',max_pages=2)
        self.assertEqual(result.status,'success')
        self.assertEqual(len(result.emails),2)
        result=get_unread_emails(mailbox(2),page_token='100',max_pages=1)
        self.assertEqual(result.status,'deferred')
        self.assertIsNone(result.next_page_token)

    def test_repeated_page_token_and_duplicate_ids_are_bounded(self):
        service=mailbox(1)
        service.list=lambda **kwargs: FakeRequest({'messages':[{'id':'synthetic-0'}],'nextPageToken':'repeat'})
        result=get_unread_emails(service,max_pages=10)
        self.assertEqual(len(result.emails),1)
        self.assertEqual(len(service.gets),1)
        self.assertEqual(result.status,'partial')
        self.assertIsNone(result.next_page_token)

    def test_missing_ids_and_bad_listing_shapes_are_errors(self):
        service=mailbox(0)
        service.list=lambda **kwargs: FakeRequest({'messages':[{}]})
        self.assertEqual(get_unread_emails(service).failures,[{'code':'missing_message_id'}])
        service.list=lambda **kwargs: FakeRequest({'messages':'invalid'})
        self.assertEqual(get_unread_emails(service).status,'error')

    def test_oversized_provider_list_does_not_bypass_batch_limit(self):
        service=mailbox(10)
        service.list=lambda **kwargs: FakeRequest({'messages':[{'id':key} for key in service.items]})
        result=get_unread_emails(service,max_results=2,page_size=2)
        self.assertEqual(len(service.gets),2)
        self.assertEqual(result.status,'partial')

    def test_guard_cancels_between_provider_requests(self):
        service=mailbox(5)
        calls=0
        @contextmanager
        def guard():
            nonlocal calls
            calls+=1
            if calls==3: raise WorkCancelled()
            yield
        with self.assertRaises(WorkCancelled):
            get_unread_emails(service,before_request=guard)
        self.assertEqual(len(service.gets),1)

    def test_external_inline_body_is_fetched_but_file_attachment_is_not(self):
        body=leaf()
        body['body']={'attachmentId':'inline-body'}
        file=leaf(filename='secret.txt')
        file['body']={'attachmentId':'file-id'}
        service=Mailbox({'synthetic':{'payload':{'mimeType':'multipart/mixed','parts':[file,body]}}})
        service.inline_data={'inline-body':base64.urlsafe_b64encode(b'Real inline body').decode()}
        result=get_unread_emails(service)
        self.assertEqual(result.emails[0]['body'],'Real inline body')
        self.assertEqual([r['id'] for r in service.inlines],['inline-body'])

    def test_external_inline_fetch_is_bounded(self):
        parts=[]
        for i in range(4):
            part=leaf()
            part['body']={'attachmentId':str(i)}
            parts.append(part)
        loader=Mock(return_value=base64.urlsafe_b64encode(b'Good text').decode())
        result=parse_body({'mimeType':'multipart/mixed','parts':parts},inline_loader=loader)
        self.assertEqual(loader.call_count,2)
        self.assertIn('inline_fetch_limit',result.warnings)

    def test_settings_are_configurable_and_reject_bad_limits(self):
        self.assertEqual(Settings().poll_interval_seconds,5)
        with patch.dict('os.environ',{'MAILMIND_POLL_INTERVAL_SECONDS':'90','MAILMIND_BATCH_SIZE':'7','MAILMIND_GMAIL_PAGE_SIZE':'3','MAILMIND_GMAIL_MAX_PAGES':'2'}):
            settings=Settings.from_environment()
        self.assertEqual((settings.poll_interval_seconds,settings.batch_size,settings.gmail_page_size,settings.gmail_max_pages),(90,7,3,2))
        for change in ({'batch_size':0},{'batch_size':201},{'poll_interval_seconds':1},{'gmail_max_pages':21},{'gmail_page_size':True}):
            with self.subTest(change=change),self.assertRaises(ValueError): replace(Settings(),**change)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mailmind-phase3-')
        self.addCleanup(self.temp.cleanup)
        self.settings=Settings(data_dir=Path(self.temp.name),batch_size=2,gmail_page_size=1,gmail_max_pages=2,auto_mark_read=True)
        initialize_database(self.settings.db_path)
        self.manager=AccountManager(self.settings)
        token,_=self.manager.open_session()
        context,_=self.manager.session(token)
        self.manager.finish_auth(self.manager.begin_auth(context),(A,'{}'))
        self.model=Mock(model_loaded=True)
        self.model.predict.return_value=Prediction(category='IMPORTANT',outcome='CLASSIFIED')
        self.collection=Mock()

    def run_cycle(self, service, decision='UPDATES'):
        from src import main
        with patch.object(main,'refresh_gmail',return_value=service),patch.object(main,'classify_email',return_value=Prediction(category=decision if decision != 'ERROR' else None,outcome='CLASSIFIED' if decision != 'ERROR' else 'ERROR',source='gemini')) as cloud,patch.object(main,'send_telegram_alert',return_value=True):
            result=main._run_agent(settings=self.settings,manager=self.manager,model=self.model,collection=self.collection)
        return result,cloud

    def test_late_urgent_text_reaches_cloud_local_storage_and_feedback(self):
        # This checks the local shadow sees the whole text, so switch it on.
        self.settings=replace(self.settings,shadow_model_enabled=True)
        self.manager.settings=self.settings
        body='Intro. '*70+'URGENT interview tomorrow.'
        service=Mailbox({'synthetic-long':{'payload':leaf(body)}})
        result,cloud=self.run_cycle(service)
        self.assertEqual(result['processed_count'],1)
        self.assertIn('URGENT',cloud.call_args.args[2])
        self.assertIn('URGENT',self.model.predict.call_args.args[1])
        row=get_email('synthetic-long',account_id=A,db_path=self.settings.db_path)
        self.assertIn('URGENT',row['body'])
        from src.vector_db import add_email_to_vector_db
        add_email_to_vector_db('synthetic-long',row['subject'],row['body'],'IMPORTANT',account_id=A,collection=self.collection)
        self.assertIn('URGENT',self.collection.upsert.call_args.kwargs['documents'][0])

    def test_backlog_drains_with_bounded_work_despite_shifting_offsets(self):
        service=mailbox(7)
        for _ in range(7):
            old_lists,old_gets=len(service.lists),len(service.gets)
            self.run_cycle(service)
            self.assertLessEqual(len(service.lists)-old_lists,2)
            self.assertLessEqual(len(service.gets)-old_gets,2)
            if not service.unread: break
        self.assertEqual(service.unread,[])
        self.assertEqual(len(service.gets),7)

    def test_live_cycle_peeks_newest_page_and_time_slices_processing(self):
        from src import main
        service=mailbox(4)
        with self.manager.transaction() as conn:
            save_ingestion_state(A,FetchBatch(next_page_token='2',has_more=True),conn)
            conn.execute("UPDATE ingestion_state SET backlog_authorized=1,backlog_remaining=2 WHERE account_id=?",(A,))
        with patch.object(main,'refresh_gmail',return_value=service), \
             patch.object(main,'classify_email',return_value=Prediction(category='UPDATES',outcome='CLASSIFIED',source='gemini')), \
             patch.object(main,'send_telegram_alert',return_value=True):
            result=main._run_agent(settings=self.settings,manager=self.manager,model=self.model,
                collection=self.collection,newest_first=True,task_limit=1)
        self.assertNotIn('pageToken',service.lists[0])
        self.assertEqual(service.lists[1]['pageToken'],'2')
        self.assertEqual((result['fetched_count'],result['attempted_count']),(3,1))
        self.assertIsNotNone(get_email('synthetic-0',account_id=A,db_path=self.settings.db_path))
        self.assertIsNotNone(get_email('synthetic-2',account_id=A,db_path=self.settings.db_path))

    def test_initial_history_cursor_is_saved_without_crashing_the_cycle(self):
        from src import main
        with patch.object(main,'get_gmail_history_id',return_value='synthetic-history'):
            result,_=self.run_cycle(mailbox(1))
        with self.manager.transaction() as conn:
            state=get_ingestion_state(A,conn)
            task=conn.execute('SELECT source FROM processing_tasks WHERE account_id=? AND email_id=?',
                              (A,'synthetic-0')).fetchone()
        self.assertEqual(state['history_id'],'synthetic-history')
        self.assertEqual(task['source'],'live')
        self.assertIsNotNone(get_email('synthetic-0',account_id=A,db_path=self.settings.db_path))
        self.assertEqual(result['processing_status'],'complete')

    def test_existing_history_cursor_gets_one_time_newest_mail_catch_up(self):
        from src import main
        with self.manager.transaction() as conn:
            save_ingestion_state(A,FetchBatch(has_more=True),conn)
            conn.execute('UPDATE ingestion_state SET history_id=? WHERE account_id=?',
                         ('already-seeded',A))
        with patch.object(main,'get_gmail_history_id') as cursor:
            self.run_cycle(mailbox(1))
        cursor.assert_not_called()
        with self.manager.transaction() as conn:
            state=get_ingestion_state(A,conn)
            task=conn.execute('SELECT source FROM processing_tasks WHERE account_id=? AND email_id=?',
                              (A,'synthetic-0')).fetchone()
        self.assertEqual(state['history_id'],'already-seeded')
        self.assertEqual(state['history_bootstrap_complete'],1)
        self.assertEqual(task['source'],'live')

    def test_first_batch_is_bounded_and_pauses_after_one_hundred_admissions(self):
        from src import main
        settings=replace(self.settings,batch_size=100,gmail_page_size=100,gmail_max_pages=1,
                         max_pending_tasks=100,resume_pending_tasks=50)
        self.manager.settings=settings
        service=mailbox(120)
        with patch.object(main,'refresh_gmail',return_value=service), \
             patch.object(main,'classify_email',return_value=Prediction(category='UPDATES',outcome='CLASSIFIED',source='gemini')), \
             patch.object(main,'send_telegram_alert',return_value=True):
            main._run_agent(settings=settings,manager=self.manager,model=self.model,
                            collection=self.collection,task_limit=1)
        with connection(settings.db_path) as conn:
            counts=conn.execute("""SELECT COUNT(*) AS total,
                SUM(status IN ('queued','retry','running')) AS active,
                SUM(source='backlog') AS backlog FROM processing_tasks""").fetchone()
            state=get_ingestion_state(A,conn)
        self.assertEqual((counts['total'],counts['backlog']),(100,100))
        self.assertLessEqual(counts['active'],100)
        self.assertEqual((state['backlog_authorized'],state['backlog_remaining'],state['initial_batch_complete']),(0,0,1))

    def test_new_mail_uses_freed_capacity_and_is_processed_before_backlog(self):
        from src import main
        settings=replace(self.settings,batch_size=3,gmail_page_size=3,gmail_max_pages=1,
                         max_pending_tasks=3,resume_pending_tasks=1)
        self.manager.settings=settings
        service=mailbox(5)
        def cycle():
            with patch.object(main,'refresh_gmail',return_value=service), \
                 patch.object(main,'classify_email',return_value=Prediction(category='UPDATES',outcome='CLASSIFIED',source='gemini')), \
                 patch.object(main,'send_telegram_alert',return_value=True):
                return main._run_agent(settings=settings,manager=self.manager,model=self.model,
                                       collection=self.collection,task_limit=1)
        cycle()
        service.items['live-new']={'payload':leaf('Newest live message')}
        service.unread.insert(0,'live-new')
        cycle()
        with connection(settings.db_path) as conn:
            live=conn.execute("SELECT status,source FROM processing_tasks WHERE email_id='live-new'").fetchone()
            active=conn.execute("SELECT COUNT(*) FROM processing_tasks WHERE status IN ('queued','retry','running')").fetchone()[0]
        self.assertEqual((live['status'],live['source']),('complete','live'))
        self.assertLessEqual(active,3)

    def test_bad_message_does_not_block_following_messages(self):
        service=mailbox(3)
        service.items['synthetic-0']['payload']['body']['data']='a'
        for _ in range(4): self.run_cycle(service)
        self.assertEqual(service.unread,['synthetic-0'])

    def test_failed_listing_is_saved_as_error_and_polling_resets(self):
        service=mailbox(0)
        service.list_error=RuntimeError('synthetic failure')
        result,_=self.run_cycle(service)
        self.assertEqual(result['status'],'error')
        with self.manager.transaction() as conn:
            state=get_ingestion_state(A,conn)
            self.assertEqual(state['status'],'error')
            self.assertEqual(state['listing_error'],1)
            self.assertEqual(self.manager.state(conn)['is_polling'],0)

    def test_classification_failure_rewinds_unprocessed_batch(self):
        service=mailbox(4)
        self.run_cycle(service,decision='ERROR')
        with self.manager.transaction() as conn:
            self.assertIsNone(get_ingestion_state(A,conn)['page_token'])
        self.assertEqual(len(service.unread),4)

    def test_truncation_and_parse_metadata_are_persisted(self):
        service=Mailbox({'synthetic-limit':{'payload':leaf('x'*(MAX_BODY_CHARS+20))}})
        self.run_cycle(service)
        row=get_email('synthetic-limit',account_id=A,db_path=self.settings.db_path)
        self.assertEqual(len(row['body']),MAX_BODY_CHARS)
        self.assertEqual(row['body_truncated'],1)
        self.assertIn('body_character_limit',row['parse_warnings'])

    def test_account_transition_rewinds_its_cursor_and_purge_removes_state(self):
        self.run_cycle(mailbox(4))
        with self.manager.transaction() as conn:
            self.assertIsNotNone(get_ingestion_state(A,conn)['page_token'])
        token,_=self.manager.open_session()
        context,_=self.manager.session(token)
        context=self.manager.disconnect(context)
        with self.manager.transaction() as conn:
            self.assertIsNone(get_ingestion_state(A,conn)['page_token'])
        self.manager.purge(context,self.collection)
        with self.manager.transaction() as conn:
            self.assertIsNone(get_ingestion_state(A,conn))

    def test_api_status_is_account_scoped_and_hides_cursor(self):
        app=create_app(settings=self.settings,model_factory=Mock(return_value=self.model),vector_factory=Mock(return_value=self.collection),oauth_factory=Mock(return_value=(B,'{}')))
        with self.manager.transaction() as conn:
            save_ingestion_state(A,FetchBatch(listing_error=True,next_page_token='synthetic-secret-cursor'),conn)
        with TestClient(app,base_url='http://localhost') as client:
            session=client.post('/session',headers={'Origin':'http://localhost:5173'})
            client.headers.update({'Origin':'http://localhost:5173','X-CSRF-Token':session.json()['csrf_token']})
            response=client.post('/authenticate')
            app.state.auth_futures[response.json()['job_id']].result(timeout=5)
            self.assertIsNone(client.get('/status').json()['ingestion'])
            with self.manager.transaction() as conn:
                save_ingestion_state(B,FetchBatch(listing_error=True,next_page_token='private-cursor'),conn)
            response=client.get('/status')
            self.assertEqual(response.json()['ingestion']['status'],'error')
            self.assertNotIn('page_token',response.text)
            self.assertNotIn('private-cursor',response.text)

    def test_schema_three_upgrade_preserves_saved_body(self):
        path=self.settings.data_dir/'version3.db'
        with connection(path) as conn:
            _migration_1(conn)
            _migration_2(conn)
            _migration_3(conn)
            conn.execute('PRAGMA user_version=3')
            conn.execute("INSERT INTO email_logs(email_id,body,created_at) VALUES ('synthetic-old','original saved body',?)",(utc_timestamp(),))
        initialize_database(path)
        initialize_database(path)
        with connection(path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
            row=conn.execute('SELECT body,body_truncated FROM email_logs').fetchone()
            self.assertEqual(tuple(row),('original saved body',0))

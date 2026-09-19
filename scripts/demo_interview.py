"""Synthetic engineering rehearsal. Providers/similarity are fake; APIs/storage/recovery are real."""
import argparse,json,socket,tempfile
from pathlib import Path
from unittest.mock import Mock,patch
from fastapi.testclient import TestClient
from api.app import create_app
from src.config import Settings
from src.prediction import Prediction
from src.notifier import Delivery
from src.email_client import FetchBatch
from src import main
from src.vector_db import VectorService
from src.feedback import current_vector
from src.database import connection
from tests.test_phase2_security import FakeCollection


def demo():
    steps=[]
    def check(condition,name):
        if not condition:raise AssertionError(name)
        steps.append({'check':name,'passed':True})
    with tempfile.TemporaryDirectory(prefix='mailmind-interview-demo-') as directory:
        settings=Settings(data_dir=Path(directory),access_key='synthetic-interview-demo-pairing',auto_mark_read=True)
        collection=FakeCollection();vector=VectorService(lambda:collection,lambda metadata:current_vector(metadata,settings.db_path))
        class DemoModel:
            model_loaded=False;model_version='fake-demo-fallback';training_scope=None
            def predict(self,subject,body,*,account_id=None):
                return vector.get_knn_prediction(subject,body,account_id=account_id) or Prediction(category='SPAM',outcome='CLASSIFIED',source='fake-demo-fallback')
        model=DemoModel();oauth=Mock(return_value=('demo-a@example.test','{}'))
        app=create_app(settings=settings,model_factory=lambda **kwargs:model,vector_factory=lambda path:collection,oauth_factory=oauth)
        rows=[{'id':label.lower(),'sender':'Synthetic sender','subject':label+' example','body':'Synthetic original body'} for label in ['IMPORTANT','UPDATES','SPAM']]
        rows+=[{'id':'receipt-'+str(index),'sender':'Shop '+str(index),'subject':'Receipt '+str(index),'body':'Payment received. No response required.'} for index in range(3)]
        counts={}
        def classify(sender,subject,body,**kwargs):
            counts[subject]=counts.get(subject,0)+1
            if subject=='UPDATES example' and counts[subject]==1:return Prediction(outcome='ERROR',source='fake-cloud',reason='provider_transient')
            category=subject.split()[0] if subject.split()[0] in ['IMPORTANT','UPDATES','SPAM'] else 'SPAM'
            return Prediction(category=category,outcome='CLASSIFIED',source='fake-cloud',model_version='synthetic-provider-v1')
        alerts=Mock(side_effect=[Delivery('retry','provider_transient'),Delivery('sent',message_id='synthetic-alert')]);marker=Mock(return_value=True)
        with patch.object(socket,'create_connection',side_effect=AssertionError('No network')),patch.object(socket,'getaddrinfo',side_effect=AssertionError('No DNS')),TestClient(app,base_url='http://localhost') as client:
            client.headers['Origin']='http://localhost:5173';paired=client.post('/session',json={'code':settings.access_key});client.headers['X-CSRF-Token']=paired.json()['csrf_token']
            def connect(account):
                oauth.return_value=(account,'{}');response=client.post('/authenticate');check(response.status_code==202,'Fake OAuth accepted for '+account);app.state.auth_futures[response.json()['job_id']].result(timeout=5)
            connect('demo-a@example.test');manager=app.state.accounts
            def cycle(batch):
                with patch.object(main,'refresh_gmail',return_value=object()),patch.object(main,'get_unread_emails',return_value=FetchBatch(emails=batch)),patch.object(main,'classify_email',side_effect=classify),patch.object(main,'send_telegram_alert',alerts),patch.object(main,'mark_as_read',marker):
                    return main._run_agent(settings=settings,manager=manager,model=model,collection=collection)
            cycle(rows)
            with connection(settings.db_path) as conn:
                task=conn.execute("SELECT status FROM processing_tasks WHERE email_id='important'").fetchone();check(task['status']=='retry','Failed alert remains retryable')
                check(conn.execute("SELECT status FROM processing_tasks WHERE email_id='updates'").fetchone()['status']=='retry','Failed provider remains retryable')
                conn.execute('UPDATE processing_tasks SET next_retry_at=0');conn.execute('UPDATE notification_outbox SET next_retry_at=0')
            cycle([])
            saved=client.get('/emails').json()['emails'];check({row['effective_category'] for row in saved}=={'IMPORTANT','UPDATES','SPAM'},'All three categories are stored')
            check(counts['IMPORTANT example']==1,'Alert recovery does not classify again')
            before=client.post('/predict',json={'subject':'New receipt','body':'Payment received. No response required.'}).json();check(before['category']=='SPAM','Fake initial receipt error is visible')
            for index in range(3):
                response=client.post('/feedback',json={'email_id':'receipt-'+str(index),'label':'UPDATES'});check(response.status_code==202,'Receipt correction queued '+str(index))
            check(client.post('/feedback/reconcile').json()['indexed']==3,'Queued receipt corrections are indexed')
            after=client.post('/predict',json={'subject':'New receipt','body':'Payment received. No response required.'}).json();check(after['category']=='UPDATES' and after['source']=='retrieval' and after['support']==3,'Three current corrections support the receipt result')
            connect('demo-b@example.test');check(client.get('/emails').json()['total']==0,'Switching accounts hides the first account records')
            check(client.post('/feedback',json={'email_id':'receipt-0','label':'SPAM'}).status_code==404,'Other-account correction is rejected')
            check(client.post('/disconnect').status_code==200,'Disconnect succeeds without a live provider')
            check(not client.get('/session').json()['connected'],'Disconnect pauses Google work')
            with connection(settings.db_path) as conn:check(conn.execute('SELECT COUNT(*) FROM email_logs WHERE account_id=?',('demo-a@example.test',)).fetchone()[0]==6,'First account records are retained safely')
    return {'scope':'synthetic engineering demo; mocked providers and similarity; no model accuracy claim','live_network_or_private_data':False,'checks':steps,'all_passed':True}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',default=None);args=parser.parse_args();result=demo()
    if args.report:
        path=Path(args.report);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))

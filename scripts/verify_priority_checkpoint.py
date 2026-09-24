"""Verify a deliberately selected checkpoint through actual private API routes."""
import argparse
import json
import tempfile
from pathlib import Path
from src.local_llm import MailMindModel
from src.config import Settings
from src.prediction import LABEL2ID
from api.app import create_app
from fastapi.testclient import TestClient
from tests.test_phase2_security import FakeCollection

class NoRetrieval:
    def get_knn_prediction(self,*args,**kwargs): return None


def verify(model_path,report_dir,dataset):
    import torch
    torch.set_num_threads(2)
    split=json.loads((Path(report_dir)/'split_manifest.json').read_text(encoding='utf-8'))
    ids=set(split['splits']['test']['ids'])
    rows=[row for row in json.loads((Path(dataset)/'emails.json').read_text(encoding='utf-8')) if row['id'] in ids]
    examples=sorted(rows,key=lambda row:row['id'])
    expected={row['id']:row['personalized_local'] for row in json.loads((Path(report_dir)/'test_predictions.json').read_text(encoding='utf-8'))}
    with tempfile.TemporaryDirectory(prefix='mailmind-model-smoke-') as temp:
        settings=Settings(data_dir=Path(temp),model_path=Path(model_path))
        collection=FakeCollection()
        app=create_app(settings=settings,model_factory=lambda model_path,**kwargs:MailMindModel(model_path,vector_service=NoRetrieval()),vector_factory=lambda path:collection)
        with TestClient(app,base_url='http://localhost') as client:
            client.headers['Origin']='http://localhost:5173'
            response=client.post('/session')
            client.headers['X-CSRF-Token']=response.json()['csrf_token']
            context,_=app.state.accounts.session(client.cookies.get('mailmind_session'))
            app.state.accounts.finish_auth(app.state.accounts.begin_auth(context),('synthetic-smoke@example.test','{}'))
            results=[]
            for row in examples:
                response=client.post('/predict',json={'subject':row['subject'],'body':row['body']})
                payload=response.json()
                if response.status_code!=200 or payload.get('outcome')!='CLASSIFIED' or payload.get('category')!=expected[row['id']]:
                    raise ValueError('Runtime prediction differs from recorded held-out evaluation')
                if 'synthetic_benchmark_only' not in payload.get('limitations',[]): raise ValueError('API did not retain synthetic scope')
                results.append({'id':row['id'],'truth':row['human_label'],'expected_runtime_category':expected[row['id']],**payload})
            if {row['category'] for row in results} != set(LABEL2ID): raise ValueError('Runtime dropped a supported category')
            telemetry=client.get('/telemetry').json()
            if not telemetry['local_model']['ready'] or telemetry['local_model']['evaluation_scope']!='synthetic_benchmark_only': raise ValueError('Readiness or scope is incorrect')
    output={'model_path':str(model_path),'account':'synthetic-smoke@example.test','scope':'synthetic_benchmark_only','results':results,'local_model':telemetry['local_model']}
    Path(report_dir,'runtime_smoke.json').write_text(json.dumps(output,indent=2)+'\n',encoding='utf-8')
    return output

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',default='models/phase7-synthetic-v2/personal')
    parser.add_argument('--report-dir',default='docs/evaluation/phase7')
    parser.add_argument('--dataset',default='fixtures/priority_benchmark')
    args=parser.parse_args();print(json.dumps(verify(args.model,args.report_dir,args.dataset),indent=2))

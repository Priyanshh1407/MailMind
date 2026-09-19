"""Verify real cached embedding/classifier and saved-task processing with Python networking blocked."""
import argparse,json,os,socket,tempfile,subprocess,sys
from pathlib import Path
from unittest.mock import patch


def verify_workspace(model_path,embedding_cache,report_path,workspace):
    # The actual local-model factory applies offline/telemetry flags itself.
    from scripts.prepare_offline_assets import prepare
    from src.offline_assets import offline_embedding
    from src.config import Settings
    from src.prediction import LABEL2ID
    from api.app import create_app
    from fastapi.testclient import TestClient
    from src import main
    from src.work_queue import claim_cycle,ingest_email,finish_cycle
    from src.database import connection
    import torch
    torch.set_num_threads(2)
    if True:
        root=Path(workspace);manifest=prepare(model_path,embedding_cache,root/'assets')
        settings=Settings(data_dir=root/'data',model_path=root/'assets/classifier',asset_manifest_path=manifest,local_only=True,access_key='synthetic-offline-pairing-code')
        def forbidden(*args,**kwargs):raise AssertionError('External networking forbidden in offline verification')
        original_connect=socket.socket.connect;original_connect_ex=socket.socket.connect_ex
        def local_connect(sock,address):
            if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'):return original_connect(sock,address)
            return forbidden()
        def local_connect_ex(sock,address):
            if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'):return original_connect_ex(sock,address)
            return forbidden()
        with patch.dict(os.environ,{'GEMINI_API_KEY':'','GROQ_API_KEY':'','TELEGRAM_BOT_TOKEN':'','TELEGRAM_CHAT_ID':''}),patch.object(socket.socket,'connect',local_connect),patch.object(socket.socket,'connect_ex',local_connect_ex),patch.object(socket,'create_connection',forbidden),patch.object(socket,'getaddrinfo',forbidden),patch('requests.sessions.Session.request',forbidden),patch.object(main,'refresh_gmail',forbidden),patch.object(main,'classify_email',forbidden),patch.object(main,'send_telegram_alert',forbidden),patch.object(main,'mark_as_read',forbidden):
            embedding=offline_embedding(manifest)
            vectors=embedding(['Synthetic local receipt. No action required.'])
            if len(vectors)!=1 or len(vectors[0])!=384:raise ValueError('Embedding not available locally')
            app=create_app(settings=settings)
            with TestClient(app,base_url='http://localhost') as client:
                client.headers['Origin']='http://localhost:5173'
                paired=client.post('/session',json={'code':settings.access_key});client.headers['X-CSRF-Token']=paired.json()['csrf_token']
                prediction=client.post('/predict',json={'subject':'Synthetic receipt','body':'Payment was received. No response is required.'})
                if prediction.status_code!=200 or prediction.json().get('category') not in LABEL2ID:raise ValueError('Actual classifier not available offline')
                manager=app.state.accounts;context,_=manager.session(client.cookies.get('mailmind_session'))
                manager.finish_auth(manager.begin_auth(context),('synthetic-offline@example.test','{}'))
                context=manager.worker_context();token,job=claim_cycle(manager,context)
                ingest_email({'id':'offline-synthetic','sender':'Synthetic sender','subject':'Synthetic receipt','body':'Payment was received. No response is required.'},manager,context,token)
                finish_cycle(manager,context,token,job)
                cycle=main._run_agent(settings=settings,manager=manager,model=app.state.model)
                with connection(settings.db_path) as conn:
                    task=dict(conn.execute('SELECT * FROM processing_tasks WHERE email_id=?',('offline-synthetic',)).fetchone())
                telemetry=client.get('/telemetry').json()
                if task['category'] not in LABEL2ID or not telemetry['mode']['local_only']:raise ValueError('Saved classification did not run locally')
        result={'scope':'synthetic_integration_only','classifier_scope':telemetry['local_model']['evaluation_scope'],'python_external_socket_and_requests_networking':'blocked; loopback permitted for Windows event-loop pipes','cloud_credentials':'empty','actual_embedding_dimensions':len(vectors[0]),'actual_prediction':prediction.json(),'saved_task':{key:task[key] for key in ['status','stage','category','error_code']},'cycle':cycle,'local_mode':telemetry['mode'],'note':'Python network calls and known provider adapters were blocked, not an OS firewall. Local Chroma telemetry disabled. This does not validate real-inbox ML quality.'}
    report_path=Path(report_path);report_path.parent.mkdir(parents=True,exist_ok=True);report_path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');return result


def verify(model_path,embedding_cache,report_path):
    # Chroma keeps native handles until process shutdown on Windows. Let a
    # bounded child finish before cleaning this exclusively synthetic folder.
    with tempfile.TemporaryDirectory(prefix='mailmind-phase8-offline-') as directory:
        temporary_report=Path(directory)/'result.json'
        subprocess.run([sys.executable,'-m','scripts.verify_local_only','--model',str(model_path),'--embedding-cache',str(embedding_cache),'--report',str(temporary_report),'--workspace',directory],check=True,timeout=180)
        result=json.loads(temporary_report.read_text(encoding='utf-8'))
    report_path=Path(report_path);report_path.parent.mkdir(parents=True,exist_ok=True)
    report_path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--model',required=True);parser.add_argument('--embedding-cache',required=True);parser.add_argument('--report',default='docs/evaluation/phase8/local_only_smoke.json');parser.add_argument('--workspace',help=argparse.SUPPRESS)
    args=parser.parse_args()
    result=verify_workspace(args.model,args.embedding_cache,args.report,args.workspace) if args.workspace else verify(args.model,args.embedding_cache,args.report)
    print(json.dumps(result,indent=2))

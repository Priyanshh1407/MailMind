"""Build/run only an isolated local-only Compose project and remove its synthetic resources."""
import http.cookiejar,json,secrets,subprocess,tempfile,time
from pathlib import Path
from urllib.request import Request,build_opener,HTTPCookieProcessor,urlopen
from urllib.error import HTTPError
from scripts.launch import ROOT,check_ports


def verify(report='docs/evaluation/phase9/container_smoke.json'):
    check_ports();project='mailmind-phase9-check-'+secrets.token_hex(4)
    with tempfile.TemporaryDirectory(prefix='mailmind-compose-check-') as directory:
        override=Path(directory)/'override.yml';override.write_text('services: {}\n',encoding='utf-8')
        base=['docker','compose','-p',project,'-f',str(ROOT/'docker-compose.yml'),'-f',str(override)]
        def command(*args):return subprocess.run([*base,*args],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=900,encoding='utf-8',errors='replace').stdout
        try:
            command('up','-d','--build','--wait','--wait-timeout','180')
            cookie=http.cookiejar.CookieJar();opener=build_opener(HTTPCookieProcessor(cookie))
            def api(path,body=None):
                request=Request('http://127.0.0.1:8000'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:5173'})
                if body is not None and path!='/session':request.add_header('X-CSRF-Token',csrf)
                with opener.open(request,timeout=30) as response:return response.status,json.load(response)
            _,session=api('/session',{});csrf=session['csrf_token']
            _,prediction=api('/predict',{'subject':'Synthetic receipt','body':'Payment was received. No response is required.'})
            if prediction.get('outcome')!='CLASSIFIED' or 'synthetic_benchmark_only' not in prediction.get('limitations',[]):raise AssertionError('Verified demo checkpoint did not run in container')
            _,telemetry=api('/telemetry')
            if not telemetry['mode']['local_only']:raise AssertionError('Container not local-only')
            for path in ['/authenticate','/inbox/sync']:
                try:api(path,{});raise AssertionError('External action was allowed')
                except HTTPError as error:
                    if error.code!=403:raise
            uid=command('exec','-T','api','python','-c','import os;print(os.getuid())').strip()
            if uid!='10001':raise AssertionError('Backend not running as nonroot')
            with urlopen('http://127.0.0.1:5173/',timeout=5) as response:ui=response.status
            command('exec','-T','api','python','-c',"from src.db_utils import log_email_to_db;log_email_to_db('phase9-persistence','synthetic@example.test','Synthetic retained receipt','Public demo only','UPDATES','UPDATES',account_id='phase9@example.test',db_path='/app/data/email_logs.db')")
            command('restart','api');deadline=time.monotonic()+90
            while True:
                try:
                    with urlopen('http://127.0.0.1:8000/',timeout=5) as response:
                        if response.status==200:break
                except OSError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.2)
            try:api('/telemetry');raise AssertionError('Restart did not invalidate old session')
            except HTTPError as error:
                if error.code!=401:raise
            _,session=api('/session',{});csrf=session['csrf_token']
            api('/telemetry')
            retained=command('exec','-T','api','python','-c',"from src.db_utils import get_email;print(get_email('phase9-persistence',account_id='phase9@example.test',db_path='/app/data/email_logs.db')['subject'])").strip()
            if retained!='Synthetic retained receipt':raise AssertionError('Saved mail lost after restart')
            result={'all_passed':True,'scope':'real CPU Linux containers and public cached assets; no Gmail/cloud/Telegram calls','services':['api','worker','frontend'],'api_uid':int(uid),'ui_status':ui,'model_prediction':prediction['category'],'model_scope':telemetry['local_model']['evaluation_scope'],'local_only':True,'external_actions':'rejected','sqlite_saved_mail_survives_api_restart':True,'old_session_rejected_after_restart':True,'new_session_after_restart':'passed','compose_health_dependencies':'passed','private_build_context':'excluded by allowlist','network_note':'Containers retain networking for localhost/health; local-only adapters block providers. Not an OS egress-firewall audit.'}
        except subprocess.CalledProcessError as error:
            print(error.stdout[-12000:]);raise
        finally:
            # This unique project contains only resources created above; never
            # stop another project or delete its volumes.
            command('down','--volumes','--remove-orphans')
    path=Path(report);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');return result

if __name__=='__main__':print(json.dumps(verify(),indent=2))

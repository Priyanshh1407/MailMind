"""Start/stop only the actual owned stack using isolated local-only synthetic storage."""
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
from scripts.launch import ROOT,check_ports,read_state,stop,owner_alive


def verify(report='docs/evaluation/phase9/launcher_smoke.json'):
    check_ports()
    with tempfile.TemporaryDirectory(prefix='mailmind-launcher-check-') as directory:
        root=Path(directory);env=os.environ.copy();env.update({'MAILMIND_LOCAL_ONLY':'true','MAILMIND_DATA_DIR':str(root/'synthetic-data'),'MAILMIND_MODEL_PATH':str(root/'missing-demo-model'),'MAILMIND_ASSET_MANIFEST':str(root/'missing-manifest.json'),'MAILMIND_AUTO_MARK_READ':'false'})
        log=root/'supervisor.log'
        with log.open('w',encoding='utf-8') as stream:
            flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            process=subprocess.Popen([sys.executable,'-m','scripts.launch','start','--state-dir',str(root/'state')],cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,creationflags=flags)
        try:
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError(log.read_text())
                if 'MailMind ready' in log.read_text():break
                time.sleep(.1)
            else:raise RuntimeError('Supervisor readiness timed out')
            state=read_state(root/'state/services.json')
            request=Request(f"http://127.0.0.1:{state['control_port']}/stop",data=b'',method='POST')
            try:urlopen(request,timeout=2);raise AssertionError('Unauthenticated shutdown accepted')
            except __import__('urllib.error',fromlist=['HTTPError']).HTTPError as error:
                if error.code!=403:raise
            with urlopen('http://127.0.0.1:8000/',timeout=2) as response:api=json.load(response)
            with urlopen('http://127.0.0.1:5173/',timeout=2) as response:ui=response.status
            stopped=stop(root/'state');process.wait(timeout=70)
            if process.returncode!=0:raise RuntimeError(log.read_text())
            summary=json.loads(log.read_text().splitlines()[-1])
            if summary['forced_terminations']!=0 or (root/'state/services.json').exists():raise AssertionError('Graceful cleanup failed')
            check_ports()
            result={'all_passed':True,'scope':'actual stack; isolated synthetic local-only storage; no cloud/Gmail calls or real model loading','api_ready':api['message']=='MailMind API is online.','degraded_model_is_honest':api['model_loaded'] is False,'ui_status':ui,'unauthenticated_stop':'rejected','stop_acknowledged':stopped['status'],'shutdown':summary,'ports_released':True}
        finally:
            if process.poll() is None:
                try:stop(root/'state');process.wait(timeout=70)
                except Exception:process.terminate();process.wait(timeout=5)
    path=Path(report);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');return result

if __name__=='__main__':print(json.dumps(verify(),indent=2))

"""Foreground supervisor: authenticated stop, owned children and bounded cleanup."""
import argparse,hmac,json,os,secrets,socket,subprocess,sys,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request,urlopen
import psutil

ROOT=Path(__file__).resolve().parents[1]


def check_ports(ports=(8000,5173)):
    held=[]
    try:
        for port in ports:
            sock=socket.socket();held.append(sock);sock.bind(('127.0.0.1',port))
    except OSError:
        raise RuntimeError('A required port is already in use; existing processes were not changed') from None
    finally:
        for sock in held:sock.close()


def owner_alive(entry):
    try:
        process=psutil.Process(entry['pid'])
        return abs(process.create_time()-entry['created'])<.01 and Path(process.cwd()).resolve()==ROOT and '-m' in process.cmdline() and 'scripts.launch' in process.cmdline()
    except (psutil.Error,KeyError,TypeError,ValueError):return False


def read_state(path):
    if path.stat().st_size>16384:raise RuntimeError('Invalid launcher state')
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema')!=1 or value.get('workspace')!=str(ROOT):raise RuntimeError('State does not belong to this workspace')
    return value


def stop(state_dir):
    path=Path(state_dir)/'services.json'
    if not path.exists():return {'status':'not_running'}
    entry=read_state(path)
    if not owner_alive(entry):
        # Never signal a reused PID or infer ownership from a listening port.
        path.unlink();return {'status':'stale_state_removed','processes_signalled':0}
    port=entry['control_port']
    if type(port) is not int or not 1<=port<=65535:raise RuntimeError('Invalid control port')
    request=Request(f'http://127.0.0.1:{port}/stop',data=b'',method='POST',headers={'Authorization':'Bearer '+entry['token']})
    with urlopen(request,timeout=3) as response:
        if response.status!=202:raise RuntimeError('Owned supervisor did not acknowledge stop')
    return {'status':'stopping','message':'Supervisor is draining only its owned services.'}


def wait_ready(process,url,timeout=60,api=False):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if process.poll() is not None:raise RuntimeError('Service exited before becoming ready; inspect its local log')
        try:
            with urlopen(url,timeout=1) as response:
                data=response.read(8192)
                if response.status==200 and (not api or json.loads(data).get('message')=='MailMind API is online.'):return
        except (OSError,ValueError):pass
        time.sleep(.1)
    raise RuntimeError('Service readiness timed out; dependent services were not started')


def shutdown(children,grace=60):
    for process in children:
        if process.poll() is None:
            try:process.stdin.write(b'STOP\n');process.stdin.flush();process.stdin.close()
            except (OSError,ValueError):pass
    deadline=time.monotonic()+grace
    while any(process.poll() is None for process in children) and time.monotonic()<deadline:time.sleep(.1)
    forced=0
    for process in children:
        if process.poll() is None:
            # Popen objects identify our exact children; no global port/name kills.
            process.terminate();forced+=1
    for process in children:
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait();forced+=1
    return forced


def start(state_dir):
    from src.config import Settings
    config=Settings.from_environment(load_file=True)
    state_dir=Path(state_dir);state_dir.mkdir(parents=True,exist_ok=True)
    journal=state_dir/'services.json'
    if journal.exists():raise RuntimeError('Launcher state already exists; use status/stop before starting again')
    check_ports()
    node=__import__('shutil').which('node')
    if not node or not (ROOT/'frontend/dist/index.html').is_file():raise RuntimeError('Build the frontend with npm ci and npm run build before launch')
    event=threading.Event();token=secrets.token_urlsafe(32)
    class Control(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path!='/stop' or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):self.send_error(403);return
            self.send_response(202);self.end_headers();event.set()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Control)
    entry={'schema':1,'workspace':str(ROOT),'pid':os.getpid(),'created':psutil.Process().create_time(),'control_port':server.server_port,'token':token}
    descriptor=os.open(journal,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as stream:json.dump(entry,stream)
    control=threading.Thread(target=server.serve_forever,daemon=True);control.start()
    children=[];logs=[];forced=0
    def spawn(name,command):
        log_path=state_dir/(name+'.log');descriptor=os.open(log_path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600);log=os.fdopen(descriptor,'wb');logs.append(log)
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        process=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,creationflags=flags)
        children.append(process);return process
    try:
        api=spawn('api',[sys.executable,'-m','scripts.service_runner','api'])
        print('Starting API; the normal-mode local model will continue loading in the background...',flush=True)
        wait_ready(api,'http://127.0.0.1:8000/',api=True)
        worker=spawn('worker',[sys.executable,'-m','scripts.service_runner','worker'])
        ui=spawn('frontend',[node,str(ROOT/'frontend/serve.mjs')])
        wait_ready(ui,'http://127.0.0.1:5173/')
        print('MailMind ready at http://127.0.0.1:5173. Pairing code is in the local API log. Keep this supervisor running; use stop or Ctrl+C.',flush=True)
        while not event.wait(.25):
            if any(process.poll() is not None for process in children):raise RuntimeError('An owned service exited; shutting down the remaining owned services')
    except KeyboardInterrupt:event.set()
    finally:
        forced=shutdown(children);server.shutdown();server.server_close()
        for log in logs:log.close()
        journal.unlink(missing_ok=True)
        print(json.dumps({'status':'stopped','owned_services':len(children),'forced_terminations':forced}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['start','stop','status']);parser.add_argument('--state-dir',default=str(ROOT/'.run'))
    args=parser.parse_args()
    try:
        if args.action=='start':start(args.state_dir)
        elif args.action=='stop':print(json.dumps(stop(args.state_dir)))
        else:
            path=Path(args.state_dir)/'services.json';print(json.dumps({'status':'running' if path.exists() and owner_alive(read_state(path)) else 'not_running'}))
    except (OSError,RuntimeError,ValueError) as error:raise SystemExit(str(error))

if __name__=='__main__':main()

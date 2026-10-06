"""Foreground supervisor: authenticated stop, owned children and bounded cleanup."""
import argparse,hmac,json,os,secrets,socket,subprocess,sys,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request,urlopen
import psutil

from src.file_utils import replace_retrying

ROOT=Path(__file__).resolve().parents[1]
INDEXER_READY_TIMEOUT_SECONDS=180
# Every owned service is restarted when it exits, with backoff, forever. Only a
# startup crash loop (CRASH_LOOP_LIMIT crashes in a row, each within
# FAST_EXIT_SECONDS of starting) is treated as impossible to recover: the user
# is warned, restarts continue for SHUTDOWN_GRACE_SECONDS, and only then does
# the supervisor stop.
RECOVERABLE_SERVICES=frozenset({'indexer','api','worker','frontend'})
FAST_EXIT_SECONDS=10
CRASH_LOOP_LIMIT=5
SHUTDOWN_GRACE_SECONDS=60
RESTART_DELAY_CAP_SECONDS=60


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


def wait_log_ready(process,path,marker,timeout=60):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if process.poll() is not None:raise RuntimeError('Service exited before becoming ready; inspect its local log')
        try:
            if marker in path.read_text(encoding='utf-8',errors='replace'):return
        except OSError:pass
        time.sleep(.1)
    raise RuntimeError('Service readiness timed out; dependent services were not started')


def restart_delay(attempt):
    """Seconds before restarting a service: 1, 2, 4, 8, 16, 32, then 60."""
    return min(RESTART_DELAY_CAP_SECONDS, 2 ** max(0, attempt - 1))


class ServiceWatch:
    """Restart bookkeeping for one owned service (no processes in here)."""

    def __init__(self, name, *, now):
        self.name = name
        self.started_at = now
        self.fast_failures = 0      # crashes in a row, each soon after starting
        self.restarts = 0
        self.restart_at = None      # when the next restart is due, if waiting
        self.last_exit_code = None

    def exited(self, *, now, code):
        self.fast_failures = self.fast_failures + 1 if now - self.started_at < FAST_EXIT_SECONDS else 0
        self.restarts += 1
        self.last_exit_code = code
        self.restart_at = now + restart_delay(max(1, self.fast_failures))

    def started(self, *, now):
        self.started_at = now
        self.restart_at = None

    def check_stable(self, *, now):
        """Running for FAST_EXIT_SECONDS proves the service recovered."""
        if self.restart_at is None and now - self.started_at >= FAST_EXIT_SECONDS:
            self.fast_failures = 0

    @property
    def crash_loop(self):
        return self.fast_failures >= CRASH_LOOP_LIMIT

    def state(self):
        if self.crash_loop:
            return 'crash_loop'
        return 'restarting' if self.restart_at is not None else 'running'


def shutdown_plan(watches, *, now, deadline):
    """(shutdown deadline, message): warn first, stop only if nothing recovers."""
    looping = [watch for watch in watches if watch.crash_loop]
    if not looping:
        return None, None
    watch = looping[0]
    message = (f"MailMind's {watch.name} keeps failing right after it starts "
               f'(exit code {watch.last_exit_code}, {watch.fast_failures} times in a row). '
               f'It keeps retrying; if it does not recover, MailMind will shut down. '
               f'See .run/{watch.name}.log.')
    return (deadline if deadline is not None else now + SHUTDOWN_GRACE_SECONDS), message


_LAST_STATUS = {}


def write_status(path, watches, *, now, deadline, message):
    """Service states for the dashboard; never contains the stop token.

    Best effort, never raises: returns False when the file could not be
    updated. On Windows the rename fails with "Access is denied" while the
    dashboard server or antivirus has the file open, so it retries briefly,
    and it only rewrites the file when the content changed."""
    payload = {
        'schema': 1,
        'services': {watch.name: {
            'state': watch.state(),
            'restarts': watch.restarts,
            'last_exit_code': watch.last_exit_code,
            'retry_in_seconds': None if watch.restart_at is None else max(0, round(watch.restart_at - now)),
        } for watch in watches},
        'shutdown_in_seconds': None if deadline is None else max(0, round(deadline - now)),
        'message': message,
    }
    path = Path(path)
    text = json.dumps(payload)
    if _LAST_STATUS.get(str(path)) == text and path.exists():
        return True
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        replace_retrying(temporary, path)
    except OSError:
        return False   # still locked: the next update tries again
    _LAST_STATUS[str(path)] = text
    return True


def supervise(services, spawn, status_path, *, stop, clock=time.monotonic, say=None):
    """Keep every owned service running until stop(seconds) returns True.

    An exited service is restarted after its backoff. A startup crash loop
    gets a warning (terminal and dashboard) and a grace period; only if it
    never recovers does this raise, which shuts MailMind down."""
    say=say or (lambda text:print(text,flush=True))
    now=clock()
    watches={name:ServiceWatch(name,now=now) for name in services}
    deadline=None;warned=None;status_failed=False
    def publish(**state):
        # The dashboard's status file must never be able to stop MailMind.
        nonlocal status_failed
        written=write_status(status_path,watches.values(),now=now,**state)
        if not written and not status_failed:
            say(json.dumps({'event':'status_file_unavailable','detail':'status file locked; will retry next update'}))
        status_failed=not written
    publish(deadline=None,message=None)
    while not stop(.25):
        now=clock()
        for name,watch in watches.items():
            service=services[name]
            if watch.restart_at is not None:
                # Waiting out the backoff, then start it again.
                if now>=watch.restart_at:
                    spawn(name,service['command'],append=True)
                    watch.started(now=now)
                    say(json.dumps({'event':'owned_service_restarted','service':name,'restarts':watch.restarts}))
                continue
            code=service['process'].poll()
            if code is None:
                watch.check_stable(now=now)
                continue
            service['log'].close()
            watch.exited(now=now,code=code)
            say(json.dumps({'event':'owned_service_exited','service':name,'exit_code':code,
                            'fast_failures':watch.fast_failures,
                            'restart_in_seconds':round(watch.restart_at-now)}))
        deadline,message=shutdown_plan(watches.values(),now=now,deadline=deadline)
        if message and message!=warned:
            # Inform first: the dashboard shows this too, with the countdown.
            say('WARNING: '+message+f' Shutting down in {round(deadline-now)} s unless it recovers.')
            warned=message
        elif warned and not message:
            say('MailMind recovered; the scheduled shutdown is cancelled.')
            warned=None
        publish(deadline=deadline,message=message)
        if deadline is not None and now>=deadline:
            raise RuntimeError('Stopping: '+message)


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
    children=[];logs=[];services={};forced=0
    status_path=state_dir/'supervisor-status.json'
    # The frontend server publishes this file so the dashboard can warn the user
    # even while the API itself is down.
    child_env={**os.environ,'MAILMIND_SUPERVISOR_STATUS':str(status_path)}
    def spawn(name,command,*,append=False):
        log_path=state_dir/(name+'.log')
        mode=os.O_CREAT|os.O_WRONLY|(os.O_APPEND if append else os.O_TRUNC)
        descriptor=os.open(log_path,mode,0o600);log=os.fdopen(descriptor,'ab' if append else 'wb');logs.append(log)
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        process=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,creationflags=flags,env=child_env)
        children.append(process)
        services[name]={'process':process,'command':command,'log':log}
        return process
    try:
        indexer=spawn('indexer',[sys.executable,'-m','scripts.service_runner','indexer'])
        print('Starting isolated semantic indexer...',flush=True)
        wait_log_ready(indexer,state_dir/'indexer.log','MailMind semantic indexer ready',
                       timeout=INDEXER_READY_TIMEOUT_SECONDS)
        api=spawn('api',[sys.executable,'-m','scripts.service_runner','api'])
        print('Starting API; the normal-mode local model will continue loading in the background...',flush=True)
        wait_ready(api,'http://127.0.0.1:8000/',api=True)
        worker=spawn('worker',[sys.executable,'-m','scripts.service_runner','worker'])
        ui=spawn('frontend',[node,str(ROOT/'frontend/serve.mjs')])
        wait_ready(ui,'http://127.0.0.1:5173/')
        print('MailMind ready at http://127.0.0.1:5173. The local browser session opens automatically. Keep this supervisor running; use stop or Ctrl+C.',flush=True)
        supervise(services,spawn,status_path,stop=event.wait)
    except KeyboardInterrupt:event.set()
    finally:
        forced=shutdown(children);server.shutdown();server.server_close()
        for log in logs:log.close()
        journal.unlink(missing_ok=True)
        (state_dir/'supervisor-status.json').unlink(missing_ok=True)
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

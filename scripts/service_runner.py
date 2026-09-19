"""Owned services support stdin shutdown; container workers support SIGTERM."""
import argparse,os,signal,sys,threading
from src.config import Settings


def run(service):
    stop=threading.Event()
    def shutdown(*args):stop.set()
    for name in ['SIGINT','SIGTERM','SIGBREAK']:
        if hasattr(signal,name):signal.signal(getattr(signal,name),shutdown)
    if os.environ.get('MAILMIND_STDIN_CONTROL','true')=='true':
        def reader():
            try:sys.stdin.readline()
            finally:stop.set()
        threading.Thread(target=reader,daemon=True).start()
    if service=='api':
        import uvicorn
        server=uvicorn.Server(uvicorn.Config('api.app:app',host='127.0.0.1',port=8000,log_level='info'))
        def monitor():stop.wait();server.should_exit=True
        threading.Thread(target=monitor,daemon=True).start();server.run()
    else:
        from src.main import _run_agent
        from src.logging_utils import log_event
        config=Settings.from_environment(load_file=True)
        if not config.local_only:
            try:
                from src.llm_api import get_client
                get_client()
            except Exception:
                pass
        while not stop.is_set():
            try:_run_agent(settings=config,stop_event=stop)
            except Exception as error:log_event('agent_cycle_failed',error=error)
            stop.wait(config.poll_interval_seconds)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('service',choices=['api','worker']);run(parser.parse_args().service)

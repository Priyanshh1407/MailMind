"""Owned services support stdin shutdown; container workers support SIGTERM."""
import argparse,os,signal,sys,threading
from src.config import Settings


def wait_for_stdin_stop(stop, descriptor=None, poll_seconds=.1):
    '''Wait for supervisor input without blocking the main interpreter thread.'''
    descriptor=sys.stdin.fileno() if descriptor is None else descriptor
    if os.name!='nt':
        try:os.read(descriptor,64)
        finally:stop.set()
        return
    import ctypes,msvcrt
    from ctypes import wintypes
    handle=msvcrt.get_osfhandle(descriptor)
    kernel=ctypes.windll.kernel32
    if kernel.GetFileType(handle)!=3:  # FILE_TYPE_PIPE
        try:os.read(descriptor,64)
        finally:stop.set()
        return
    available=wintypes.DWORD()
    while not stop.wait(poll_seconds):
        if not kernel.PeekNamedPipe(handle,None,0,None,ctypes.byref(available),None):
            stop.set();return
        if available.value:
            os.read(descriptor,min(4096,available.value));stop.set();return


def run(service):
    stop=threading.Event()
    def shutdown(*args):stop.set()
    for name in ['SIGINT','SIGTERM','SIGBREAK']:
        if hasattr(signal,name):signal.signal(getattr(signal,name),shutdown)
    if os.environ.get('MAILMIND_STDIN_CONTROL','true')=='true':
        # Blocking reads on a Windows anonymous pipe can retain the GIL while
        # waiting. That prevented the indexer's main thread from importing and
        # warming ONNX until the supervisor finally wrote STOP. The helper uses
        # nonblocking PeekNamedPipe polling for that case.
        threading.Thread(target=wait_for_stdin_stop,args=(stop,),daemon=True).start()
    if service=='api':
        import uvicorn
        server=uvicorn.Server(uvicorn.Config('api.app:app',host='127.0.0.1',port=8000,log_level='info'))
        def monitor():stop.wait();server.should_exit=True
        threading.Thread(target=monitor,daemon=True).start();server.run()
    elif service=='worker':
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
            # Keep live inbox discovery responsive even when cloud/local
            # classification is slow. Durable tasks drain across later cycles.
            try:_run_agent(settings=config,stop_event=stop,task_limit=2,newest_first=True)
            except Exception as error:log_event('agent_cycle_failed',error=error)
            stop.wait(config.poll_interval_seconds)
    else:
        from src.account_state import AccountManager, WorkCancelled, AccessDenied
        from src.email_search import reconcile_search_index
        from src.logging_utils import log_event
        from src.vector_db import create_search_collection, embed_search_documents
        config=Settings.from_environment(load_file=True)
        manager=AccountManager(config)
        collection=[]
        def collection_provider():
            if not collection:
                collection.append(create_search_collection(config.data_dir/'search_chroma_db',settings=config))
            return collection[0]
        # The service may start before OAuth, but it must not read saved mail,
        # initialize the embedding runtime, or open the search store until the
        # selected Google account is connected. OAuth completion flips the
        # durable connected bit; the next loop then starts indexing.
        print('MailMind semantic indexer waiting for Google connection',flush=True)
        print('MailMind semantic indexer ready',flush=True)
        while not stop.is_set():
            result={'indexed':0,'failed':0}
            context=manager.indexer_context()
            if context is not None:
                try:
                    result=reconcile_search_index(manager,context,collection_provider,limit=10,
                        max_attempts=config.max_processing_attempts,bounded=False,require_connected=True)
                except (WorkCancelled,AccessDenied):
                    pass
                except Exception as error:
                    log_event('email_search_indexer_failed',error=error)
            stop.wait(.1 if result['indexed'] else config.poll_interval_seconds)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('service',choices=['api','worker','indexer']);run(parser.parse_args().service)

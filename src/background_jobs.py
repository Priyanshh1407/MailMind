"""A small bounded daemon runner for interactive sign-in jobs."""
from concurrent.futures import Future
from threading import BoundedSemaphore, Lock, Thread

class BackgroundJobs:
    def __init__(self,max_workers=2,**kwargs):
        self.slots=BoundedSemaphore(max_workers)
        self.lock=Lock()
        self.closed=False
        self.futures=set()

    def submit(self,function):
        with self.lock:
            if self.closed or not self.slots.acquire(blocking=False):
                raise RuntimeError('Sign-in runner is unavailable')
            future=Future()
            self.futures.add(future)
        def run():
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(function())
                    except BaseException as error:
                        future.set_exception(error)
            finally:
                self.slots.release()
                with self.lock:
                    self.futures.discard(future)
        Thread(target=run,daemon=True,name='mailmind-sign-in').start()
        return future

    def shutdown(self,wait=False,cancel_futures=True):
        with self.lock:
            self.closed=True
            if cancel_futures:
                for future in self.futures:
                    future.cancel()

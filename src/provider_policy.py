"""Typed failures and a bounded request budget; never inspect private error text."""
from dataclasses import dataclass
from time import monotonic
import requests
import httpx

@dataclass(frozen=True)
class Failure:
    code: str
    retryable: bool
    ambiguous: bool = False


def provider_failure(error):
    status = getattr(error, 'status_code', None) or getattr(error, 'code', None)
    response = getattr(error, 'resp', None)
    if response is None:
        response = getattr(error, 'response', None)
    if status is None and response is not None:
        status = getattr(response, 'status', None) or getattr(response, 'status_code', None)
    if status in (401,403):
        return Failure('provider_auth', False)
    if status in (400,404,422):
        return Failure('provider_invalid_request', False)
    if status == 429:
        return Failure('provider_quota', True)
    if isinstance(status, int) and status >= 500:
        return Failure('provider_transient', True)
    if isinstance(error, (requests.exceptions.ConnectTimeout,httpx.ConnectTimeout)):
        return Failure('provider_connect_timeout', True)
    if isinstance(error, (requests.exceptions.ReadTimeout, requests.exceptions.Timeout, httpx.TimeoutException, TimeoutError)):
        return Failure('provider_timeout', True, True)
    if isinstance(error, (requests.exceptions.ConnectionError,httpx.NetworkError)):
        return Failure('provider_connection', True, True)
    return Failure('provider_failed', False)


def retry_delay(attempt):
    return min(300, 5 * 2 ** min(max(attempt-1,0),6))


class RequestBudget:
    def __init__(self, seconds, *, clock=monotonic):
        self.clock = clock
        self.deadline = clock() + seconds

    def remaining(self):
        return max(0, self.deadline-self.clock())

    def timeout(self, maximum):
        remaining = self.remaining()
        if remaining < 0.05:
            raise TimeoutError('Request budget exhausted')
        return min(maximum, remaining)


class BoundedCalls:
    """Fixed daemon workers; a stuck adapter cannot create unlimited threads.

    A timeout stops waiting, not the underlying operation. The caller must treat
    external writes as ambiguous and prevent later commits from timed-out work.
    """
    def __init__(self, workers=2):
        from threading import Lock, BoundedSemaphore
        from queue import Queue
        self.workers=workers
        self.lock=Lock()
        self.slots=BoundedSemaphore(workers)
        self.queue=Queue(maxsize=workers)
        self.started=False

    def _worker(self):
        while True:
            function, done, result=self.queue.get()
            try:
                result.append((True,function()))
            except BaseException as error:
                result.append((False,error))
            finally:
                done.set()
                self.slots.release()
                self.queue.task_done()

    def run(self,function,timeout):
        from threading import Thread, Event
        if not self.slots.acquire(blocking=False):
            raise TimeoutError('Provider workers are occupied')
        with self.lock:
            if not self.started:
                for _ in range(self.workers):
                    Thread(target=self._worker,daemon=True,name='mailmind-bounded-call').start()
                self.started=True
        done,result=Event(),[]
        self.queue.put_nowait((function,done,result))
        if not done.wait(timeout):
            raise TimeoutError('Provider time budget exhausted')
        success,value=result[0]
        if not success:
            raise value
        return value


PROVIDER_CALLS=BoundedCalls(2)
RETRIEVAL_CALLS=BoundedCalls(1)
LOCAL_CALLS=BoundedCalls(1)

INDEX_CALLS=BoundedCalls(1)

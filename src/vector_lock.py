"""Cross-process write barrier shared by feedback indexing and account deletion."""
from contextlib import contextmanager
import os
import time


@contextmanager
def vector_write_lock(data_dir, timeout=2):
    path=data_dir/'vector-write.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        deadline=time.monotonic()+timeout
        acquired=False
        while not acquired:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                acquired=True
            except (OSError,BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError('Feedback write is still in flight') from None
                time.sleep(0.01)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

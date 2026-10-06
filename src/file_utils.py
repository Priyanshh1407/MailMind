"""File helpers that cope with Windows file locking."""
import os
import time


def replace_retrying(source, target, attempts=5):
    """os.replace, retried briefly. On Windows it fails with "Access is denied"
    while another process (antivirus, a reader) has the target open for a
    moment. Raises the last error if the file stays locked."""
    for attempt in range(attempts):
        try:
            return os.replace(source, target)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))   # 50, 100, 150, 200 ms

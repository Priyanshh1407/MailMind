"""Bounded loopback readiness check; no pairing or provider calls."""
import argparse
import json
import time
from urllib.request import urlopen


def wait_for_api(url='http://127.0.0.1:8000/',timeout=30,*,opener=urlopen,clock=time.monotonic,sleeper=time.sleep):
    if url not in ('http://127.0.0.1:8000/','http://localhost:8000/') or not 1 <= timeout <= 60:
        raise ValueError('Use the local API and a timeout between 1 and 60 seconds')
    deadline=clock()+timeout
    while clock() < deadline:
        try:
            with opener(url,timeout=min(1,deadline-clock())) as response:
                result=json.loads(response.read(8192))
                if response.status == 200 and result.get('message') == 'MailMind API is online.' and isinstance(result.get('model_loaded'),bool):
                    return True
        except (OSError,ValueError):
            pass
        remaining=deadline-clock()
        if remaining > 0:
            sleeper(min(0.25,remaining))
    return False


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout',type=int,default=30)
    options=parser.parse_args()
    if not wait_for_api(timeout=options.timeout):
        raise SystemExit('API did not become ready. Check its terminal; dependent services were not started.')

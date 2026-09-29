'''Allowlisted JSON event fields that never serialize private content.'''
import json
import logging
import re


logger = logging.getLogger('mailmind')
EVENT_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')


def log_event(event, *, error=None, status_code=None):
    if not isinstance(event, str) or not EVENT_CODE.fullmatch(event):
        raise ValueError('Invalid safe log event code')
    fields = {'event': event}
    if error is not None:
        fields['error_type'] = type(error).__name__
    if status_code is not None:
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise ValueError('Invalid HTTP status code')
        fields['status_code'] = status_code
    logger.log(
        logging.ERROR if error is not None else logging.INFO,
        json.dumps(fields),
    )

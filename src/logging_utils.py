"""Allowlisted JSON events: never serialize exceptions, URLs, or mail content."""
import json
import logging

logger = logging.getLogger("mailmind")


def log_event(event, *, error=None, status_code=None):
    fields = {"event": event}
    if error is not None:
        fields["error_type"] = type(error).__name__
    if status_code is not None:
        fields["status_code"] = int(status_code)
    logger.log(logging.ERROR if error is not None else logging.INFO, json.dumps(fields))

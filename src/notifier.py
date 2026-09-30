"""Plain Telegram text; ambiguous sends are never automatically repeated."""
from dataclasses import dataclass
import os
import requests
from .logging_utils import log_event
from .provider_policy import provider_failure
from .email_text import normalize_text

@dataclass(frozen=True)
class Delivery:
    status: str
    code: str | None = None
    message_id: str | None = None
    retry_after: float | None = None

    def __bool__(self):
        return self.status == 'sent'


def telegram_timeouts(seconds):
    """Return the request's (connect, read) timeout and how long a caller waits.

    The caller must outlast the request's own limits. Otherwise a slow but
    successful send is abandoned first and recorded as an unknown delivery.
    """
    connect, read = min(3.0, float(seconds)), float(seconds)
    return (connect, read), connect + read + 2.0


def send_telegram_alert(sender, subject, summary, *, timeout=5, settings=None):
    from .config import Settings
    if (settings or Settings.from_environment()).local_only:
        return Delivery('blocked','notification_disabled_local_only')
    from .privacy import external_email
    minimized=external_email(sender,subject,summary)
    sender,subject,summary=minimized['sender'],minimized['subject'],minimized['body']
    token, chat_id = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id or ':' not in token or not chat_id.strip():
        log_event('telegram_unconfigured')
        return Delivery('blocked','notification_unconfigured')
    message = ('MailMind Priority Alert\n\nFrom: ' + normalize_text(sender,limit=512)
               + '\nSubject: ' + normalize_text(subject,limit=1000)
               + '\n\nSnippet:\n' + normalize_text(summary,limit=1000))
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                 json={'chat_id':chat_id,'text':message}, timeout=timeout)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            # A successful HTTP send with an unreadable acknowledgement is ambiguous.
            return Delivery('unknown','invalid_delivery_ack')
        if not isinstance(payload,dict):
            return Delivery('unknown','invalid_delivery_ack')
        if response.status_code == 200 and payload.get('ok') is True:
            result = payload.get('result')
            if isinstance(result,dict) and isinstance(result.get('message_id'),int):
                return Delivery('sent',message_id=str(result['message_id']))
            return Delivery('unknown','invalid_delivery_ack')
        status = payload.get('error_code',response.status_code)
        if status == 429:
            delay = payload.get('parameters',{}).get('retry_after')
            return Delivery('retry','notification_quota',retry_after=float(delay) if isinstance(delay,(int,float)) and 0 < delay <= 86400 else None)
        if isinstance(status,int) and status >= 500:
            return Delivery('retry','notification_transient')
        return Delivery('blocked','notification_rejected')
    except Exception as error:
        failure = provider_failure(error)
        log_event('telegram_failed',error=error)
        return Delivery('unknown' if failure.ambiguous else 'retry' if failure.retryable else 'blocked',failure.code)

"""Interactive login is explicit; background access never opens a browser."""
from dataclasses import dataclass, field
from contextlib import nullcontext
import json
import webbrowser
import wsgiref.simple_server
import wsgiref.util
from .mime_parser import parse_gmail_message, MessageParseError
from .account_state import WorkCancelled
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from .config import ROOT, Settings
from .provider_policy import RequestBudget, GMAIL_CALLS, GMAIL_CREDENTIAL_CALLS
import httplib2
from google_auth_httplib2 import AuthorizedHttp


def build_gmail(creds, timeout=5):
    transport=AuthorizedHttp(creds,http=httplib2.Http(timeout=timeout))
    return build('gmail','v1',http=transport,cache_discovery=False)


def execute_gmail(request, budget=None):
    if budget is not None:
        budget.timeout(5)
    # Real SDK retries are disabled. Test adapters keep their simple contract.
    from googleapiclient.http import HttpRequest
    timeout=budget.timeout(5) if budget is not None else 5
    return GMAIL_CALLS.run(lambda:request.execute(num_retries=0) if isinstance(request,HttpRequest) else request.execute(),timeout)
from .logging_utils import log_event

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']


class _ClosingOAuthCallback:
    """Capture the loopback redirect and close the OAuth tab when permitted."""
    def __init__(self, dashboard_url='http://127.0.0.1:5173/'):
        self.last_request_uri = None
        self.dashboard_url = dashboard_url

    def __call__(self, environ, start_response):
        self.last_request_uri = wsgiref.util.request_uri(environ)
        target=json.dumps(self.dashboard_url)
        body = ('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>MailMind connected</title></head><body><p id="message">Google connected. Returning to MailMind...</p><p><a id="return" href="#">Return to MailMind</a></p><script>const target=''' + target + ''';document.getElementById('return').href=target;try{if(window.opener&&!window.opener.closed){window.opener.postMessage({type:'mailmind-oauth-complete'},new URL(target).origin);}}catch(error){}window.close();setTimeout(function(){document.getElementById('message').textContent='Google connected. Redirecting to MailMind...';window.location.replace(target);},700);</script></body></html>''').encode('utf-8')
        start_response('200 OK', [
            ('Content-Type', 'text/html; charset=utf-8'),
            ('Content-Length', str(len(body))),
            ('Cache-Control', 'no-store'),
            ('Referrer-Policy', 'no-referrer'),
            ('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'none'"),
        ])
        return [body]


def _run_oauth_with_closing_tab(flow, timeout_seconds=180):
    callback = _ClosingOAuthCallback()
    wsgiref.simple_server.WSGIServer.allow_reuse_address = False
    server = wsgiref.simple_server.make_server('127.0.0.1', 0, callback)
    try:
        flow.redirect_uri = f'http://127.0.0.1:{server.server_port}/'
        auth_url, _ = flow.authorization_url(prompt='select_account')
        webbrowser.open(auth_url, new=1, autoraise=True)
        server.timeout = timeout_seconds
        server.handle_request()
        if callback.last_request_uri is None:
            raise TimeoutError('Google sign-in timed out')
        flow.fetch_token(authorization_response=callback.last_request_uri.replace('http://', 'https://', 1))
        return flow.credentials
    finally:
        server.server_close()


def _authenticate_gmail():
    if Settings.from_environment().local_only: raise RuntimeError('Google authentication disabled in local-only mode')
    flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / 'credentials.json'), SCOPES)
    creds = _run_oauth_with_closing_tab(flow)
    service = build_gmail(creds)
    account_id = execute_gmail(service.users().getProfile(userId='me')).get('emailAddress')
    # Return a candidate only. The lifecycle manager decides whether login is
    # still allowed before saving anything.
    return account_id, creds.to_json()


def authenticate_gmail():
    try:
        return _authenticate_gmail()
    except Exception as error:
        log_event('gmail_auth_failed', error=error)
        return None


class GmailUnavailable(Exception):
    """Gmail can't be reached right now (no internet, DNS, timeout, Gmail outage).
    Temporary: the account stays connected and the next cycle tries again."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _temporary_gmail_failure(error):
    """The code for a failure that proves nothing about the login, else None."""
    import socket
    from google.auth.exceptions import RefreshError, TransportError
    if isinstance(error, RefreshError):
        return None  # Google rejected the saved login (revoked, expired grant)
    status = getattr(getattr(error, 'resp', None), 'status', None)
    if status in (401, 403):
        return None
    if isinstance(error, (TransportError, httplib2.ServerNotFoundError, socket.gaierror,
                          TimeoutError, ConnectionError, socket.timeout)):
        return 'network_unavailable'
    if isinstance(status, int) and (status >= 500 or status == 429):
        return 'gmail_temporarily_unavailable'
    if isinstance(error, OSError):
        return 'network_unavailable'
    return None


def refresh_gmail(manager, context):
    if manager.settings.local_only: return None
    try:
        with manager.guard(context):
            path=manager.credential_path(context.account_id)
            if not path.exists():
                return None
            creds=Credentials.from_authorized_user_file(str(path),SCOPES)
        with manager.external(context):
            if not creds.valid:
                if not creds.expired or not creds.refresh_token:
                    return None
                class BoundedRefresh(Request):
                    def __call__(self,*args,**kwargs):
                        kwargs['timeout']=manager.settings.provider_timeout_seconds
                        with manager.external(context):
                            return super().__call__(*args,**kwargs)
                GMAIL_CREDENTIAL_CALLS.run(
                    lambda: creds.refresh(BoundedRefresh()),
                    manager.settings.provider_timeout_seconds,
                )
            service=build_gmail(creds,manager.settings.provider_timeout_seconds)
            account_id=execute_gmail(service.users().getProfile(userId='me')).get('emailAddress')
        if account_id != context.account_id:
            log_event('gmail_account_mismatch')
            return None
        with manager.guard(context):
            manager.write_credentials(context,creds.to_json())
        return service
    except WorkCancelled:
        raise
    except Exception as error:
        log_event('gmail_refresh_failed',error=error)
        temporary=_temporary_gmail_failure(error)
        if temporary:
            raise GmailUnavailable(temporary) from None
        return None


def renew_saved_login(path, expected_account, *, timeout):
    """Silent reconnect: renew a saved Google login without opening Google's
    page. Returns (account_id, credential_json), or None when Google refuses
    it (expired, revoked), it belongs to another account, or it can't be read.
    The caller then falls back to the normal Google sign-in page."""
    try:
        creds=Credentials.from_authorized_user_file(str(path),SCOPES)
        if not creds.valid:
            if not creds.refresh_token:
                return None
            class BoundedRefresh(Request):
                def __call__(self,*args,**kwargs):
                    kwargs['timeout']=timeout
                    return super().__call__(*args,**kwargs)
            GMAIL_CREDENTIAL_CALLS.run(lambda: creds.refresh(BoundedRefresh()),timeout)
        service=build_gmail(creds,timeout)
        account_id=execute_gmail(service.users().getProfile(userId='me')).get('emailAddress')
        if account_id != expected_account:
            log_event('gmail_account_mismatch')
            return None
        return account_id,creds.to_json()
    except Exception as error:
        log_event('gmail_silent_reconnect_failed',error=error)
        return None


@dataclass
class FetchBatch:
    emails: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    listing_error: bool = False
    pages: int = 0
    scanned: int = 0
    has_more: bool = False
    next_page_token: str | None = None
    deferred: bool = False
    history_id: str | None = None
    history_expired: bool = False

    @property
    def status(self):
        if self.listing_error or self.failures:
            return 'partial' if self.emails else 'error'
        return 'success' if self.emails else ('deferred' if self.deferred else 'empty')

    def summary(self):
        return {'status': self.status, 'fetched_count':len(self.emails),
                'failed_count':len(self.failures), 'pages_count':self.pages,
                'scanned_count':self.scanned, 'has_more':self.has_more,
                'warning_count':sum(bool(e.get('parse_warnings')) for e in self.emails),
                'truncated_count':sum(bool(e.get('body_truncated')) for e in self.emails)}


def get_gmail_history_id(service, *, before_request=None, budget_seconds=10):
    """Return the current Gmail history cursor without exposing account data."""
    if not hasattr(service,'users'):
        return None
    users=service.users()
    if not hasattr(users,'getProfile'):
        return None
    budget=RequestBudget(budget_seconds)
    guard=before_request or nullcontext
    try:
        with guard():
            result=execute_gmail(users.getProfile(userId='me'),budget)
        value=result.get('historyId') if isinstance(result,dict) else None
        return str(value) if value is not None else None
    except WorkCancelled:
        raise
    except Exception as error:
        log_event('gmail_history_cursor_failed',error=error)
        return None


def _fetch_message(service, msg_id, budget, guard):
    with guard():
        message=execute_gmail(service.users().messages().get(userId='me',id=msg_id,format='full'),budget)
    def inline_loader(attachment_id):
        try:
            with guard():
                response=execute_gmail(service.users().messages().attachments().get(
                    userId='me',messageId=msg_id,id=attachment_id),budget)
            if not isinstance(response,dict):
                raise MessageParseError('malformed_inline_body')
            return response.get('data')
        except WorkCancelled:
            raise
        except Exception as error:
            log_event('gmail_inline_body_fetch_failed',error=error)
            raise MessageParseError('inline_body_fetch_failed') from None
    return parse_gmail_message(message,msg_id,inline_loader=inline_loader)


def get_new_emails(service, start_history_id, max_results=20, *, max_pages=3,
                   before_request=None, exclude_ids=None, budget_seconds=30):
    """Fetch messages added to INBOX after a durable Gmail history cursor."""
    if not isinstance(start_history_id,str) or not start_history_id or not 1 <= max_results <= 200 or not 1 <= max_pages <= 20:
        raise ValueError('Invalid Gmail history fetch')
    users=service.users()
    if not hasattr(users,'history'):
        return FetchBatch(deferred=True,history_id=start_history_id)
    batch=FetchBatch(history_id=start_history_id)
    excluded=set(exclude_ids or ())
    seen=set()
    identities=[]
    page_token=None
    budget=RequestBudget(budget_seconds)
    guard=before_request or nullcontext
    overflow=False
    while batch.pages < max_pages and not overflow:
        if len(seen) >= max_results:
            # The batch is full and Gmail has more pages. Asking for
            # maxResults=0 is rejected (HTTP 400); stop here instead, and the
            # cursor stays put so the next cycle continues from it.
            overflow=True
            break
        parameters={'userId':'me','startHistoryId':start_history_id,
                    'historyTypes':['messageAdded'],'labelId':'INBOX',
                    'maxResults':min(100,max_results-len(seen))}
        if page_token:
            parameters['pageToken']=page_token
        try:
            with guard():
                result=execute_gmail(users.history().list(**parameters),budget)
            if not isinstance(result,dict) or not isinstance(result.get('history',[]),list):
                raise ValueError('Malformed Gmail history')
        except WorkCancelled:
            raise
        except Exception as error:
            if getattr(getattr(error,'resp',None),'status',None) in (404,410):
                batch.history_expired=True
                batch.listing_error=True
                return batch
            log_event('gmail_history_failed',error=error)
            batch.listing_error=True
            # Earlier pages may have moved the cursor, but their messages were
            # not fetched. Advancing would skip them for good.
            batch.history_id=start_history_id
            return batch
        batch.pages+=1
        cursor=result.get('historyId')
        if cursor is not None:
            batch.history_id=str(cursor)
        for event in result.get('history',[]):
            if not isinstance(event,dict):
                continue
            for added in event.get('messagesAdded',[]) if isinstance(event.get('messagesAdded',[]),list) else []:
                message=added.get('message') if isinstance(added,dict) else None
                identity=message.get('id') if isinstance(message,dict) else None
                if isinstance(identity,str) and identity and identity not in excluded:
                    if identity not in seen:
                        seen.add(identity)
                        identities.append(identity)
                        overflow=len(identities) > max_results
        page_token=result.get('nextPageToken')
        if page_token is not None and not isinstance(page_token,str):
            batch.listing_error=True
            page_token=None
        if not page_token:
            break
    batch.has_more=bool(page_token) or overflow
    batch.deferred=overflow
    batch.next_page_token=page_token
    if batch.has_more or batch.listing_error:
        # Do not advance beyond events that were not admitted. Re-reading the
        # history range is safe because durable Gmail IDs are excluded.
        batch.history_id=start_history_id
    for msg_id in identities[:max_results]:
        if budget.remaining() < 0.05:
            batch.deferred=True
            batch.has_more=True
            break
        batch.scanned+=1
        try:
            batch.emails.append(_fetch_message(service,msg_id,budget,guard))
        except WorkCancelled:
            raise
        except MessageParseError as error:
            batch.failures.append({'code':error.code,'email_id':msg_id})
        except Exception as error:
            log_event('gmail_message_fetch_failed',error=error)
            batch.failures.append({'code':fetch_failure_code(error),'email_id':msg_id})
    return batch


def fetch_failure_code(error):
    """'message_fetch_failed' for a temporary failure (retried until Gmail
    answers); 'message_unavailable' for a permanent one (deleted, rejected)."""
    from .provider_policy import provider_failure
    if _temporary_gmail_failure(error) or provider_failure(error).retryable:
        return 'message_fetch_failed'
    return 'message_unavailable'


def get_messages_by_id(service, message_ids, *, before_request=None, budget_seconds=30):
    """Fetch specific messages again, e.g. ones whose earlier download failed."""
    batch=FetchBatch()
    budget=RequestBudget(budget_seconds)
    guard=before_request or nullcontext
    for msg_id in message_ids:
        if budget.remaining() < 0.05:
            batch.deferred=True
            break
        batch.scanned+=1
        try:
            batch.emails.append(_fetch_message(service,msg_id,budget,guard))
        except WorkCancelled:
            raise
        except MessageParseError as error:
            batch.failures.append({'code':error.code,'email_id':msg_id})
        except Exception as error:
            log_event('gmail_message_fetch_failed',error=error)
            batch.failures.append({'code':fetch_failure_code(error),'email_id':msg_id})
    return batch


def list_recent_inbox_ids(service, *, days=2, max_results=50, before_request=None, budget_seconds=10):
    """IDs of INBOX messages received in the last `days` days (read or unread).

    Used as a safety net next to history sync. Returns None if listing fails.
    """
    guard=before_request or nullcontext
    budget=RequestBudget(budget_seconds)
    try:
        with guard():
            result=execute_gmail(service.users().messages().list(
                userId='me',labelIds=['INBOX'],q=f'newer_than:{int(days)}d',
                maxResults=max_results),budget)
    except WorkCancelled:
        raise
    except Exception as error:
        log_event('gmail_recent_list_failed',error=error)
        return None
    messages=result.get('messages',[]) if isinstance(result,dict) else []
    if not isinstance(messages,list):
        return None
    return [item['id'] for item in messages[:max_results]
            if isinstance(item,dict) and isinstance(item.get('id'),str) and item['id']]


def get_unread_emails(service, max_results=20, *, page_size=10, max_pages=3, page_token=None, before_request=None, exclude_ids=None, budget_seconds=30):
    """Bound listing/detail work; keep good messages if one request fails.

    The optional guard runs around EACH provider request, not a whole inbox.
    The cursor is an opaque provider hint, not a durable processing queue.
    """
    if not 1 <= max_results <= 200 or not 1 <= page_size <= 100 or not 1 <= max_pages <= 20:
        raise ValueError('Invalid Gmail fetch limits')
    batch = FetchBatch(next_page_token=page_token)
    seen, seen_tokens = set(), set()
    excluded=set(exclude_ids or ())
    budget=RequestBudget(budget_seconds)
    guard = before_request or nullcontext
    while batch.pages < max_pages and batch.scanned < max_results:
        if page_token in seen_tokens:
            batch.listing_error = True
            batch.next_page_token = None
            batch.has_more = True
            log_event('gmail_repeated_page_token')
            break
        seen_tokens.add(page_token)
        parameters = dict(userId='me',labelIds=['INBOX','UNREAD'],maxResults=min(page_size,max_results-batch.scanned))
        if page_token:
            parameters['pageToken'] = page_token
        try:
            with guard():
                result = execute_gmail(service.users().messages().list(**parameters),budget)
            if not isinstance(result,dict) or not isinstance(result.get('messages',[]),list):
                raise ValueError('Malformed Gmail listing')
        except WorkCancelled:
            raise
        except Exception as error:
            log_event('gmail_list_failed',error=error)
            batch.listing_error = True
            # Invalid cursors are discarded. Transient failures retry the page.
            if getattr(getattr(error,'resp',None),'status',None) == 400 and page_token:
                batch.next_page_token = None
                batch.has_more = True
            else:
                batch.next_page_token = page_token
                batch.has_more = bool(page_token)
            break
        batch.pages += 1
        messages = result.get('messages',[])
        request_limit = parameters['maxResults']
        if len(messages) > request_limit:
            # A provider response must not bypass the work limit.
            batch.listing_error = True
            messages = messages[:request_limit]
        for item in messages:
            if budget.remaining() < 0.05:
                batch.deferred=True
                batch.next_page_token=parameters.get('pageToken')
                batch.has_more=True
                return batch
            batch.scanned += 1
            msg_id = item.get('id') if isinstance(item,dict) else None
            if not isinstance(msg_id,str) or not msg_id:
                batch.failures.append({'code':'missing_message_id'})
                continue
            if msg_id in seen:
                continue
            seen.add(msg_id)
            if msg_id in excluded:
                continue
            try:
                batch.emails.append(_fetch_message(service,msg_id,budget,guard))
            except WorkCancelled:
                raise
            except MessageParseError as error:
                log_event('gmail_message_parse_failed')
                batch.failures.append({'code':error.code,'email_id':msg_id})
            except Exception as error:
                log_event('gmail_message_fetch_failed',error=error)
                batch.failures.append({'code':'message_fetch_failed','email_id':msg_id})
        token = result.get('nextPageToken')
        if token is not None and not isinstance(token,str):
            batch.listing_error = True
            token = None
        page_token = token or None
        batch.next_page_token = page_token
        batch.has_more = bool(page_token)
        if not page_token:
            if not messages and parameters.get('pageToken') and not batch.scanned:
                # An exhausted/stale continuation is not proof of an empty
                # inbox. Recheck page one if the page budget still allows it.
                if batch.pages < max_pages:
                    continue
                batch.deferred = True
            break
    return batch


def mark_as_read(service, message_id):
    """
    Removes the 'UNREAD' label from a specific email ID.
    """
    try:
        # The Gmail API requires a dictionary specifying which labels to add or remove
        execute_gmail(service.users().messages().modify(
            userId='me',
            id=message_id,
            body={
                'removeLabelIds': ['UNREAD']
            }
        ))
        return True
    except Exception as error:
        log_event("gmail_mark_read_failed", error=error)
        return False

if __name__ == '__main__':
    print('Connect Google through the local MailMind dashboard.')

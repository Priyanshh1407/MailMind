"""Interactive login is explicit; background access never opens a browser."""
from dataclasses import dataclass, field
from contextlib import nullcontext
from .mime_parser import parse_gmail_message, MessageParseError
from .account_state import WorkCancelled
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from .config import ROOT, Settings
from .provider_policy import RequestBudget, PROVIDER_CALLS
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
    return PROVIDER_CALLS.run(lambda:request.execute(num_retries=0) if isinstance(request,HttpRequest) else request.execute(),timeout)
from .logging_utils import log_event

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']


def _authenticate_gmail():
    if Settings.from_environment().local_only: raise RuntimeError('Google authentication disabled in local-only mode')
    flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / 'credentials.json'), SCOPES)
    creds = flow.run_local_server(host='127.0.0.1', port=0, prompt='select_account',
                                 timeout_seconds=180, success_message='Connected. You can close this tab.')
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
                creds.refresh(BoundedRefresh())
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
                with guard():
                    message = execute_gmail(service.users().messages().get(userId='me',id=msg_id,format='full'),budget)
                def inline_loader(attachment_id):
                    try:
                        with guard():
                            response = execute_gmail(service.users().messages().attachments().get(
                                userId='me',messageId=msg_id,id=attachment_id),budget)
                        if not isinstance(response,dict):
                            raise MessageParseError('malformed_inline_body')
                        return response.get('data')
                    except WorkCancelled:
                        raise
                    except Exception as error:
                        log_event('gmail_inline_body_fetch_failed',error=error)
                        raise MessageParseError('inline_body_fetch_failed') from None
                batch.emails.append(parse_gmail_message(message,msg_id,inline_loader=inline_loader))
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

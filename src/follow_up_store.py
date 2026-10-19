"""Gmail reads and local storage for follow-up drafts.

Sent mail is listed with Gmail metadata only (To, Subject, Date, thread and
snippet) into `sent_messages`, so the list is instant on the next visit. A
thread is read in full only when the user picks one. Every Gmail request is
bounded and fenced: an account switch or stop ends the work.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
import json
import time

from .database import utc_timestamp
from .email_client import execute_gmail
from .email_text import normalize_text
from .follow_ups import SENT_PAGE, learn_style, own_text
from .mime_parser import parse_gmail_message
from .provider_policy import RequestBudget

STYLE_MAX_AGE = timedelta(days=7)
STYLE_SAMPLES = 12
REPLY_CHECKS = 10


def _header(message, name):
    for item in (message.get('payload') or {}).get('headers') or []:
        if isinstance(item, dict) and str(item.get('name', '')).casefold() == name.casefold():
            return str(item.get('value') or '')
    return ''


def list_sent(service, *, page_token=None, guard, budget_seconds=20):
    """One page of sent messages (newest first) with metadata only."""
    budget = RequestBudget(budget_seconds)
    with guard():
        listing = execute_gmail(service.users().messages().list(
            userId='me', labelIds=['SENT'], q='newer_than:90d', maxResults=SENT_PAGE,
            **({'pageToken': page_token} if page_token else {})), budget)
    rows = []
    for item in (listing.get('messages') or [])[:SENT_PAGE]:
        if budget.remaining() < 0.5:
            break
        with guard():
            message = execute_gmail(service.users().messages().get(
                userId='me', id=item['id'], format='metadata', metadataHeaders=['To', 'Cc', 'Subject', 'Date']), budget)
        rows.append({'message_id': message['id'], 'thread_id': message.get('threadId') or message['id'],
                     'to_header': normalize_text(_header(message, 'To'), limit=1000),
                     'subject': normalize_text(_header(message, 'Subject'), limit=300),
                     'snippet': normalize_text(message.get('snippet') or '', limit=300),
                     'sent_at': int(message.get('internalDate') or 0)})
    return rows, listing.get('nextPageToken')


def read_thread(service, thread_id, *, guard, budget_seconds=20):
    """All messages of a thread, oldest first, as from/to/date/body."""
    budget = RequestBudget(budget_seconds)
    with guard():
        thread = execute_gmail(service.users().threads().get(userId='me', id=thread_id, format='full'), budget)
    messages = []
    for message in thread.get('messages') or []:
        try:
            parsed = parse_gmail_message(message, message.get('id') or 'thread-message')
        except Exception:
            continue
        messages.append({'id': message.get('id'), 'from': _header(message, 'From'), 'to': _header(message, 'To'),
                         'date_ms': int(message.get('internalDate') or 0), 'body': parsed['body']})
    return sorted(messages, key=lambda item: item['date_ms'])


def reply_states(service, thread_ids, account_id, *, guard, budget_seconds=15):
    """For each thread: is the latest message the user's own (still waiting)?"""
    budget = RequestBudget(budget_seconds)
    states = {}
    for thread_id in thread_ids[:REPLY_CHECKS]:
        if budget.remaining() < 0.5:
            break
        with guard():
            thread = execute_gmail(service.users().threads().get(
                userId='me', id=thread_id, format='metadata', metadataHeaders=['From']), budget)
        messages = sorted(thread.get('messages') or [], key=lambda item: int(item.get('internalDate') or 0))
        if messages:
            last = messages[-1]
            mine = parseaddr(_header(last, 'From'))[1].casefold() == account_id.casefold()
            states[thread_id] = ('waiting' if mine else 'replied', int(last.get('internalDate') or 0))
    return states


def save_sent(conn, account_id, rows, states):
    stamp = utc_timestamp()
    for row in rows:
        state, last_at = states.get(row['thread_id'], (None, None))
        conn.execute(
            """INSERT INTO sent_messages(account_id,message_id,thread_id,to_header,subject,snippet,sent_at,
                   reply_state,last_activity_at,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id,message_id) DO UPDATE SET to_header=excluded.to_header,
                   subject=excluded.subject,snippet=excluded.snippet,
                   reply_state=COALESCE(excluded.reply_state,sent_messages.reply_state),
                   last_activity_at=COALESCE(excluded.last_activity_at,sent_messages.last_activity_at),
                   fetched_at=excluded.fetched_at""",
            (account_id, row['message_id'], row['thread_id'], row['to_header'], row['subject'], row['snippet'],
             row['sent_at'], state, last_at, stamp))


def sent_page(conn, account_id, *, search='', limit=25, offset=0, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    clauses, values = ['account_id=?'], [account_id]
    if search:
        like = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        clauses.append("(subject LIKE ? ESCAPE '\\' OR to_header LIKE ? ESCAPE '\\' OR snippet LIKE ? ESCAPE '\\')")
        values += [like, like, like]
    where = ' AND '.join(clauses)
    total = conn.execute(f'SELECT COUNT(*) FROM sent_messages WHERE {where}', values).fetchone()[0]
    rows = conn.execute(f'SELECT * FROM sent_messages WHERE {where} ORDER BY sent_at DESC LIMIT ? OFFSET ?',
                        (*values, limit, offset)).fetchall()
    items = []
    for row in rows:
        waiting_days = None
        if row['reply_state'] == 'waiting' and row['last_activity_at']:
            waiting_days = max(0, int((now_ms - row['last_activity_at']) // 86400000))
        items.append({'message_id': row['message_id'], 'thread_id': row['thread_id'], 'to': row['to_header'],
                      'subject': row['subject'], 'snippet': row['snippet'], 'sent_at': row['sent_at'],
                      'reply_state': row['reply_state'], 'waiting_days': waiting_days})
    return items, total


def cached_style(conn, account_id):
    row = conn.execute('SELECT profile_json,computed_at FROM follow_up_style WHERE account_id=?', (account_id,)).fetchone()
    if row is None:
        return None
    computed = datetime.fromisoformat(row['computed_at'].replace('Z', '+00:00'))
    return json.loads(row['profile_json']) if datetime.now(timezone.utc) - computed < STYLE_MAX_AGE else None


def compute_style(service, conn_factory, account_id, my_name, *, guard, budget_seconds=20):
    """Learn the user's style from their recent sent mail, locally, and cache it."""
    budget = RequestBudget(budget_seconds)
    with guard():
        listing = execute_gmail(service.users().messages().list(
            userId='me', labelIds=['SENT'], maxResults=STYLE_SAMPLES), budget)
    bodies = []
    for item in listing.get('messages') or []:
        if budget.remaining() < 0.5:
            break
        with guard():
            message = execute_gmail(service.users().messages().get(userId='me', id=item['id'], format='full'), budget)
        try:
            bodies.append(own_text(parse_gmail_message(message, item['id'])['body']))
        except Exception:
            continue
    profile = learn_style(bodies, my_name)
    with conn_factory() as conn:
        conn.execute("""INSERT INTO follow_up_style(account_id,profile_json,computed_at) VALUES (?,?,?)
                        ON CONFLICT(account_id) DO UPDATE SET profile_json=excluded.profile_json,
                        computed_at=excluded.computed_at""", (account_id, json.dumps(profile), utc_timestamp()))
    return profile


def public_follow_up(row):
    return {'follow_up_id': row['follow_up_id'], 'message_id': row['message_id'], 'thread_id': row['thread_id'],
            'length': row['length'], 'status': row['status'], 'text': row['draft_text'],
            'issues': json.loads(row['issues_json']), 'revised': bool(row['revised']),
            'summary': json.loads(row['summary_json']), 'references': json.loads(row['references_json']),
            'recipient': row['recipient'], 'total_tokens': row['total_tokens'], 'latency_ms': row['latency_ms'],
            'error_code': row['error_code'], 'created_at': row['created_at'], 'updated_at': row['updated_at']}

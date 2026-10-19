"""Security evidence for each email: who really sent it, and where its links go.

Gmail already checked SPF, DKIM and DMARC when the message arrived and wrote the
verdicts into the Authentication-Results header. MailMind records them with the
reply paths, unsubscribe headers, links and hidden text, so later checks (scam
forensics, the red-team) work from saved evidence without asking Gmail again.
Nothing here touches the network or a provider.
"""
from email.utils import getaddresses, parseaddr
import json
import re
import time
from urllib.parse import urlsplit

from .database import utc_timestamp
from .email_text import MAX_HIDDEN_TEXT_CHARS, MAX_LINK_HREF_CHARS, MAX_LINK_TEXT_CHARS, MAX_LINKS, normalize_text
from .provider_policy import retry_delay

SIGNALS_VERSION = 1
AUTH_RESULTS = ('pass', 'fail', 'softfail', 'neutral', 'none', 'temperror', 'permerror', 'policy', 'unknown')
BACKFILL_STATUSES = ('queued', 'running', 'retry', 'complete', 'dead')
MAX_SIGNALS_BACKFILL = 1000
SIGNALS_BATCH = 10
MAX_THREAD_ID_CHARS = 64
MAX_ADDRESS_CHARS = 320
MAX_DOMAIN_CHARS = 255
MAX_UNSUBSCRIBE_CHARS = 2048
_RESULT = re.compile(r'\b(spf|dkim|dmarc)\s*=\s*([a-z]+)', re.I)


def domain_of(address):
    """Lower-case domain of one address ('' if there is none)."""
    address = (address or '').strip().strip('<>').casefold()
    if '@' not in address:
        return ''
    return address.rsplit('@', 1)[1].strip().rstrip('.')[:MAX_DOMAIN_CHARS]


def link_domain(href):
    try:
        parts = urlsplit(href)
    except ValueError:
        return ''
    if parts.scheme.lower() not in ('http', 'https'):
        return ''
    try:
        host = parts.hostname or ''
    except ValueError:
        return ''
    return host.casefold().rstrip('.')[:MAX_DOMAIN_CHARS]


def parse_authentication_results(values):
    """SPF/DKIM/DMARC verdicts Gmail recorded, or 'unknown' when absent.

    Several hops may each add a header; Gmail's own (authserv-id google.com)
    is trusted first, because a sender can forge headers added before it.
    """
    values = [value for value in values or () if isinstance(value, str)]
    result = dict.fromkeys(('spf', 'dkim', 'dmarc'), 'unknown')
    if not values:
        return result
    chosen = next((value for value in values
                   if 'google.com' in value.split(';', 1)[0].casefold()), values[0])
    found = {}
    for method, verdict in _RESULT.findall(chosen):
        found.setdefault(method.lower(), []).append(verdict.lower())
    for method, verdicts in found.items():
        verdicts = [value if value in AUTH_RESULTS else 'unknown' for value in verdicts]
        # A message can carry several DKIM signatures; one valid one is enough.
        result[method] = 'pass' if 'pass' in verdicts else verdicts[0]
    return result


def extract_signals(headers, thread_id, links=(), hidden_text=''):
    """Bounded signals from parsed headers; never raises for bad input."""
    from .mime_parser import decode_mail_header
    def header(name):
        try:
            value = headers.get(name)
        except Exception:
            return ''
        return normalize_text(decode_mail_header(str(value)) if value else '', limit=MAX_UNSUBSCRIBE_CHARS)
    try:
        auth = parse_authentication_results(headers.get_all('Authentication-Results') or [])
    except Exception:
        auth = dict.fromkeys(('spf', 'dkim', 'dmarc'), 'unknown')
    from_address = parseaddr(header('From'))[1].casefold()[:MAX_ADDRESS_CHARS]
    reply_to = [address for _name, address in getaddresses([header('Reply-To')]) if address]
    return_path = parseaddr(header('Return-Path'))[1]
    safe_links = []
    for link in list(links or ())[:MAX_LINKS]:
        if not isinstance(link, dict) or not isinstance(link.get('href'), str):
            continue
        href = link['href'][:MAX_LINK_HREF_CHARS]
        safe_links.append({'text': str(link.get('text') or '')[:MAX_LINK_TEXT_CHARS],
                           'href': href, 'href_domain': link_domain(href)})
    thread = thread_id if isinstance(thread_id, str) and 0 < len(thread_id) <= MAX_THREAD_ID_CHARS else None
    hidden = normalize_text(hidden_text, limit=MAX_HIDDEN_TEXT_CHARS)
    return {
        'signals_version': SIGNALS_VERSION,
        'thread_id': thread,
        'from_address': from_address,
        'from_domain': domain_of(from_address),
        'reply_to_domain': domain_of(reply_to[0]) if reply_to else '',
        'return_path_domain': domain_of(return_path),
        'auth_spf': auth['spf'], 'auth_dkim': auth['dkim'], 'auth_dmarc': auth['dmarc'],
        'list_unsubscribe': header('List-Unsubscribe'),
        'list_unsubscribe_post': 'list-unsubscribe=one-click' in header('List-Unsubscribe-Post').casefold(),
        'links': safe_links,
        'hidden_text': hidden,
    }


def save_email_signals(conn, account_id, email_id, signals):
    if not isinstance(signals, dict):
        return False
    conn.execute(
        """INSERT INTO email_signals(
               account_id,email_id,signals_version,thread_id,from_address,from_domain,
               reply_to_domain,return_path_domain,auth_spf,auth_dkim,auth_dmarc,
               list_unsubscribe,list_unsubscribe_post,links_json,hidden_text,captured_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(account_id,email_id) DO UPDATE SET
               signals_version=excluded.signals_version,thread_id=excluded.thread_id,
               from_address=excluded.from_address,from_domain=excluded.from_domain,
               reply_to_domain=excluded.reply_to_domain,return_path_domain=excluded.return_path_domain,
               auth_spf=excluded.auth_spf,auth_dkim=excluded.auth_dkim,auth_dmarc=excluded.auth_dmarc,
               list_unsubscribe=excluded.list_unsubscribe,
               list_unsubscribe_post=excluded.list_unsubscribe_post,
               links_json=excluded.links_json,hidden_text=excluded.hidden_text,
               captured_at=excluded.captured_at""",
        (account_id, email_id, signals['signals_version'], signals['thread_id'],
         signals['from_address'], signals['from_domain'], signals['reply_to_domain'],
         signals['return_path_domain'], signals['auth_spf'], signals['auth_dkim'],
         signals['auth_dmarc'], signals['list_unsubscribe'],
         int(bool(signals['list_unsubscribe_post'])),
         json.dumps(signals['links'], ensure_ascii=False), signals['hidden_text'],
         utc_timestamp()))
    return True


def get_email_signals(conn, account_id, email_id):
    row = conn.execute('SELECT * FROM email_signals WHERE account_id=? AND email_id=?',
                       (account_id, email_id)).fetchone()
    if row is None:
        return None
    value = dict(row)
    value['links'] = json.loads(value.pop('links_json'))
    value['list_unsubscribe_post'] = bool(value['list_unsubscribe_post'])
    return value


_ELIGIBLE = """FROM email_logs e
    LEFT JOIN email_signals s ON s.account_id=e.account_id AND s.email_id=e.email_id
    LEFT JOIN signals_backfill_items b ON b.account_id=e.account_id AND b.email_id=e.email_id
    WHERE e.account_id=? AND e.message_type='gmail' AND s.email_id IS NULL AND b.email_id IS NULL"""


def signals_summary(conn, account_id):
    counts = {row['status']: row['count'] for row in conn.execute(
        'SELECT status,COUNT(*) AS count FROM signals_backfill_items WHERE account_id=? GROUP BY status',
        (account_id,))}
    return {
        'captured': conn.execute('SELECT COUNT(*) FROM email_signals WHERE account_id=?',
                                 (account_id,)).fetchone()[0],
        'eligible': conn.execute('SELECT COUNT(*) ' + _ELIGIBLE, (account_id,)).fetchone()[0],
        **{status: counts.get(status, 0) for status in BACKFILL_STATUSES},
    }


def queue_signals_backfill(conn, account_id, limit):
    """Admit saved Gmail messages that have no signals yet, newest first."""
    if type(limit) is not int or not 1 <= limit <= MAX_SIGNALS_BACKFILL:
        raise ValueError('Invalid signals backfill limit')
    rows = conn.execute('SELECT e.email_id ' + _ELIGIBLE + ' ORDER BY e.created_at DESC,e.email_id LIMIT ?',
                        (account_id, limit)).fetchall()
    stamp = utc_timestamp()
    conn.executemany(
        """INSERT INTO signals_backfill_items(account_id,email_id,status,requested_at,updated_at)
           VALUES (?,?,'queued',?,?)""", [(account_id, row['email_id'], stamp, stamp) for row in rows])
    return len(rows)


def process_signals_backfill(manager, context, token, service, fetch, *, fence, guard,
                             limit=SIGNALS_BATCH, budget_seconds=10):
    """Re-read due messages from Gmail and save only their signals.

    No task, alert, label or read state changes: the email itself is already
    saved and processed. Temporary download failures retry with backoff; a
    deleted or unparseable message stops after the attempt limit.
    """
    now = time.time()
    with manager.guard(context) as conn:
        fence(manager, context, token, conn)
        ids = [row['email_id'] for row in conn.execute(
            """SELECT email_id FROM signals_backfill_items
               WHERE account_id=? AND status IN ('queued','retry') AND next_retry_at<=?
               ORDER BY status='retry',requested_at DESC,email_id LIMIT ?""",
            (context.account_id, now, limit))]
        if not ids:
            return {'processed': 0, 'complete': 0, 'retry': 0, 'dead': 0}
        conn.executemany(
            "UPDATE signals_backfill_items SET status='running',updated_at=? WHERE account_id=? AND email_id=?",
            [(utc_timestamp(), context.account_id, key) for key in ids])
    batch = fetch(service, ids, before_request=guard, budget_seconds=budget_seconds)
    result = {'processed': len(ids), 'complete': 0, 'retry': 0, 'dead': 0}
    with manager.guard(context) as conn:
        fence(manager, context, token, conn)
        stamp = utc_timestamp()
        done = set()
        for email in batch.emails:
            if email.get('id') not in ids:
                continue
            save_email_signals(conn, context.account_id, email['id'], email.get('signals'))
            conn.execute(
                """UPDATE signals_backfill_items SET status='complete',error_code=NULL,updated_at=?
                   WHERE account_id=? AND email_id=?""", (stamp, context.account_id, email['id']))
            done.add(email['id'])
            result['complete'] += 1
        for failure in batch.failures:
            key = failure.get('email_id')
            if key not in ids or key in done:
                continue
            row = conn.execute('SELECT attempt_count FROM signals_backfill_items WHERE account_id=? AND email_id=?',
                               (context.account_id, key)).fetchone()
            count = row['attempt_count'] + 1
            temporary = failure.get('code') == 'message_fetch_failed'
            dead = not temporary and count >= manager.settings.max_processing_attempts
            conn.execute(
                """UPDATE signals_backfill_items SET status=?,attempt_count=?,next_retry_at=?,
                       error_code=?,updated_at=? WHERE account_id=? AND email_id=?""",
                ('dead' if dead else 'retry', count, now + retry_delay(count),
                 str(failure.get('code') or 'message_fetch_failed')[:80], stamp, context.account_id, key))
            done.add(key)
            result['dead' if dead else 'retry'] += 1
        # Ran out of time before reaching these: they go back unchanged.
        conn.executemany(
            "UPDATE signals_backfill_items SET status='queued',updated_at=? WHERE account_id=? AND email_id=? AND status='running'",
            [(stamp, context.account_id, key) for key in ids if key not in done])
    return result

"""Ledger rows: scan saved mail in bounded steps, and answer the Money tab.

The worker scans newest-first. The local detector skips mail that is not a
transaction without any provider call. For the rest, at most one cloud
extraction runs per cycle (and at most one every LEDGER_CLOUD_INTERVAL
seconds) through the assistant call slots; a temporary failure retries with
backoff, and after the attempt limit, or on a permanent failure, the local
rules decide. Rows you corrected are never overwritten.
"""
from datetime import date, datetime, timedelta, timezone
import time
from zoneinfo import ZoneInfo

from .database import utc_timestamp
from .intelligence_contract import DEFAULT_TIMEZONE, TokenOperation
from .ledger import (EXTRACTION_VERSION, OUTPUT_SCHEMA, SYSTEM_INSTRUCTION, detect_subscriptions,
                     extract_by_rules, is_transaction, provider_contents, resolve_cloud_answer,
                     subscription_insights)
from .provider_policy import retry_delay

SCAN_LIMIT = 20
LEDGER_CLOUD_INTERVAL = 10
_LAST_CLOUD_CALL = {}
SPEND_KINDS = ('purchase', 'subscription', 'debit')


def _received(created_at):
    moment = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    return moment.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).date()


def save_entry(conn, account_id, email_id, entry, occurred_at):
    """Insert or refresh the row for this email; a corrected row is kept."""
    stamp = utc_timestamp()
    conn.execute(
        """INSERT INTO ledger_entries(account_id,email_id,kind,amount_minor,currency,merchant,occurred_at,
               renews_on,period,evidence,source,extraction_version,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(account_id,email_id) DO UPDATE SET
               kind=excluded.kind,amount_minor=excluded.amount_minor,currency=excluded.currency,
               merchant=excluded.merchant,renews_on=excluded.renews_on,period=excluded.period,
               evidence=excluded.evidence,source=excluded.source,
               extraction_version=excluded.extraction_version,updated_at=excluded.updated_at,
               revision=ledger_entries.revision+1
           WHERE ledger_entries.user_corrected=0""",
        (account_id, email_id, entry['kind'], entry['amount_minor'], entry['currency'], entry['merchant'],
         occurred_at, entry['renews_on'], entry['period'], entry['evidence'], entry['source'],
         EXTRACTION_VERSION, stamp, stamp))


def _mark(conn, account_id, email_id, status, *, attempts=0, retry_at=0.0, code=None):
    conn.execute(
        """INSERT INTO ledger_scans(account_id,email_id,status,extraction_version,attempt_count,next_retry_at,
               error_code,updated_at) VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(account_id,email_id) DO UPDATE SET status=excluded.status,
               extraction_version=excluded.extraction_version,attempt_count=excluded.attempt_count,
               next_retry_at=excluded.next_retry_at,error_code=excluded.error_code,updated_at=excluded.updated_at""",
        (account_id, email_id, status, EXTRACTION_VERSION, attempts, retry_at, code, utc_timestamp()))


def due_scans(conn, account_id, limit, now):
    return conn.execute(
        """SELECT e.email_id,e.sender,e.subject,e.body,e.created_at,s.attempt_count FROM email_logs e
           LEFT JOIN ledger_scans s ON s.account_id=e.account_id AND s.email_id=e.email_id
           WHERE e.account_id=? AND COALESCE(e.human_label,e.prediction) IN ('IMPORTANT','UPDATES')
             AND (s.email_id IS NULL OR s.extraction_version!=? OR (s.status='retry' AND s.next_retry_at<=?))
           ORDER BY e.created_at DESC,e.email_id LIMIT ?""",
        (account_id, EXTRACTION_VERSION, now, limit)).fetchall()


def scan_ledger(manager, context, token, *, fence, guard, extract, recorder_factory=None, limit=SCAN_LIMIT,
                now=None, monotonic=time.monotonic):
    """One bounded step. `extract(contents, recorder, guard)` is the cloud
    call (None in local-only mode); returns counts."""
    settings = manager.settings
    now = time.time() if now is None else now
    with manager.guard(context) as conn:
        fence(manager, context, token, conn)
        rows = due_scans(conn, context.account_id, limit, now)
    counts = {'scanned': 0, 'entries': 0, 'not_transaction': 0, 'retry': 0}
    cloud_used = False
    for row in rows:
        counts['scanned'] += 1
        received = _received(row['created_at'])
        if not is_transaction(row['subject'], row['body']):
            with manager.guard(context) as conn:
                fence(manager, context, token, conn)
                _mark(conn, context.account_id, row['email_id'], 'not_transaction')
            counts['not_transaction'] += 1
            continue
        entry, failure = None, None
        last = _LAST_CLOUD_CALL.get(context.account_id, float('-inf'))
        if extract is not None and not settings.local_only:
            if cloud_used or monotonic() - last < LEDGER_CLOUD_INTERVAL:
                counts['scanned'] -= 1
                continue  # this receipt waits for a later cycle's cloud slot
            cloud_used = True
            _LAST_CLOUD_CALL[context.account_id] = monotonic()
            contents, masked, amounts = provider_contents(row['sender'], row['subject'], row['body'], received)
            recorder = recorder_factory(row['email_id'], row['attempt_count'] or 0) if recorder_factory else None
            try:
                answer, source = extract(contents, recorder, guard)
                entry = resolve_cloud_answer(answer, sender=row['sender'], masked=masked, amounts=amounts,
                                             received=received, source=source)
            except Exception as error:
                from .account_state import WorkCancelled
                if isinstance(error, WorkCancelled):
                    raise
                failure = error
        attempts = (row['attempt_count'] or 0) + 1
        with manager.guard(context) as conn:
            fence(manager, context, token, conn)
            temporary = failure is not None and getattr(failure, 'retryable', False)
            if temporary and attempts < settings.max_processing_attempts:
                _mark(conn, context.account_id, row['email_id'], 'retry', attempts=attempts,
                      retry_at=now + retry_delay(attempts), code=getattr(failure, 'code', 'ledger_extraction_failed'))
                counts['retry'] += 1
                continue
            if entry is None:
                # Local-only, no provider, a permanent failure, or out of retries.
                entry = extract_by_rules(row['sender'], row['subject'], row['body'], received)
            if entry is None or entry.get('kind') == 'none':
                _mark(conn, context.account_id, row['email_id'], 'not_transaction')
                counts['not_transaction'] += 1
                continue
            save_entry(conn, context.account_id, row['email_id'], entry, row['created_at'])
            _mark(conn, context.account_id, row['email_id'], 'done', attempts=attempts,
                  code=getattr(failure, 'code', None) if failure else None)
            counts['entries'] += 1
    return counts


def cloud_extractor(settings):
    """The provider call used by the worker: assistant slots, own schema."""
    from .llm_api import call_structured
    def extract(contents, recorder, guard):
        value, source, _model = call_structured(
            contents, system=SYSTEM_INSTRUCTION, schema=OUTPUT_SCHEMA, schema_name='mailmind_ledger',
            settings=settings, operation=TokenOperation.LEDGER_EXTRACTION.value, validate=lambda value: value,
            usage_recorder=recorder, before_request=guard, budget_seconds=settings.classification_budget_seconds,
            max_output_tokens=512, record_key='ledger', max_chars=4000)
        return value, source
    return extract


# --- reads for the Money tab ----------------------------------------------------

def month_bounds(month, timezone_name=DEFAULT_TIMEZONE):
    """UTC start/end of a local calendar month 'YYYY-MM'."""
    zone = ZoneInfo(timezone_name)
    year, number = (int(part) for part in month.split('-'))
    start = datetime(year, number, 1, tzinfo=zone)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=zone)
    return utc_timestamp(start), utc_timestamp(end)


def local_month(moment=None, timezone_name=DEFAULT_TIMEZONE):
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(ZoneInfo(timezone_name)).strftime('%Y-%m')


def _shift_month(month, delta):
    year, number = (int(part) for part in month.split('-'))
    index = year * 12 + number - 1 + delta
    return f'{index // 12:04d}-{index % 12 + 1:02d}'


def public_entry(row):
    return {key: row[key] for key in ('entry_id', 'email_id', 'kind', 'amount_minor', 'currency', 'merchant',
                                      'occurred_at', 'renews_on', 'period', 'evidence', 'source', 'revision')} | {
        'user_corrected': bool(row['user_corrected'])}


def _totals(conn, account_id, start, end):
    totals = {}
    for row in conn.execute(
            """SELECT currency,kind,COUNT(*) AS count,SUM(amount_minor) AS amount FROM ledger_entries
               WHERE account_id=? AND occurred_at>=? AND occurred_at<? GROUP BY currency,kind""",
            (account_id, start, end)):
        bucket = totals.setdefault(row['currency'], {'spent_minor': 0, 'refunds_minor': 0, 'bills_minor': 0,
                                                     'count': 0})
        bucket['count'] += row['count']
        if row['kind'] in SPEND_KINDS:
            bucket['spent_minor'] += row['amount']
        elif row['kind'] == 'refund':
            bucket['refunds_minor'] += row['amount']
        else:
            bucket['bills_minor'] += row['amount']
    for bucket in totals.values():
        bucket['net_minor'] = bucket['spent_minor'] - bucket['refunds_minor']
    return totals


def ledger_summary(conn, account_id, month):
    start, end = month_bounds(month)
    totals = _totals(conn, account_id, start, end)
    primary = conn.execute(
        'SELECT currency FROM ledger_entries WHERE account_id=? GROUP BY currency ORDER BY COUNT(*) DESC LIMIT 1',
        (account_id,)).fetchone()
    currency = primary['currency'] if primary else 'INR'
    trend = []
    for offset in range(-11, 1):
        label = _shift_month(month, offset)
        bucket = _totals(conn, account_id, *month_bounds(label)).get(currency, {})
        trend.append({'month': label, 'spent_minor': bucket.get('spent_minor', 0),
                      'refunds_minor': bucket.get('refunds_minor', 0)})
    merchants = [dict(row) for row in conn.execute(
        f"""SELECT merchant,currency,COUNT(*) AS count,SUM(amount_minor) AS spent_minor FROM ledger_entries
            WHERE account_id=? AND occurred_at>=? AND occurred_at<? AND kind IN ({','.join('?' * len(SPEND_KINDS))})
            GROUP BY merchant,currency ORDER BY spent_minor DESC LIMIT 10""",
        (account_id, start, end, *SPEND_KINDS))]
    scans = {row['status']: row['count'] for row in conn.execute(
        'SELECT status,COUNT(*) AS count FROM ledger_scans WHERE account_id=? GROUP BY status', (account_id,))}
    waiting = conn.execute(
        """SELECT COUNT(*) FROM email_logs e LEFT JOIN ledger_scans s
               ON s.account_id=e.account_id AND s.email_id=e.email_id
           WHERE e.account_id=? AND COALESCE(e.human_label,e.prediction) IN ('IMPORTANT','UPDATES')
             AND (s.email_id IS NULL OR s.extraction_version!=?)""",
        (account_id, EXTRACTION_VERSION)).fetchone()[0]
    return {'month': month, 'currency': currency, 'totals': totals, 'trend': trend, 'merchants': merchants,
            'scan': {'done': scans.get('done', 0), 'not_transaction': scans.get('not_transaction', 0),
                     'retry': scans.get('retry', 0), 'waiting': waiting}}


def list_entries(conn, account_id, *, month=None, merchant=None, limit=50, offset=0):
    clauses, values = ['account_id=?'], [account_id]
    if month:
        start, end = month_bounds(month)
        clauses.append('occurred_at>=? AND occurred_at<?')
        values += [start, end]
    if merchant:
        clauses.append('merchant=?')
        values.append(merchant)
    where = ' AND '.join(clauses)
    total = conn.execute(f'SELECT COUNT(*) FROM ledger_entries WHERE {where}', values).fetchone()[0]
    rows = conn.execute(f'SELECT * FROM ledger_entries WHERE {where} ORDER BY occurred_at DESC,entry_id DESC '
                        'LIMIT ? OFFSET ?', (*values, limit, offset)).fetchall()
    return [public_entry(row) for row in rows], total


class EntryConflict(Exception):
    pass


def correct_entry(conn, account_id, entry_id, expected_revision, changes):
    row = conn.execute('SELECT * FROM ledger_entries WHERE account_id=? AND entry_id=?',
                       (account_id, entry_id)).fetchone()
    if row is None:
        return None
    if row['revision'] != expected_revision:
        raise EntryConflict()
    allowed = {key: value for key, value in changes.items() if key in ('amount_minor', 'merchant', 'kind')}
    if not allowed:
        return public_entry(row)
    assignments = ', '.join(f'{key}=?' for key in allowed)
    conn.execute(f'UPDATE ledger_entries SET {assignments},user_corrected=1,revision=revision+1,updated_at=? '
                 'WHERE account_id=? AND entry_id=?', (*allowed.values(), utc_timestamp(), account_id, entry_id))
    return public_entry(conn.execute('SELECT * FROM ledger_entries WHERE account_id=? AND entry_id=?',
                                     (account_id, entry_id)).fetchone())


def subscriptions(conn, account_id, today=None):
    today = today or datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date()
    since = utc_timestamp(datetime.combine(today - timedelta(days=400), datetime.min.time(), tzinfo=timezone.utc))
    entries = [dict(row) for row in conn.execute(
        'SELECT * FROM ledger_entries WHERE account_id=? AND occurred_at>=? ORDER BY occurred_at',
        (account_id, since))]
    found = [item for item in detect_subscriptions(entries, today)
             if date.fromisoformat(item['renews_on']) >= today - timedelta(days=45)]
    return {'subscriptions': found, 'insights': subscription_insights(found)}

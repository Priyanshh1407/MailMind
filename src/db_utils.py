"""Account-keyed repository; private callers supply their authorized account."""
from contextlib import nullcontext
import json
from .config import CATEGORIES, LEGACY_ACCOUNT, Settings
from .database import connection, utc_timestamp
from .email_text import preview_text
from .prediction import Prediction, Category

DB_PATH = Settings().db_path

REVIEW_MESSAGES = {
    'queued': 'Waiting for classification.',
    'running': 'Classification is currently in progress.',
    'retry': 'Classification will retry after a temporary failure.',
    'dead': 'Classification stopped after repeated failures.',
    'low_confidence': 'The model was not confident enough to choose a category.',
    'category_tie': 'The two most likely categories were too close to choose safely.',
    'provider_timeout': 'The classification provider took too long to respond.',
    'invalid_provider_output': 'The provider returned a result MailMind could not safely use.',
    'cloud_not_configured_or_unavailable': 'No configured cloud classifier was available.',
    'missing_checkpoint': 'The local model checkpoint is unavailable.',
    'model_loading': 'The local model is still loading.',
    'model_process_failed': 'The local model process could not start.',
    'local_inference_failed': 'The local model could not complete this prediction.',
    'legacy_binary_has_no_updates_coverage': 'The installed local model cannot classify all three categories.',
    'message_parse_failed': 'MailMind could not safely read enough of this message.',
    'classification_unavailable': 'No reliable classification was available.',
}


def review_reason(row, latest, task):
    if row['human_label'] or (latest and latest['category']):
        return None
    if task and task['status'] in ('queued','running'):
        code=task['status']
    elif task and task['error_code']:
        code=task['error_code']
    elif latest:
        metadata=json.loads(latest['metadata'])
        code=metadata.get('reason') or latest['outcome'].lower()
    elif row['parse_warnings'] != '[]':
        code='message_parse_failed'
    else:
        code='classification_unavailable'
    message=REVIEW_MESSAGES.get(code)
    if message is None:
        state=task['status'] if task and task['status'] in ('retry','dead') else 'classification_unavailable'
        message=REVIEW_MESSAGES[state]
        code=state
    return {'code':code,'message':message}


def log_email_to_db(email_id, sender, subject, body, prediction, local_prediction, human_label=None, *, account_id=LEGACY_ACCOUNT, message_type="gmail", db_path=None, db_conn=None, body_truncated=False, body_kind=None, parse_warnings=None):
    result = prediction if isinstance(prediction, Prediction) else Prediction(category=prediction if prediction in CATEGORIES else None, outcome='CLASSIFIED' if prediction in CATEGORIES else 'ERROR', source='agent')
    category = result.category
    local_category = local_prediction.category if isinstance(local_prediction, Prediction) else local_prediction if local_prediction in CATEGORIES else None
    outcome = result.outcome
    timestamp = utc_timestamp()
    with (nullcontext(db_conn) if db_conn is not None else connection(db_path)) as conn:
        conn.execute("INSERT INTO accounts(account_id) VALUES (?) ON CONFLICT DO NOTHING", (account_id,))
        inserted = conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,prediction,local_prediction,human_label,message_type,processing_state,created_at,body_truncated,body_kind,parse_warnings)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,email_id) DO NOTHING""", (
            account_id,email_id,sender or "",subject or "",body or "",category,local_category,human_label,message_type,
            "classified" if category else "abstained" if outcome == 'ABSTAIN' else "error",timestamp,int(body_truncated),body_kind,json.dumps(parse_warnings or [])))
        if inserted.rowcount and human_label is not None:
            conn.execute("INSERT INTO feedback_history(account_id,email_id,label,created_at) VALUES (?,?,?,?)",
                         (account_id,email_id,human_label,timestamp))
        if not inserted.rowcount:
            # Preserve the first successful categories, while allowing a later
            # success to fill rows whose earlier provider attempts had no label.
            conn.execute('UPDATE email_logs SET prediction=COALESCE(prediction,?),local_prediction=COALESCE(local_prediction,?) WHERE account_id=? AND email_id=?',(category,local_category,account_id,email_id))
        metadata = result.to_dict()
        if isinstance(local_prediction, Prediction):
            metadata['local'] = local_prediction.to_dict()
        conn.execute("""INSERT INTO prediction_attempts(account_id,email_id,category,local_category,outcome,source,model_version,metadata,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", (account_id,email_id,category,local_category,outcome,result.source,result.model_version,json.dumps(metadata),timestamp))


def get_recent_emails(limit=50, *, account_id=None, db_path=None, db_conn=None, offset=0, search='', category=None, ranked_ids=None):
    # None is reserved for deliberate local maintenance. HTTP/worker callers
    # always supply the authorized account.
    with (nullcontext(db_conn) if db_conn is not None else connection(db_path)) as conn:
        if ranked_ids is None:
            where, parameters = email_filter(account_id, search, category)
            rows = conn.execute(f'SELECT e.* FROM email_logs e {where} ORDER BY e.created_at DESC,e.account_id,e.email_id LIMIT ? OFFSET ?', (*parameters,limit,offset)).fetchall()
        elif not ranked_ids:
            rows = []
        else:
            page_ids = list(ranked_ids)[offset:offset+limit]
            placeholders = ','.join('?' for _ in page_ids)
            rows_by_id = {row['email_id']:row for row in conn.execute(
                f'SELECT e.* FROM email_logs e WHERE e.account_id=? AND e.email_id IN ({placeholders})',
                (account_id,*page_ids)).fetchall()}
            rows = [rows_by_id[identity] for identity in page_ids if identity in rows_by_id]
        output = []
        for row in rows:
            latest = conn.execute('SELECT * FROM prediction_attempts WHERE account_id=? AND email_id=? ORDER BY attempt_id DESC LIMIT 1', (row['account_id'],row['email_id'])).fetchone()
            feedback = conn.execute('SELECT revision_id,label,indexing_state FROM feedback_history WHERE account_id=? AND email_id=? ORDER BY revision_id DESC LIMIT 1', (row['account_id'],row['email_id'])).fetchone()
            task = conn.execute('SELECT status,stage,attempt_count,next_retry_at,error_code FROM processing_tasks WHERE account_id=? AND email_id=?',(row['account_id'],row['email_id'])).fetchone()
            delivery = conn.execute('SELECT status,attempt_count,next_retry_at,error_code,provider_message_id FROM notification_outbox WHERE account_id=? AND email_id=?',(row['account_id'],row['email_id'])).fetchone()
            output.append({'processing':dict(task) if task else None, 'notification':dict(delivery) if delivery else None, **dict(row), 'id':row['email_id'], 'body_snippet':preview_text(row['body']),
                           'parse_warnings':json.loads(row['parse_warnings']),
                           'latest_prediction': {**json.loads(latest['metadata']), 'category':latest['category'], 'outcome':latest['outcome'], 'source':latest['source'], 'model_version':latest['model_version']} if latest else None,
                           'review_reason':review_reason(row,latest,task),
                           'effective_category':row['human_label'] or (latest['category'] if latest else row['prediction']),
                           'feedback':dict(feedback) if feedback else None})
        return output


def clear_all_emails(*, account_id=None, db_path=None):
    with connection(db_path) as conn:
        if account_id is None:
            conn.execute("DELETE FROM email_logs")
        else:
            conn.execute("DELETE FROM email_logs WHERE account_id=?", (account_id,))


def get_email(email_id, *, account_id=LEGACY_ACCOUNT, db_path=None, db_conn=None):
    with (nullcontext(db_conn) if db_conn is not None else connection(db_path)) as conn:
        row = conn.execute("SELECT * FROM email_logs WHERE account_id=? AND email_id=?", (account_id,email_id)).fetchone()
        return dict(row) if row else None


def update_human_label(email_id, human_label, *, account_id=LEGACY_ACCOUNT, db_path=None, db_conn=None):
    human_label = Category(human_label).value
    with (nullcontext(db_conn) if db_conn is not None else connection(db_path)) as conn:
        cursor = conn.execute("UPDATE email_logs SET human_label=? WHERE account_id=? AND email_id=?", (human_label,account_id,email_id))
        if not cursor.rowcount:
            raise LookupError('Email does not belong to this account')
        return conn.execute("INSERT INTO feedback_history(account_id,email_id,label,created_at) VALUES (?,?,?,?)", (account_id,email_id,human_label,utc_timestamp())).lastrowid


def get_ingestion_state(account_id, conn):
    row = conn.execute('SELECT * FROM ingestion_state WHERE account_id=?', (account_id,)).fetchone()
    return dict(row) if row else None


def ensure_ingestion_state(account_id, conn, *, batch_limit):
    if type(batch_limit) is not int or batch_limit < 1:
        raise ValueError('Invalid initial inbox batch limit')
    stamp=utc_timestamp()
    conn.execute("""INSERT INTO ingestion_state(
        account_id,page_token,status,fetched_count,failed_count,pages_count,
        scanned_count,warning_count,truncated_count,has_more,listing_error,last_checked_at,
        backlog_authorized,backlog_remaining,initial_batch_complete)
        VALUES (?,NULL,'empty',0,0,0,0,0,0,1,0,?,1,?,0)
        ON CONFLICT(account_id) DO NOTHING""",(account_id,stamp,batch_limit))
    return get_ingestion_state(account_id,conn)


def save_ingestion_state(account_id, batch, conn):
    summary = batch.summary()
    conn.execute("""INSERT INTO ingestion_state(account_id,page_token,status,fetched_count,failed_count,pages_count,
        scanned_count,warning_count,truncated_count,has_more,listing_error,last_checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id) DO UPDATE SET page_token=excluded.page_token,status=excluded.status,
        fetched_count=excluded.fetched_count,failed_count=excluded.failed_count,pages_count=excluded.pages_count,
        scanned_count=excluded.scanned_count,warning_count=excluded.warning_count,truncated_count=excluded.truncated_count,
        has_more=excluded.has_more,listing_error=excluded.listing_error,last_checked_at=excluded.last_checked_at""",
        (account_id,batch.next_page_token,summary['status'],summary['fetched_count'],summary['failed_count'],
         summary['pages_count'],summary['scanned_count'],summary['warning_count'],summary['truncated_count'],
         int(batch.has_more),int(batch.listing_error),utc_timestamp()))


def save_live_ingestion_state(account_id, batch, conn, *, history_id=None, error_code=None):
    summary=batch.summary()
    conn.execute("""UPDATE ingestion_state SET
        history_id=COALESCE(?,history_id),live_status=?,live_fetched_count=?,
        last_live_sync_at=?,last_live_error=? WHERE account_id=?""",
        (history_id,summary['status'],summary['fetched_count'],utc_timestamp(),
         error_code,account_id))


EFFECTIVE_SQL = """COALESCE(e.human_label, CASE WHEN EXISTS (
    SELECT 1 FROM prediction_attempts p WHERE p.account_id=e.account_id AND p.email_id=e.email_id)
    THEN (SELECT category FROM prediction_attempts p WHERE p.account_id=e.account_id AND p.email_id=e.email_id ORDER BY attempt_id DESC LIMIT 1)
    ELSE e.prediction END)"""


def email_filter(account_id, search="", category=None):
    clauses, parameters = [], []
    if account_id is not None:
        clauses.append('e.account_id=?')
        parameters.append(account_id)
    if search:
        # Treat %, _ and backslash as literal search text, never SQL wildcards.
        escaped = search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        clauses.append("(e.subject LIKE ? ESCAPE '\\' OR e.sender LIKE ? ESCAPE '\\' OR e.body LIKE ? ESCAPE '\\')")
        parameters.extend(['%'+escaped+'%']*3)
    if category:
        if category == 'NEEDS_REVIEW':
            clauses.append(EFFECTIVE_SQL+' IS NULL')
        else:
            clauses.append(EFFECTIVE_SQL+'=?')
            parameters.append(Category(category).value)
    return ('WHERE '+' AND '.join(clauses) if clauses else ''), parameters


def count_emails(account_id, conn, *, search="", category=None):
    where, parameters = email_filter(account_id, search, category)
    return conn.execute(f'SELECT COUNT(*) FROM email_logs e {where}', parameters).fetchone()[0]


def lexical_search_ids(account_id, conn, search, category=None):
    where, parameters = email_filter(account_id, search, category)
    return [row['email_id'] for row in conn.execute(
        f'SELECT e.email_id FROM email_logs e {where} ORDER BY e.created_at DESC,e.email_id',
        parameters).fetchall()]


def filter_ranked_ids(account_id, conn, identities, category=None):
    ordered = list(dict.fromkeys(identities))[:900]
    if not ordered:
        return []
    placeholders = ','.join('?' for _ in ordered)
    where, parameters = email_filter(account_id, '', category)
    allowed = {row['email_id'] for row in conn.execute(
        f'SELECT e.email_id FROM email_logs e {where} AND e.email_id IN ({placeholders})',
        (*parameters,*ordered)).fetchall()}
    return [identity for identity in ordered if identity in allowed]


def dashboard_totals(account_id, conn):
    row = conn.execute("""SELECT COUNT(*) AS saved,
        COALESCE(SUM(processing_state='complete'),0) AS completed,
        COALESCE(SUM(human_label IS NOT NULL),0) AS labelled,
        COALESCE(SUM(human_label IS NOT NULL AND prediction IS NOT NULL AND human_label!=prediction),0) AS corrected,
        COALESCE(SUM(human_label IS NOT NULL AND prediction IS NOT NULL AND human_label=prediction),0) AS confirmed
        FROM email_logs WHERE account_id=?""", (account_id,)).fetchone()
    output = dict(row)
    output['feedback_events'] = conn.execute('SELECT COUNT(*) FROM feedback_history WHERE account_id=?',(account_id,)).fetchone()[0]
    output['prediction_attempts'] = conn.execute('SELECT COUNT(*) FROM prediction_attempts WHERE account_id=?',(account_id,)).fetchone()[0]
    return output


def withdraw_human_label(email_id, *, account_id, db_conn):
    if not db_conn.execute('UPDATE email_logs SET human_label=NULL WHERE account_id=? AND email_id=?',(account_id,email_id)).rowcount:
        raise LookupError('Email does not belong to this account')
    return db_conn.execute('INSERT INTO feedback_history(account_id,email_id,label,created_at) VALUES (?,?,NULL,?)',(account_id,email_id,utc_timestamp())).lastrowid

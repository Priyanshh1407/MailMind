"""Durable account/message steps plus fenced, expiring cycle ownership."""
import json
import secrets
import time
from .database import utc_timestamp
from .account_state import WorkCancelled
from .provider_policy import retry_delay


def enqueue_cycle(manager, context):
    with manager.guard(context) as conn:
        row=conn.execute("SELECT job_id FROM worker_jobs WHERE account_id=? AND generation=? AND kind='processing' AND status IN ('queued','running') ORDER BY job_id LIMIT 1",(context.account_id,context.generation)).fetchone()
        if row:
            return row['job_id']
        stamp=utc_timestamp()
        return conn.execute("INSERT INTO worker_jobs(account_id,status,created_at,updated_at,generation) VALUES (?,'queued',?,?,?)",(context.account_id,stamp,stamp,context.generation)).lastrowid


def claim_cycle(manager, context, *, now=None):
    now=time.time() if now is None else now
    with manager.guard(context) as conn:
        state=manager.state(conn)
        if state['worker_token'] and state['lease_until'] > now:
            return None
        # Never steal a live lease, including one owned by another process.
        token=secrets.token_hex(24)
        stamp=utc_timestamp()
        conn.execute("UPDATE worker_jobs SET status='failed',error_code='lease_expired',updated_at=? WHERE account_id=? AND status='running'",(stamp,context.account_id))
        conn.execute("UPDATE processing_tasks SET status='retry',owner_token=NULL,error_code='interrupted',next_retry_at=0 WHERE account_id=? AND status='running'",(context.account_id,))
        # Sending may have reached Telegram before a process died.
        conn.execute("UPDATE notification_outbox SET status='unknown',error_code='interrupted_send',updated_at=? WHERE account_id=? AND status='sending'",(stamp,context.account_id))
        row=conn.execute("SELECT job_id FROM worker_jobs WHERE account_id=? AND generation=? AND kind='processing' AND status='queued' ORDER BY job_id LIMIT 1",(context.account_id,context.generation)).fetchone()
        if row:
            job_id=row['job_id']
            conn.execute("UPDATE worker_jobs SET status='running',owner_token=?,updated_at=? WHERE job_id=?",(token,stamp,job_id))
        else:
            job_id=conn.execute("INSERT INTO worker_jobs(account_id,status,created_at,updated_at,generation,owner_token) VALUES (?,'running',?,?,?,?)",(context.account_id,stamp,stamp,context.generation,token)).lastrowid
        conn.execute('INSERT INTO worker_health(account_id,heartbeat_at) VALUES (?,?) ON CONFLICT(account_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at',(context.account_id,stamp))
        conn.execute('UPDATE runtime_state SET worker_token=?,lease_until=?,is_polling=1,heartbeat_at=?',(token,now+manager.settings.worker_lease_seconds,stamp))
        return token,job_id


def fence(manager, context, token, conn):
    state=manager.state(conn)
    if state['worker_token'] != token or state['lease_until'] <= time.time():
        raise WorkCancelled()
    conn.execute('UPDATE worker_health SET heartbeat_at=? WHERE account_id=?',(utc_timestamp(),context.account_id))
    conn.execute('UPDATE runtime_state SET heartbeat_at=?,lease_until=?',(utc_timestamp(),time.time()+manager.settings.worker_lease_seconds))


def finish_cycle(manager, context, token, job_id, error_code=None):
    with manager.guard(context) as conn:
        if manager.state(conn)['worker_token'] != token:
            return
        stamp=utc_timestamp()
        conn.execute("UPDATE worker_jobs SET status=?,error_code=?,updated_at=? WHERE job_id=? AND owner_token=?",('failed' if error_code else 'complete',error_code,stamp,job_id,token))
        conn.execute('UPDATE runtime_state SET is_polling=0,worker_token=NULL,lease_until=0,heartbeat_at=?',(stamp,))
        if error_code:
            conn.execute('UPDATE worker_health SET last_error_at=?,last_error_code=? WHERE account_id=?',(stamp,error_code,context.account_id))
            conn.execute('UPDATE runtime_state SET last_error_at=?,last_error_code=?',(stamp,error_code))
        else:
            conn.execute('UPDATE worker_health SET last_success_at=?,last_error_at=NULL,last_error_code=NULL WHERE account_id=?',(stamp,context.account_id))
            conn.execute('UPDATE runtime_state SET last_success_at=?,last_error_at=NULL,last_error_code=NULL',(stamp,))


def ingest_email(email, manager, context, token, *, source='backlog'):
    if source not in ('live','backlog'):
        raise ValueError('Invalid ingestion source')
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,created_at,body_truncated,body_kind,parse_warnings)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,email_id) DO NOTHING""",
            (context.account_id,email['id'],email['sender'],email['subject'],email['body'],utc_timestamp(),
             int(email.get('body_truncated',False)),email.get('body_kind'),json.dumps(email.get('parse_warnings',[]))))
        stamp=utc_timestamp()
        conn.execute("""INSERT INTO processing_tasks(account_id,email_id,created_at,updated_at,read_required,source)
            VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,email_id) DO NOTHING""",
            (context.account_id,email['id'],stamp,stamp,int(manager.settings.auto_mark_read),source))
        conn.execute('DELETE FROM ingestion_failures WHERE account_id=? AND email_id=?',(context.account_id,email['id']))


def record_ingestion_failure(failure, manager, context, token):
    identity=failure.get('email_id')
    if not identity:
        return
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        row=conn.execute('SELECT attempt_count FROM ingestion_failures WHERE account_id=? AND email_id=?',(context.account_id,identity)).fetchone()
        count=(row['attempt_count'] if row else 0)+1
        status='dead' if count >= manager.settings.max_processing_attempts else 'retry'
        conn.execute("""INSERT INTO ingestion_failures(account_id,email_id,attempt_count,status,next_retry_at,error_code,updated_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id,email_id) DO UPDATE SET attempt_count=excluded.attempt_count,
            status=excluded.status,next_retry_at=excluded.next_retry_at,error_code=excluded.error_code,updated_at=excluded.updated_at""",
            (context.account_id,identity,count,status,time.time()+retry_delay(count),failure['code'],utc_timestamp()))


def excluded_messages(conn, account_id):
    # Saved tasks use durable bodies; don't download already-adopted mail again.
    result={row[0] for row in conn.execute('SELECT email_id FROM processing_tasks WHERE account_id=?',(account_id,))}
    result.update(row[0] for row in conn.execute("SELECT email_id FROM ingestion_failures WHERE account_id=? AND (status='dead' OR next_retry_at>?)",(account_id,time.time())))
    return result


def due_tasks(manager, context, token, limit):
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        return [dict(row) for row in conn.execute("SELECT t.*,e.sender,e.subject,e.body FROM processing_tasks t JOIN email_logs e USING(account_id,email_id) WHERE t.account_id=? AND t.status IN ('queued','retry') AND t.next_retry_at<=? ORDER BY t.created_at,t.email_id LIMIT ?",(context.account_id,time.time(),limit))]


def newest_due_tasks(manager, context, token, limit):
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        query = '''SELECT t.*,e.sender,e.subject,e.body
            FROM processing_tasks t JOIN email_logs e USING(account_id,email_id)
            WHERE t.account_id=? AND t.status IN ('queued','retry') AND t.next_retry_at<=?
            ORDER BY CASE t.source WHEN 'live' THEN 0 ELSE 1 END,
                     CASE t.status WHEN 'queued' THEN 0 ELSE 1 END,
                     t.created_at DESC,t.email_id LIMIT ?'''
        return [dict(row) for row in conn.execute(query,(context.account_id,time.time(),limit))]


def attempt(conn, context, identity, stage, outcome, code=None):
    conn.execute('INSERT INTO processing_attempts(account_id,email_id,stage,outcome,error_code,created_at) VALUES (?,?,?,?,?,?)',(context.account_id,identity,stage,outcome,code,utc_timestamp()))


def task_retry(conn, context, task, stage, code, *, retryable=True, delay=None, max_attempts=3):
    row=conn.execute('SELECT attempt_count FROM processing_tasks WHERE account_id=? AND email_id=?',(context.account_id,task['email_id'])).fetchone()
    count=row[0]+1
    status='retry' if retryable and count < max_attempts else 'dead'
    conn.execute('UPDATE processing_tasks SET status=?,stage=?,attempt_count=?,next_retry_at=?,error_code=?,owner_token=NULL,updated_at=? WHERE account_id=? AND email_id=?',
        (status,stage,count,time.time()+(delay if delay is not None else retry_delay(count)),code,utc_timestamp(),context.account_id,task['email_id']))
    attempt(conn,context,task['email_id'],stage,status,code)

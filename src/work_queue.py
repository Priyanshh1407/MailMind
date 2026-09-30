"""Durable account/message steps plus fenced, expiring cycle ownership."""
import json
from datetime import datetime, timezone
import secrets
import time
from .database import utc_timestamp
from .email_analysis import save_analysis_result
from .action_center import expire_snoozed_actions, persist_analysis_actions
from .privacy import provider_email_text
from .logging_utils import log_event
from .prediction import EmailAnalysis
from .account_state import WorkCancelled
from .provider_policy import provider_failure, retry_delay


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
        conn.execute(
            """UPDATE intelligence_backfill_items
               SET status='retry',error_code='interrupted',updated_at=?
               WHERE account_id=? AND status='running'""",
            (stamp,context.account_id))
        # Sending may have reached Telegram before a process died.
        conn.execute("UPDATE notification_outbox SET status='unknown',error_code='interrupted_send',updated_at=? WHERE account_id=? AND status='sending'",(stamp,context.account_id))
        conn.execute("""UPDATE action_reminders SET
            status=CASE channel WHEN 'dashboard' THEN 'retry' ELSE 'dead' END,
            owner_token=NULL,next_retry_at=0,
            error_code=CASE channel WHEN 'dashboard' THEN 'interrupted'
                            ELSE 'reminder_delivery_unknown' END,
            updated_at=? WHERE account_id=? AND status='claimed'""",
            (stamp,context.account_id))
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
            (context.account_id,email['id'],email['sender'],email['subject'],email['body'],email.get('source_created_at') or utc_timestamp(),
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



def reconcile_email_analysis(manager, context, token, limit=20):
    """Retry bounded analysis/action writes without repeating classification."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid analysis reconciliation limit')
    completed = 0
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        rows = conn.execute(
            """SELECT p.email_id,p.metadata,e.subject,e.body,e.created_at,
                      CASE WHEN a.email_id IS NULL THEN 1 ELSE 0 END AS analysis_missing
               FROM prediction_attempts p
               JOIN (
                   SELECT account_id,email_id,MAX(attempt_id) AS attempt_id
                   FROM prediction_attempts
                   WHERE account_id=?
                   GROUP BY account_id,email_id
               ) latest ON latest.attempt_id=p.attempt_id
               JOIN email_logs e
                 ON e.account_id=p.account_id AND e.email_id=p.email_id
               LEFT JOIN email_analysis a
                 ON a.account_id=p.account_id AND a.email_id=p.email_id
               WHERE p.account_id=? AND (
                   a.email_id IS NULL OR (
                       ?=1
                       AND json_type(p.metadata,'$.analysis.actions')='array'
                       AND json_array_length(p.metadata,'$.analysis.actions')>0
                       AND NOT EXISTS (
                           SELECT 1 FROM processing_attempts x
                           WHERE x.account_id=p.account_id
                             AND x.email_id=p.email_id
                             AND x.stage='actions' AND x.outcome='complete'
                       )
                   )
               )
               ORDER BY p.attempt_id
               LIMIT ?""",
            (context.account_id,context.account_id,
             int(manager.settings.action_extraction_enabled),limit),
        ).fetchall()
        for row in rows:
            try:
                metadata=json.loads(row['metadata'])
                payload=metadata.get('analysis') if isinstance(metadata,dict) else None
                if not isinstance(payload,dict):
                    continue
                analysis=EmailAnalysis.from_dict(payload)
                if row['analysis_missing']:
                    save_analysis_result(
                        context.account_id,row['email_id'],analysis,db_conn=conn)
                    attempt(conn,context,row['email_id'],'analysis','complete')
                if (manager.settings.action_extraction_enabled
                        and analysis.actions):
                    conn.execute('SAVEPOINT reconcile_actions')
                    try:
                        persist_analysis_actions(
                            context.account_id,row['email_id'],analysis,
                            source_created_at=row['created_at'],
                            source_text=provider_email_text(
                                row['subject'],row['body']),
                            timezone_name=manager.settings.default_timezone,
                            reminders_enabled=(
                                manager.settings.action_reminders_enabled),
                            db_conn=conn,
                        )
                        attempt(
                            conn,context,row['email_id'],'actions','complete')
                        conn.execute('RELEASE SAVEPOINT reconcile_actions')
                    except Exception:
                        conn.execute(
                            'ROLLBACK TO SAVEPOINT reconcile_actions')
                        conn.execute('RELEASE SAVEPOINT reconcile_actions')
                        attempt(
                            conn,context,row['email_id'],'actions','retry',
                            'action_persistence_failed')
                        raise
                completed += 1
            except Exception as error:
                log_event('analysis_reconciliation_deferred',error=error)
        fence(manager,context,token,conn)
    return completed


def excluded_messages(conn, account_id):
    # Saved tasks use durable bodies; don't download already-adopted mail again.
    result={row[0] for row in conn.execute('SELECT email_id FROM processing_tasks WHERE account_id=?',(account_id,))}
    result.update(row[0] for row in conn.execute("SELECT email_id FROM ingestion_failures WHERE account_id=? AND (status='dead' OR next_retry_at>?)",(account_id,time.time())))
    return result


def due_tasks(manager, context, token, limit):
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        return [dict(row) for row in conn.execute("SELECT t.*,e.sender,e.subject,e.body,e.created_at AS email_created_at FROM processing_tasks t JOIN email_logs e USING(account_id,email_id) WHERE t.account_id=? AND t.status IN ('queued','retry') AND t.next_retry_at<=? ORDER BY t.created_at,t.email_id LIMIT ?",(context.account_id,time.time(),limit))]


def newest_due_tasks(manager, context, token, limit):
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        query = '''SELECT t.*,e.sender,e.subject,e.body,
                   e.created_at AS email_created_at,
                   CASE WHEN backfill.email_id IS NULL THEN 0 ELSE 1 END
                       AS intelligence_backfill
            FROM processing_tasks t JOIN email_logs e USING(account_id,email_id)
            LEFT JOIN intelligence_backfill_items backfill
              ON backfill.account_id=t.account_id
             AND backfill.email_id=t.email_id
            WHERE t.account_id=? AND t.status IN ('queued','retry') AND t.next_retry_at<=?
            ORDER BY CASE
                       WHEN t.source='live' THEN 0
                       WHEN backfill.email_id IS NULL THEN 1
                       ELSE 2
                     END,
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
    if task.get('intelligence_backfill'):
        conn.execute(
            """UPDATE intelligence_backfill_items
               SET status=?,error_code=?,updated_at=?
               WHERE account_id=? AND email_id=?""",
            (status,code,utc_timestamp(),context.account_id,task['email_id']))
    attempt(conn,context,task['email_id'],stage,status,code)



def process_due_reminders(manager, context, token, notifier, *, limit=20,
                          now=None):
    """Claim and finish a bounded reminder batch under the existing worker lease."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid reminder limit')
    current = now or datetime.now(timezone.utc)
    if not isinstance(current,datetime) or current.tzinfo is None:
        raise ValueError('Reminder time must include a timezone')
    stamp=utc_timestamp(current)
    epoch=current.timestamp()
    stats={'processed':0,'delivered':0,'retry':0,'dead':0,'snoozes_expired':0}
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        stats['snoozes_expired']=expire_snoozed_actions(
            context.account_id,now=current,db_conn=conn)
    for _ in range(limit):
        with manager.guard(context) as conn:
            fence(manager,context,token,conn)
            row=conn.execute(
                """SELECT r.*,a.title AS action_title,
                          a.description AS action_description,
                          a.evidence AS action_evidence
                   FROM action_reminders r
                   JOIN email_actions a
                     ON a.account_id=r.account_id AND a.action_id=r.action_id
                   WHERE r.account_id=? AND a.status='open'
                     AND r.status IN ('scheduled','retry')
                     AND r.next_retry_at<=? AND r.remind_at<=?
                   ORDER BY r.remind_at,r.reminder_id LIMIT 1""",
                (context.account_id,epoch,stamp)).fetchone()
            if row is None:
                break
            claimed=conn.execute(
                """UPDATE action_reminders SET status='claimed',owner_token=?,
                          updated_at=?
                   WHERE account_id=? AND reminder_id=?
                     AND status IN ('scheduled','retry')""",
                (token,stamp,context.account_id,row['reminder_id']))
            if not claimed.rowcount:
                continue
            reminder=dict(row)
        if reminder['channel']=='dashboard':
            delivery_status='delivered'
            error_code=None
            retry_after=None
        elif not manager.settings.telegram_action_reminders_enabled:
            delivery_status='dead'
            error_code='telegram_reminders_disabled'
            retry_after=None
        else:
            try:
                with manager.guard(context) as conn:
                    fence(manager,context,token,conn)
                delivery=notifier(
                    'MailMind Action Reminder',
                    reminder['action_title'],
                    reminder['action_description'] or reminder['action_evidence'],
                    settings=manager.settings,
                )
                if delivery.status=='sent':
                    delivery_status='delivered'
                    error_code=None
                elif delivery.status=='retry':
                    delivery_status='retry'
                    error_code=delivery.code or 'reminder_transient'
                elif delivery.status=='unknown':
                    delivery_status='dead'
                    error_code='reminder_delivery_unknown'
                else:
                    delivery_status='dead'
                    error_code=delivery.code or 'reminder_rejected'
                retry_after=getattr(delivery,'retry_after',None)
            except WorkCancelled:
                raise
            except Exception as error:
                failure=provider_failure(error)
                delivery_status=(
                    'dead' if failure.ambiguous or not failure.retryable
                    else 'retry')
                error_code=(
                    'reminder_delivery_unknown' if failure.ambiguous
                    else failure.code)
                retry_after=None
        with manager.guard(context) as conn:
            fence(manager,context,token,conn)
            owned=conn.execute(
                """SELECT status,owner_token,attempt_count
                   FROM action_reminders
                   WHERE account_id=? AND reminder_id=?""",
                (context.account_id,reminder['reminder_id'])).fetchone()
            if (owned is None or owned['status']!='claimed'
                    or owned['owner_token']!=token):
                continue
            attempts=owned['attempt_count']+1
            target=delivery_status
            next_retry=0
            if target=='retry':
                if attempts>=manager.settings.max_processing_attempts:
                    target='dead'
                    error_code='reminder_retry_exhausted'
                else:
                    delay=(retry_after if isinstance(retry_after,(int,float))
                           and 0<retry_after<=86400
                           else retry_delay(attempts))
                    next_retry=epoch+delay
            conn.execute(
                """UPDATE action_reminders SET status=?,attempt_count=?,
                          owner_token=NULL,next_retry_at=?,error_code=?,
                          updated_at=?
                   WHERE account_id=? AND reminder_id=? AND owner_token=?""",
                (target,attempts,next_retry,error_code,utc_timestamp(),
                 context.account_id,reminder['reminder_id'],token))
        stats['processed']+=1
        stats[target]+=1
    return stats

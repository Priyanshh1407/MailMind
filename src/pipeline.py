"""Resume each saved message at its first unfinished step."""
from contextlib import contextmanager
import time
from .account_state import WorkCancelled
from .database import utc_timestamp
from .prediction import Prediction
from .provider_policy import provider_failure, retry_delay, PROVIDER_CALLS, LOCAL_CALLS
from .notifier import Delivery
from .email_text import preview_text
from .work_queue import fence, attempt, task_retry


@contextmanager
def cycle_external(manager, context, token):
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
    yield
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)


def process_task(task, manager, context, token, service, model, collection_provider,
                 *, classifier, shadow, notifier, marker, logger, validator):
    identity=task['email_id']
    config=manager.settings
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        claimed=conn.execute("UPDATE processing_tasks SET status='running',owner_token=?,updated_at=? WHERE account_id=? AND email_id=? AND status IN ('queued','retry') AND next_retry_at<=?",(token,utc_timestamp(),context.account_id,identity,time.time()))
        if not claimed.rowcount:
            return False
    stage=task['stage']
    if stage == 'classify':
        try:
            if config.local_only:
                with cycle_external(manager,context,token):
                    decision=LOCAL_CALLS.run(lambda:model.predict(task['subject'],task['body'],account_id=context.account_id),config.classification_budget_seconds)
                local=decision
            else:
                with cycle_external(manager,context,token):
                    decision=classifier(task['sender'],task['subject'],task['body'],account_id=context.account_id,
                        collection_provider=collection_provider,validator=validator,settings=config,
                        before_request=lambda:cycle_external(manager,context,token))
                try:
                    with cycle_external(manager,context,token):
                        local,_=LOCAL_CALLS.run(lambda:shadow(task['subject'],task['body'],decision,account_id=context.account_id,model=model),20)
                except WorkCancelled:
                    raise
                except Exception:
                    local=Prediction(outcome='ERROR',reason='shadow_inference_failed')
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                logger(identity,task['sender'],task['subject'],task['body'],decision,local,
                       account_id=context.account_id,db_conn=conn)
                attempt(conn,context,identity,'classify',decision.outcome,decision.reason)
                if decision.outcome != 'CLASSIFIED':
                    code=decision.reason or 'classification_unavailable'
                    retryable=code not in ('invalid_provider_output','provider_auth','provider_invalid_request')
                    task_retry(conn,context,task,'classify',code,retryable=retryable,max_attempts=config.max_processing_attempts)
                    return False
                stage='notify' if decision.category == 'IMPORTANT' else 'mark_read'
                conn.execute('UPDATE processing_tasks SET stage=?,category=?,attempt_count=0,error_code=NULL WHERE account_id=? AND email_id=?',(stage,decision.category,context.account_id,identity))
                conn.execute("UPDATE email_logs SET processing_state='classified' WHERE account_id=? AND email_id=?",(context.account_id,identity))
                if stage == 'notify':
                    stamp=utc_timestamp()
                    conn.execute("INSERT INTO notification_outbox(account_id,email_id,created_at,updated_at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",(context.account_id,identity,stamp,stamp))
        except WorkCancelled:
            raise
        except Exception as error:
            failure=provider_failure(error)
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                task_retry(conn,context,task,'classify',failure.code,retryable=failure.retryable,max_attempts=config.max_processing_attempts)
            return False
    if stage == 'notify':
        if config.local_only:
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                previous=conn.execute('SELECT status FROM notification_outbox WHERE account_id=? AND email_id=?',(context.account_id,identity)).fetchone()
                if previous['status'] != 'sent':
                    ambiguous=previous['status'] in ('unknown','sending')
                    code='delivery_unknown' if ambiguous else 'notification_disabled_local_only'
                    if not ambiguous:
                        conn.execute("UPDATE notification_outbox SET status='blocked',error_code=? WHERE account_id=? AND email_id=?",(code,context.account_id,identity))
                    task_retry(conn,context,task,'notify',code,retryable=False,max_attempts=config.max_processing_attempts)
                    return False
        with manager.guard(context) as conn:
            fence(manager,context,token,conn)
            outbox=conn.execute('SELECT * FROM notification_outbox WHERE account_id=? AND email_id=?',(context.account_id,identity)).fetchone()
            if outbox['status'] == 'sent':
                stage='mark_read'
            elif outbox['status'] in ('unknown','sending'):
                task_retry(conn,context,task,'notify','delivery_unknown',retryable=False,max_attempts=config.max_processing_attempts)
                return False
            elif outbox['next_retry_at'] > time.time():
                conn.execute("UPDATE processing_tasks SET status='retry',next_retry_at=?,owner_token=NULL WHERE account_id=? AND email_id=?",(outbox['next_retry_at'],context.account_id,identity))
                return False
            else:
                # Commit 'sending' BEFORE contacting Telegram. A crash is ambiguous.
                conn.execute("UPDATE notification_outbox SET status='sending',attempt_count=attempt_count+1,updated_at=? WHERE account_id=? AND email_id=?",(utc_timestamp(),context.account_id,identity))
        if stage == 'notify':
            try:
                with cycle_external(manager,context,token):
                    def send():
                        with cycle_external(manager,context,token):
                            from .privacy import redact
                        from .notifier import send_telegram_alert
                        return notifier('[SENDER]',redact(task['subject']),preview_text(redact(task['body']))[:100]+'...',timeout=config.provider_timeout_seconds,**({'settings':config} if notifier is send_telegram_alert else {}))
                    delivery=PROVIDER_CALLS.run(send,config.provider_timeout_seconds)
                # Explicit compatibility for injected old boolean test adapters.
                if isinstance(delivery,bool):
                    delivery=Delivery('sent' if delivery else 'retry','delivery_failed' if not delivery else None)
            except WorkCancelled:
                raise
            except Exception as error:
                failure=provider_failure(error)
                delivery=Delivery('unknown' if failure.ambiguous else 'retry' if failure.retryable else 'blocked',failure.code)
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                count=conn.execute('SELECT attempt_count FROM notification_outbox WHERE account_id=? AND email_id=?',(context.account_id,identity)).fetchone()[0]
                delay=max(retry_delay(count),delivery.retry_after or 0)
                state=delivery.status
                if state == 'retry' and count >= config.max_processing_attempts:
                    state='dead'
                stamp=utc_timestamp()
                conn.execute('UPDATE notification_outbox SET status=?,error_code=?,provider_message_id=?,next_retry_at=?,updated_at=? WHERE account_id=? AND email_id=?',
                    (state,delivery.code,delivery.message_id,time.time()+delay,stamp,context.account_id,identity))
                conn.execute('INSERT INTO notification_attempts(account_id,email_id,status,attempt_count,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                    (context.account_id,identity,'sent' if state == 'sent' else 'unknown' if state == 'unknown' else 'failed',count,stamp,stamp))
                attempt(conn,context,identity,'notify',state,delivery.code)
                if state != 'sent':
                    task_retry(conn,context,task,'notify',delivery.code or 'delivery_failed',retryable=state in ('retry','blocked'),delay=delay,max_attempts=config.max_processing_attempts)
                    return False
                stage='mark_read'
                conn.execute("UPDATE processing_tasks SET stage='mark_read',attempt_count=0,error_code=NULL WHERE account_id=? AND email_id=?",(context.account_id,identity))
    if stage == 'mark_read':
        if not config.local_only and config.auto_mark_read and task['read_required']:
            try:
                with cycle_external(manager,context,token):
                    success=marker(service,identity)
            except WorkCancelled:
                raise
            except Exception:
                success=False
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                if not success:
                    task_retry(conn,context,task,'mark_read','mark_read_failed',max_attempts=config.max_processing_attempts)
                    return False
                attempt(conn,context,identity,'mark_read','complete')
        with manager.guard(context) as conn:
            fence(manager,context,token,conn)
            conn.execute("UPDATE processing_tasks SET status='complete',stage='complete',owner_token=NULL,error_code=NULL,updated_at=? WHERE account_id=? AND email_id=?",(utc_timestamp(),context.account_id,identity))
            conn.execute("UPDATE email_logs SET processing_state='complete' WHERE account_id=? AND email_id=?",(context.account_id,identity))
        return True
    return False

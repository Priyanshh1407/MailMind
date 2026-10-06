"""Resume each saved message at its first unfinished step."""
from contextlib import contextmanager
from threading import Event
import time
from .account_state import WorkCancelled
from .database import utc_timestamp
from .prediction import Prediction
from .provider_policy import provider_failure, retry_delay, TELEGRAM_CALLS
from .notifier import Delivery
from .logging_utils import log_event
from .email_text import preview_text
from .privacy import provider_email_text
from .email_analysis import ensure_prediction_analysis, save_analysis_result
from .action_center import persist_analysis_actions
from .intelligence_contract import TokenOperation
from .local_llm import predict_with_token_count
from .token_usage import TokenRecorder, usage_request_prefix
from .work_queue import fence, attempt, task_retry
from .classification_service import run_local


# Every route has already been tried when one of these is returned (see
# llm_api.classify_email); retrying the same email later cannot help.
PERMANENT_CLASSIFICATION_FAILURES = frozenset({
    'invalid_provider_output', 'provider_auth', 'provider_invalid_request',
})


@contextmanager
def cycle_external(manager, context, token):
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
    yield
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)


def process_task(task, manager, context, token, service, model, collection_provider,
                 *, classifier, shadow, notifier, marker, logger, validator,
                 job_id=None):
    identity=task['email_id']
    config=manager.settings
    intelligence_backfill=bool(task.get('intelligence_backfill'))
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
        claimed=conn.execute("UPDATE processing_tasks SET status='running',owner_token=?,updated_at=? WHERE account_id=? AND email_id=? AND status IN ('queued','retry') AND next_retry_at<=?",(token,utc_timestamp(),context.account_id,identity,time.time()))
        if not claimed.rowcount:
            return False
        if intelligence_backfill:
            conn.execute(
                """UPDATE intelligence_backfill_items
                   SET status='running',error_code=NULL,updated_at=?
                   WHERE account_id=? AND email_id=?""",
                (utc_timestamp(),context.account_id,identity))
    stage=task['stage']
    usage_prefix=usage_request_prefix(
        'worker-classification', context.account_id, identity,
        task['attempt_count'],
    )
    usage_recorder=TokenRecorder(
        context.account_id, usage_prefix, email_id=identity, job_id=job_id,
        db_path=config.db_path, enabled=config.token_collection_enabled,
    )
    if stage == 'classify':
        try:
            guard=lambda:cycle_external(manager,context,token)
            if config.local_only:
                decision=local=run_local(
                    lambda:predict_with_token_count(
                        model, task['subject'], task['body'],
                        sender=task['sender'], account_id=context.account_id),
                    model=model,subject=task['subject'],body=task['body'],
                    timeout=config.classification_budget_seconds,
                    recorder=usage_recorder,key='local-primary',
                    operation=TokenOperation.CLASSIFICATION_ANALYSIS.value,
                    guard=guard)
            else:
                with cycle_external(manager,context,token):
                    decision=classifier(
                        task['sender'],task['subject'],task['body'],
                        account_id=context.account_id,
                        collection_provider=collection_provider,
                        validator=validator,settings=config,
                        source_timestamp=task['email_created_at'],
                        before_request=guard,
                        usage_recorder=usage_recorder,
                    )
                decision=ensure_prediction_analysis(decision)
                if not config.shadow_active:
                    # The comparison-only shadow is switched off (the default).
                    local=ensure_prediction_analysis(Prediction(
                        outcome='UNAVAILABLE',reason='shadow_disabled'))
                else:
                    try:
                        local=run_local(
                            lambda:shadow(
                                task['sender'],task['subject'],task['body'],
                                decision,account_id=context.account_id,model=model),
                            model=model,subject=task['subject'],body=task['body'],
                            timeout=20,recorder=usage_recorder,key='local-shadow',
                            operation=TokenOperation.LOCAL_SHADOW.value,
                            guard=guard)
                    except WorkCancelled:
                        raise
                    except Exception:
                        # The shadow is evaluation only; it can never fail the task.
                        local=ensure_prediction_analysis(Prediction(
                            outcome='ERROR',reason='shadow_inference_failed'))
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                logger(identity,task['sender'],task['subject'],task['body'],decision,local,
                       account_id=context.account_id,db_conn=conn)
                attempt(conn,context,identity,'classify',decision.outcome,decision.reason)
                try:
                    save_analysis_result(
                        context.account_id, identity, decision.analysis,
                        db_conn=conn,
                    )
                    attempt(conn,context,identity,'analysis','complete')
                except Exception as analysis_error:
                    # Classification is authoritative. The prediction metadata
                    # keeps the bounded analysis for a later owned reconciliation.
                    log_event('analysis_persistence_deferred',error=analysis_error)
                    attempt(
                        conn,context,identity,'analysis','retry',
                        'analysis_persistence_failed',
                    )
                if (config.action_extraction_enabled
                        and decision.outcome == 'CLASSIFIED'):
                    conn.execute('SAVEPOINT phase4_actions')
                    try:
                        persist_analysis_actions(
                            context.account_id,identity,decision.analysis,
                            source_created_at=task['email_created_at'],
                            source_text=provider_email_text(
                                task['subject'],task['body']),
                            timezone_name=config.default_timezone,
                            reminders_enabled=(
                                config.action_reminders_enabled
                                and not intelligence_backfill),
                            db_conn=conn,
                        )
                        attempt(
                            conn,context,identity,'actions','complete')
                        conn.execute('RELEASE SAVEPOINT phase4_actions')
                    except Exception as action_error:
                        conn.execute(
                            'ROLLBACK TO SAVEPOINT phase4_actions')
                        conn.execute('RELEASE SAVEPOINT phase4_actions')
                        log_event(
                            'action_persistence_deferred',
                            error=action_error)
                        attempt(
                            conn,context,identity,'actions','retry',
                            'action_persistence_failed',
                        )
                if decision.outcome != 'CLASSIFIED':
                    code=decision.reason or 'classification_unavailable'
                    retryable=code not in PERMANENT_CLASSIFICATION_FAILURES
                    task_retry(conn,context,task,'classify',code,retryable=retryable)
                    return False
                if intelligence_backfill:
                    stamp=utc_timestamp()
                    conn.execute(
                        """UPDATE processing_tasks
                           SET status='complete',stage='complete',category=?,
                               attempt_count=0,owner_token=NULL,error_code=NULL,
                               updated_at=?
                           WHERE account_id=? AND email_id=?""",
                        (decision.category,stamp,context.account_id,identity))
                    conn.execute(
                        """UPDATE email_logs SET processing_state='complete'
                           WHERE account_id=? AND email_id=?""",
                        (context.account_id,identity))
                    conn.execute(
                        """UPDATE intelligence_backfill_items
                           SET status='complete',error_code=NULL,updated_at=?
                           WHERE account_id=? AND email_id=?""",
                        (stamp,context.account_id,identity))
                    attempt(
                        conn,context,identity,'backfill','complete')
                    return True
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
                task_retry(conn,context,task,'classify',failure.code,retryable=failure.retryable)
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
                    task_retry(conn,context,task,'notify',code,retryable=False)
                    return False
        with manager.guard(context) as conn:
            fence(manager,context,token,conn)
            outbox=conn.execute('SELECT * FROM notification_outbox WHERE account_id=? AND email_id=?',(context.account_id,identity)).fetchone()
            if outbox['status'] == 'sent':
                stage='mark_read'
            elif outbox['status'] in ('unknown','sending'):
                task_retry(conn,context,task,'notify','delivery_unknown',retryable=False)
                return False
            elif outbox['next_retry_at'] > time.time():
                conn.execute("UPDATE processing_tasks SET status='retry',next_retry_at=?,owner_token=NULL WHERE account_id=? AND email_id=?",(outbox['next_retry_at'],context.account_id,identity))
                return False
            else:
                # Commit 'sending' BEFORE contacting Telegram. A crash is ambiguous.
                conn.execute("UPDATE notification_outbox SET status='sending',attempt_count=attempt_count+1,updated_at=? WHERE account_id=? AND email_id=?",(utc_timestamp(),context.account_id,identity))
        if stage == 'notify':
            from .notifier import telegram_timeouts
            request_timeout,wait_seconds=telegram_timeouts(config.provider_timeout_seconds)
            # Set only when the send is handed to Telegram. A timeout before that
            # (busy pool, lease check) sent nothing, so it is a plain retry.
            attempted=Event()
            try:
                with cycle_external(manager,context,token):
                    def send():
                        with cycle_external(manager,context,token):
                            from .privacy import redact
                        from .notifier import send_telegram_alert
                        attempted.set()
                        return notifier('[SENDER]',redact(task['subject']),preview_text(redact(task['body']))[:100]+'...',timeout=request_timeout,**({'settings':config} if notifier is send_telegram_alert else {}))
                    delivery=TELEGRAM_CALLS.run(send,wait_seconds)
                # Explicit compatibility for injected old boolean test adapters.
                if isinstance(delivery,bool):
                    delivery=Delivery('sent' if delivery else 'retry','delivery_failed' if not delivery else None)
            except WorkCancelled:
                raise
            except Exception as error:
                failure=provider_failure(error)
                if not attempted.is_set():
                    delivery=Delivery('retry','notification_not_attempted',retry_after=5)
                else:
                    delivery=Delivery('unknown' if failure.ambiguous else 'retry' if failure.retryable else 'blocked',failure.code)
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                count=conn.execute('SELECT attempt_count FROM notification_outbox WHERE account_id=? AND email_id=?',(context.account_id,identity)).fetchone()[0]
                delay=max(retry_delay(count),delivery.retry_after or 0)
                # A temporary failure keeps retrying; 'unknown' (maybe sent) and
                # 'blocked' (rejected) stop so an alert is never duplicated.
                state=delivery.status
                stamp=utc_timestamp()
                conn.execute('UPDATE notification_outbox SET status=?,error_code=?,provider_message_id=?,next_retry_at=?,updated_at=? WHERE account_id=? AND email_id=?',
                    (state,delivery.code,delivery.message_id,time.time()+delay,stamp,context.account_id,identity))
                conn.execute('INSERT INTO notification_attempts(account_id,email_id,status,attempt_count,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                    (context.account_id,identity,'sent' if state == 'sent' else 'unknown' if state == 'unknown' else 'failed',count,stamp,stamp))
                attempt(conn,context,identity,'notify',state,delivery.code)
                if state != 'sent':
                    task_retry(conn,context,task,'notify',delivery.code or 'delivery_failed',retryable=state=='retry',delay=delay)
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
                    task_retry(conn,context,task,'mark_read','mark_read_failed')
                    return False
                attempt(conn,context,identity,'mark_read','complete')
        with manager.guard(context) as conn:
            fence(manager,context,token,conn)
            conn.execute("UPDATE processing_tasks SET status='complete',stage='complete',owner_token=NULL,error_code=NULL,updated_at=? WHERE account_id=? AND email_id=?",(utc_timestamp(),context.account_id,identity))
            conn.execute("UPDATE email_logs SET processing_state='complete' WHERE account_id=? AND email_id=?",(context.account_id,identity))
        return True
    return False

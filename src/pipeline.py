"""Resume each saved message at its first unfinished step."""
from contextlib import contextmanager
from threading import Event
import time
from .account_state import WorkCancelled
from .database import utc_timestamp
from .prediction import Prediction
from .provider_policy import provider_failure, retry_delay, TELEGRAM_CALLS, LOCAL_CALLS
from .notifier import Delivery
from .logging_utils import log_event
from .email_text import format_email_text, preview_text
from .privacy import provider_email_text
from .email_analysis import ensure_prediction_analysis, save_analysis_result
from .action_center import persist_analysis_actions
from .intelligence_contract import TokenOperation, TokenOutcome
from .local_llm import local_retrieval_expected, predict_with_token_count
from .token_usage import (
    EMBEDDING_MODEL_VERSION,
    TokenRecorder,
    estimate_text_tokens,
    tokenizer_usage_measurement,
    unavailable_measurement,
    usage_request_prefix,
)
from .work_queue import fence, attempt, task_retry


@contextmanager
def cycle_external(manager, context, token):
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)
    yield
    with manager.guard(context) as conn:
        fence(manager,context,token,conn)


def _local_outcome(prediction):
    if prediction.outcome in ('CLASSIFIED', 'ABSTAIN'):
        return TokenOutcome.SUCCESS.value
    if prediction.reason in (
        'local_inference_timeout', 'local_inference_budget_exhausted'
    ):
        return TokenOutcome.TIMEOUT.value
    return TokenOutcome.FAILED.value


def _record_local(recorder, attempt_key, prediction, input_tokens, operation):
    recorder.record(
        attempt_key,
        provider='local',
        model_version=prediction.model_version or 'local-unavailable',
        operation=operation,
        outcome=_local_outcome(prediction),
        measurement=(tokenizer_usage_measurement(input_tokens)
                     if input_tokens is not None
                     else unavailable_measurement()),
    )


def _record_local_retrieval(recorder, attempt_key, prediction,
                            measurement, outcome=None):
    recorder.record(
        attempt_key,
        provider='embedding',
        model_version=EMBEDDING_MODEL_VERSION,
        operation=TokenOperation.QUERY_EMBEDDING.value,
        outcome=(outcome or (
            TokenOutcome.FAILED.value
            if prediction.retrieval_status == 'unavailable'
            else TokenOutcome.SUCCESS.value
        )),
        measurement=measurement,
    )


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
            if config.local_only:
                local_attempted=Event()
                local_query=local_retrieval_expected(model)
                local_query_measurement=estimate_text_tokens(
                    format_email_text(task['subject'],task['body']))
                try:
                    with cycle_external(manager,context,token):
                        def run_local():
                            local_attempted.set()
                            return predict_with_token_count(
                                model, task['subject'], task['body'],
                                sender=task['sender'],
                                account_id=context.account_id,
                            )
                        decision,input_tokens=LOCAL_CALLS.run(
                            run_local, config.classification_budget_seconds)
                    decision=ensure_prediction_analysis(decision)
                    _record_local(
                        usage_recorder, 'local-primary', decision, input_tokens,
                        TokenOperation.CLASSIFICATION_ANALYSIS.value,
                    )
                    if local_query:
                        _record_local_retrieval(
                            usage_recorder, 'local-primary-retrieval',
                            decision, local_query_measurement,
                        )
                    local=decision
                except WorkCancelled:
                    if local_attempted.is_set():
                        usage_recorder.record(
                            'local-primary', provider='local',
                            model_version=getattr(model, 'model_version', None)
                            or 'local-unavailable',
                            operation=TokenOperation.CLASSIFICATION_ANALYSIS.value,
                            outcome=TokenOutcome.CANCELLED.value,
                            measurement=unavailable_measurement(),
                        )
                        if local_query:
                            _record_local_retrieval(
                                usage_recorder, 'local-primary-retrieval',
                                Prediction(), local_query_measurement,
                                TokenOutcome.CANCELLED.value,
                            )
                    raise
                except TimeoutError:
                    if local_attempted.is_set():
                        usage_recorder.record(
                            'local-primary', provider='local',
                            model_version=getattr(model, 'model_version', None)
                            or 'local-unavailable',
                            operation=TokenOperation.CLASSIFICATION_ANALYSIS.value,
                            outcome=TokenOutcome.TIMEOUT.value,
                            measurement=unavailable_measurement(),
                        )
                        if local_query:
                            _record_local_retrieval(
                                usage_recorder, 'local-primary-retrieval',
                                Prediction(), local_query_measurement,
                                TokenOutcome.TIMEOUT.value,
                            )
                    raise
            else:
                with cycle_external(manager,context,token):
                    decision=classifier(
                        task['sender'],task['subject'],task['body'],
                        account_id=context.account_id,
                        collection_provider=collection_provider,
                        validator=validator,settings=config,
                        source_timestamp=task['email_created_at'],
                        before_request=lambda:cycle_external(manager,context,token),
                        usage_recorder=usage_recorder,
                    )
                decision=ensure_prediction_analysis(decision)
                shadow_attempted=Event()
                shadow_query=local_retrieval_expected(model)
                shadow_query_measurement=estimate_text_tokens(
                    format_email_text(task['subject'],task['body']))
                try:
                    with cycle_external(manager,context,token):
                        def run_shadow():
                            shadow_attempted.set()
                            return shadow(
                                task['sender'],task['subject'],task['body'],
                                decision,account_id=context.account_id,model=model)
                        local,input_tokens=LOCAL_CALLS.run(run_shadow,20)
                    local=ensure_prediction_analysis(local)
                    _record_local(
                        usage_recorder, 'local-shadow', local, input_tokens,
                        TokenOperation.LOCAL_SHADOW.value,
                    )
                    if shadow_query:
                        _record_local_retrieval(
                            usage_recorder, 'local-shadow-retrieval',
                            local, shadow_query_measurement,
                        )
                except WorkCancelled:
                    if shadow_attempted.is_set():
                        usage_recorder.record(
                            'local-shadow', provider='local',
                            model_version=getattr(model, 'model_version', None)
                            or 'local-unavailable',
                            operation=TokenOperation.LOCAL_SHADOW.value,
                            outcome=TokenOutcome.CANCELLED.value,
                            measurement=unavailable_measurement(),
                        )
                        if shadow_query:
                            _record_local_retrieval(
                                usage_recorder, 'local-shadow-retrieval',
                                Prediction(), shadow_query_measurement,
                                TokenOutcome.CANCELLED.value,
                            )
                    raise
                except Exception as error:
                    if shadow_attempted.is_set():
                        usage_recorder.record(
                            'local-shadow', provider='local',
                            model_version=getattr(model, 'model_version', None)
                            or 'local-unavailable',
                            operation=TokenOperation.LOCAL_SHADOW.value,
                            outcome=(TokenOutcome.TIMEOUT.value
                                     if isinstance(error, TimeoutError)
                                     else TokenOutcome.FAILED.value),
                            measurement=unavailable_measurement(),
                        )
                        if shadow_query:
                            _record_local_retrieval(
                                usage_recorder, 'local-shadow-retrieval',
                                Prediction(), shadow_query_measurement,
                                (TokenOutcome.TIMEOUT.value
                                 if isinstance(error, TimeoutError)
                                 else TokenOutcome.FAILED.value),
                            )
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
                    retryable=code not in ('invalid_provider_output','provider_auth','provider_invalid_request')
                    task_retry(conn,context,task,'classify',code,retryable=retryable,max_attempts=config.max_processing_attempts)
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
                    delivery=TELEGRAM_CALLS.run(send,config.provider_timeout_seconds)
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

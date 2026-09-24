"""Paused by default; durable steps replace Gmail unread status as the queue."""
import time
import schedule
from .email_client import (refresh_gmail, get_unread_emails, get_new_emails,
                           get_gmail_history_id, mark_as_read, FetchBatch)
from .llm_api import classify_email
from .notifier import send_telegram_alert
from .db_utils import (log_email_to_db, get_ingestion_state, save_ingestion_state,
                       ensure_ingestion_state, save_live_ingestion_state)
from .config import Settings
from .setup_db import create_database
from .logging_utils import log_event
from .account_state import AccountManager, WorkCancelled
from .local_llm import DeferredMailMindModel, MailMindModel
from .retrieval_policy import load_policy
from .vector_db import create_vector_collection, create_search_collection, VectorService
from .feedback import reconcile_feedback, current_vector
from .work_queue import claim_cycle, fence, finish_cycle, ingest_email, newest_due_tasks, excluded_messages, record_ingestion_failure
from .pipeline import process_task, cycle_external
from .provider_policy import BoundedCalls
from .database import utc_timestamp

_model = None
_model_paths = None
_collection = None
_collection_path = None
_FEEDBACK_RECONCILIATION_CALLS = BoundedCalls(1)


def shadow_evaluate_email(sender, subject, body, external_prediction, *, account_id=None, model=None):
    return model.predict(subject, body, sender=sender, account_id=account_id), None


def _run_agent(*, settings=None, manager=None, model=None, collection=None, stop_event=None,
               task_limit=None, newest_first=False, search_collection=None):
    config=settings or Settings.from_environment(load_file=True)
    create_database(config.db_path)
    manager=manager or AccountManager(config)
    context=manager.worker_context()
    if context is None:
        return {'status':'paused'}
    ownership=claim_cycle(manager,context)
    if ownership is None:
        return {'status':'busy'}
    token,job_id=ownership
    error_code=None
    global _model,_model_paths,_collection,_collection_path
    try:
        def collection_provider():
            global _collection,_collection_path
            if collection is not None:
                return collection
            if _collection is None or _collection_path != (config.data_dir,config.local_only,config.asset_manifest_path):
                _collection=create_vector_collection(config.data_dir/'chroma_db',settings=config)
                _collection_path=(config.data_dir,config.local_only,config.asset_manifest_path)
            return _collection
        # SQLite is authoritative for feedback. Keep the retryable derived vector
        # index on its own bounded worker so a stuck Chroma write cannot stop
        # Gmail ingestion or durable task processing.
        try:
            _FEEDBACK_RECONCILIATION_CALLS.run(
                lambda:reconcile_feedback(manager,context,collection_provider,
                    max_attempts=config.max_processing_attempts),
                min(config.provider_timeout_seconds,2),
            )
        except WorkCancelled:
            raise
        except Exception as error:
            log_event('feedback_reconciliation_deferred',error=error)
        # Semantic indexing runs in its own owned process. Native embedding or
        # Chroma startup can never delay Gmail sync or task processing here.
        service=None
        if not config.local_only:
            try:
                with cycle_external(manager,context,token):
                    service=refresh_gmail(manager,context)
            except WorkCancelled:
                raise
            except TimeoutError:
                service=None
            if service is None:
                error_code='gmail_unavailable'
                with manager.guard(context) as conn:
                    conn.execute('UPDATE worker_health SET last_error_at=?,last_error_code=? WHERE account_id=?',(utc_timestamp(),error_code,context.account_id))
                manager.pause(context)
                return {'status':'paused','job_id':job_id}
            guard=lambda:cycle_external(manager,context,token)
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                state=ensure_ingestion_state(context.account_id,conn,batch_limit=config.max_pending_tasks)
                excluded=excluded_messages(conn,context.account_id)
                pending_count=conn.execute(
                    "SELECT COUNT(*) FROM processing_tasks WHERE account_id=? AND status IN ('queued','retry','running')",
                    (context.account_id,)).fetchone()[0]
            capacity=max(0,config.max_pending_tasks-pending_count)
            history_id=state['history_id']
            history_bootstrap=not bool(state['history_bootstrap_complete'])
            if history_id is None:
                history_id=get_gmail_history_id(service,before_request=guard,
                    budget_seconds=config.provider_timeout_seconds*2)
                if history_id is not None:
                    with manager.guard(context) as conn:
                        fence(manager,context,token,conn)
                        conn.execute('UPDATE ingestion_state SET history_id=?,last_live_sync_at=? WHERE account_id=?',
                            (history_id,utc_timestamp(),context.account_id))
            live=FetchBatch(history_id=history_id)
            if capacity and history_id is not None:
                if history_bootstrap:
                    # The cursor is a boundary for future events. First catch up
                    # the newest unread page so messages that arrived before the
                    # cursor was initialized are not stranded behind a paused
                    # historical backlog. The next history sync safely dedupes
                    # anything that arrived during this bootstrap window.
                    live=get_unread_emails(service,max_results=min(config.batch_size,capacity),
                        page_size=min(config.gmail_page_size,capacity),
                        max_pages=config.gmail_max_pages,page_token=None,
                        before_request=guard,exclude_ids=excluded,
                        budget_seconds=config.provider_timeout_seconds*2)
                    live.history_id=history_id
                else:
                    live=get_new_emails(service,history_id,max_results=min(config.batch_size,capacity),
                        max_pages=config.gmail_max_pages,before_request=guard,exclude_ids=excluded,
                        budget_seconds=config.provider_timeout_seconds*2)
                    if live.history_expired:
                        replacement=get_gmail_history_id(service,before_request=guard,
                            budget_seconds=config.provider_timeout_seconds*2)
                        live=get_unread_emails(service,max_results=min(config.batch_size,capacity),
                            page_size=min(config.gmail_page_size,capacity),max_pages=1,page_token=None,
                            before_request=guard,exclude_ids=excluded,
                            budget_seconds=config.provider_timeout_seconds*2)
                        live.history_id=replacement
                for email in live.emails:
                    ingest_email(email,manager,context,token,source='live')
                for failure in live.failures:
                    record_ingestion_failure(failure,manager,context,token)
                excluded.update(email['id'] for email in live.emails)
                pending_count+=len(live.emails)
                capacity=max(0,config.max_pending_tasks-pending_count)
                with manager.guard(context) as conn:
                    fence(manager,context,token,conn)
                    save_live_ingestion_state(context.account_id,live,conn,
                        history_id=live.history_id,error_code='history_cursor_expired' if live.history_expired else None)
                    if history_bootstrap and not live.listing_error:
                        conn.execute('UPDATE ingestion_state SET history_bootstrap_complete=1 WHERE account_id=?',
                                     (context.account_id,))
                    if live.emails:
                        # New arrivals change Gmail page offsets. Restart backlog
                        # scanning; durable message IDs prevent duplicate work.
                        conn.execute('UPDATE ingestion_state SET page_token=NULL WHERE account_id=?',(context.account_id,))
            elif history_id is not None and not capacity:
                live=FetchBatch(deferred=True,history_id=history_id)
                with manager.guard(context) as conn:
                    fence(manager,context,token,conn)
                    save_live_ingestion_state(context.account_id,live,conn,history_id=history_id)
            elif capacity and state['initial_batch_complete']:
                # Compatibility fallback for test adapters or Gmail accounts
                # where a history cursor could not be established.
                live=get_unread_emails(service,max_results=min(config.gmail_page_size,capacity),
                    page_size=min(config.gmail_page_size,capacity),max_pages=1,page_token=None,
                    before_request=guard,exclude_ids=excluded,
                    budget_seconds=config.provider_timeout_seconds*2)
                for email in live.emails:
                    ingest_email(email,manager,context,token,source='live')
                for failure in live.failures:
                    record_ingestion_failure(failure,manager,context,token)
                excluded.update(email['id'] for email in live.emails)
                pending_count+=len(live.emails)
                capacity=max(0,config.max_pending_tasks-pending_count)
                with manager.guard(context) as conn:
                    fence(manager,context,token,conn)
                    save_live_ingestion_state(context.account_id,live,conn)
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                state=get_ingestion_state(context.account_id,conn)
            batch=FetchBatch(has_more=bool(state['has_more']),next_page_token=state['page_token'],
                             deferred=not bool(state['backlog_authorized']))
            if state['backlog_authorized'] and state['backlog_remaining'] and capacity:
                fetch_limit=min(config.batch_size,state['backlog_remaining'],capacity)
                batch=get_unread_emails(service,max_results=fetch_limit,
                    page_size=min(config.gmail_page_size,fetch_limit),max_pages=config.gmail_max_pages,
                    page_token=state['page_token'],before_request=guard,exclude_ids=excluded,
                    budget_seconds=config.provider_timeout_seconds*2)
                for email in batch.emails:
                    ingest_email(email,manager,context,token,source='backlog')
                for failure in batch.failures:
                    record_ingestion_failure(failure,manager,context,token)
                remaining=max(0,state['backlog_remaining']-len(batch.emails))
                authorized=bool(remaining and batch.has_more)
                with manager.guard(context) as conn:
                    fence(manager,context,token,conn)
                    save_ingestion_state(context.account_id,batch,conn)
                    conn.execute("""UPDATE ingestion_state SET backlog_remaining=?,
                        backlog_authorized=?,initial_batch_complete=CASE WHEN ? THEN initial_batch_complete ELSE 1 END,
                        status=CASE WHEN ? AND has_more THEN 'deferred' ELSE status END
                        WHERE account_id=?""",
                        (remaining,int(authorized),int(authorized),int(not authorized),context.account_id))
            elif state['backlog_authorized'] and not capacity:
                with manager.guard(context) as conn:
                    fence(manager,context,token,conn)
                    conn.execute("UPDATE ingestion_state SET status='deferred' WHERE account_id=?",(context.account_id,))
            cycle_batch=FetchBatch(
                emails=live.emails+batch.emails,
                failures=live.failures+batch.failures,
                listing_error=live.listing_error or batch.listing_error,
                pages=live.pages+batch.pages,scanned=live.scanned+batch.scanned,
                has_more=live.has_more or batch.has_more,
                deferred=live.deferred or batch.deferred,
            )
        if task_limit is not None and (type(task_limit) is not int or not 1 <= task_limit <= config.batch_size):
            raise ValueError('Invalid task limit')
        tasks=newest_due_tasks(manager,context,token,task_limit or config.batch_size)
        if tasks and model is None:
            shadow_path = config.model_path if config.local_only else config.shadow_model_path
            paths=(shadow_path,config.data_dir,config.retrieval_policy_path,config.local_only,config.asset_manifest_path)
            with cycle_external(manager,context,token):
                if _model is None or _model_paths != paths:
                    if config.local_only:
                        _model=MailMindModel(config.model_path,settings=config,vector_service=VectorService(collection_provider,lambda metadata:current_vector(metadata,config.db_path),policy=load_policy(config.retrieval_policy_path)))
                    else:
                        _model=DeferredMailMindModel(shadow_path,settings=config)
                    _model_paths=paths
                model=_model
        completed=0
        def cloud_collection_provider():
            if _collection is None:
                raise RuntimeError('retrieval_not_initialized')
            return _collection
        for task in tasks:
            if stop_event is not None and stop_event.is_set():break
            completed+=bool(process_task(task,manager,context,token,service,model,cloud_collection_provider,
                classifier=classify_email,shadow=shadow_evaluate_email,notifier=send_telegram_alert,
                marker=mark_as_read,logger=log_email_to_db,validator=lambda metadata:current_vector(metadata,config.db_path)))
        if completed < len(tasks):
            error_code='processing_incomplete'
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                conn.execute('UPDATE ingestion_state SET page_token=NULL WHERE account_id=?',(context.account_id,))
        if not config.local_only and cycle_batch.listing_error:
            error_code='gmail_listing_failed'
        return {**({'status':'local_only','network':'disabled'} if config.local_only else cycle_batch.summary()),'processed_count':completed,'attempted_count':len(tasks),'job_id':job_id,
                'processing_status':'partial' if error_code else 'complete'}
    except WorkCancelled:
        log_event('agent_cycle_cancelled')
        return {'status':'cancelled','job_id':job_id}
    except Exception:
        error_code='cycle_failed'
        raise
    finally:
        try:
            finish_cycle(manager,context,token,job_id,error_code)
        except WorkCancelled:
            pass


def run_agent():
    try:
        return _run_agent()
    except Exception as error:
        log_event('agent_cycle_failed',error=error)
        raise RuntimeError('Email processing failed') from None


def scheduled_cycle():
    try:
        run_agent()
    except RuntimeError:
        pass


if __name__ == '__main__':
    print('MailMind worker: paused until Google is connected in the dashboard.')
    scheduled_cycle()
    config=Settings.from_environment(load_file=True)
    schedule.every(config.poll_interval_seconds).seconds.do(scheduled_cycle)
    while True:
        schedule.run_pending()
        time.sleep(1)

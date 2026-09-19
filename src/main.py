"""Paused by default; durable steps replace Gmail unread status as the queue."""
import time
import schedule
from .email_client import refresh_gmail, get_unread_emails, mark_as_read
from .llm_api import classify_email
from .notifier import send_telegram_alert
from .db_utils import log_email_to_db, get_ingestion_state, save_ingestion_state
from .config import Settings
from .setup_db import create_database
from .logging_utils import log_event
from .account_state import AccountManager, WorkCancelled
from .local_llm import DeferredMailMindModel, MailMindModel
from .retrieval_policy import load_policy
from .vector_db import create_vector_collection, VectorService
from .feedback import reconcile_feedback, current_vector
from .work_queue import claim_cycle, fence, finish_cycle, ingest_email, due_tasks, excluded_messages, record_ingestion_failure
from .pipeline import process_task, cycle_external
from .provider_policy import PROVIDER_CALLS

_model = None
_model_paths = None
_collection = None
_collection_path = None


def shadow_evaluate_email(subject, body, external_prediction, *, account_id=None, model=None):
    return model.predict(subject,body,account_id=account_id),None


def _run_agent(*, settings=None, manager=None, model=None, collection=None, stop_event=None):
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
        # A degraded derived vector index must not starve the durable mail queue.
        # Interactive reconciliation remains available without this automatic cap.
        reconcile_feedback(manager,context,collection_provider,max_attempts=config.max_processing_attempts)
        service=None
        if not config.local_only:
            try:
                with cycle_external(manager,context,token):
                    service=PROVIDER_CALLS.run(lambda:refresh_gmail(manager,context),config.provider_timeout_seconds)
            except WorkCancelled:
                raise
            except TimeoutError:
                service=None
            if service is None:
                error_code='gmail_unavailable'
                with manager.guard(context) as conn:
                    from .database import utc_timestamp
                    conn.execute('UPDATE worker_health SET last_error_at=?,last_error_code=? WHERE account_id=?',(utc_timestamp(),error_code,context.account_id))
                manager.pause(context)
                return {'status':'paused','job_id':job_id}
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                previous=get_ingestion_state(context.account_id,conn)
                excluded=excluded_messages(conn,context.account_id)
            batch=get_unread_emails(service,max_results=config.batch_size,page_size=config.gmail_page_size,
                max_pages=config.gmail_max_pages,page_token=previous['page_token'] if previous else None,
                before_request=lambda:cycle_external(manager,context,token),exclude_ids=excluded)
            # Save bodies BEFORE advancing the listing cursor.
            for email in batch.emails:
                ingest_email(email,manager,context,token)
            for failure in batch.failures:
                record_ingestion_failure(failure,manager,context,token)
            with manager.guard(context) as conn:
                fence(manager,context,token,conn)
                save_ingestion_state(context.account_id,batch,conn)
        tasks=due_tasks(manager,context,token,config.batch_size)
        if tasks and model is None:
            paths=(config.model_path,config.data_dir,config.retrieval_policy_path,config.local_only,config.asset_manifest_path)
            with cycle_external(manager,context,token):
                if _model is None or _model_paths != paths:
                    if config.local_only:
                        _model=MailMindModel(config.model_path,settings=config,vector_service=VectorService(collection_provider,lambda metadata:current_vector(metadata,config.db_path),policy=load_policy(config.retrieval_policy_path)))
                    else:
                        _model=DeferredMailMindModel(config.model_path,settings=config)
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
        if not config.local_only and batch.listing_error:
            error_code='gmail_listing_failed'
        return {**({'status':'local_only','network':'disabled'} if config.local_only else batch.summary()),'processed_count':completed,'attempted_count':len(tasks),'job_id':job_id,
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

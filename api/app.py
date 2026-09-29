"""Local API: open a loopback session, then explicitly connect one Google account."""
from contextlib import asynccontextmanager
import os
import json
import math
import secrets
import sqlite3
import time
from threading import Event, Lock
from datetime import datetime, timezone
from src.background_jobs import BackgroundJobs
from src.database import utc_timestamp
from src.work_queue import enqueue_cycle
from src.provider_policy import LOCAL_CALLS, SEARCH_CALLS
from typing import Literal
from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from src.config import Settings
from src.database import initialize_database
from src.account_state import AccountManager, AccessDenied, WorkCancelled, TransitionBusy
from src.db_utils import (get_recent_emails, get_email, log_email_to_db, update_human_label,
                          get_ingestion_state, ensure_ingestion_state, count_emails,
                          dashboard_totals, withdraw_human_label, lexical_search_ids,
                          filter_ranked_ids)
from src.local_llm import (DeferredMailMindModel, MailMindModel,
                           local_retrieval_expected, predict_with_token_count)
from src.llm_api import classify_email
from src.logging_utils import log_event
from src.email_text import format_email_text
from src import vector_db
from src.prediction import Category, Prediction
from src.email_analysis import ensure_prediction_analysis, save_analysis_result
from src.retrieval_policy import load_policy
from src.feedback import reconcile_feedback, current_vector
from src.email_search import (MIN_SEMANTIC_QUERY, SEARCH_QUERY_TIMEOUT_SECONDS,
                              semantic_candidates, hybrid_rank)
from src.vector_lock import vector_write_lock
from src.intelligence_contract import ActionStatus, ReminderChannel, TokenOperation, TokenOutcome
from src.token_usage import (
    EMBEDDING_MODEL_VERSION,
    TokenRecorder,
    aggregate_token_usage,
    estimate_text_tokens,
    tokenizer_usage_measurement,
    unavailable_measurement,
    usage_request_prefix,
)
from src.action_center import (
    ActionRevisionConflict,
    ActionTransitionError,
    action_summary,
    get_action,
    list_actions,
    persist_analysis_actions,
    schedule_reminder,
    update_action_status,
)

from src.intelligence_safety import (
    MutationRateLimited,
    claim_mutation_slot,
    intelligence_diagnostics,
)
from src.intelligence_backfill import (
    backfill_summary,
    queue_intelligence_backfill,
)

COOKIE = 'mailmind_session'


class EmailRequest(BaseModel):
    subject: str = Field(max_length=2000)
    body: str = Field(max_length=100000)

    @model_validator(mode='after')
    def nonempty(self):
        if not self.subject.strip() and not self.body.strip():
            raise ValueError('Enter a subject or email body')
        return self


class FeedbackRequest(BaseModel):
    email_id: str = Field(min_length=1, max_length=256)
    label: Category
    expected_revision_id: int | None = Field(default=None, ge=0)
    subject: str = Field(default='', max_length=2000)
    body: str = Field(default='', max_length=100000)


class DeliveryResolution(BaseModel):
    action: Literal['confirmed_sent','retry']


class ActionUpdateRequest(BaseModel):
    status: Literal['open','completed','dismissed']
    expected_revision: int = Field(ge=0)


class ActionSnoozeRequest(BaseModel):
    snoozed_until: datetime
    expected_revision: int = Field(ge=0)


class ActionReminderRequest(BaseModel):
    remind_at: datetime
    channel: Literal['dashboard','telegram'] = 'dashboard'
    expected_revision: int = Field(ge=0)


class ActionReanalysisRequest(BaseModel):
    expected_analysis_updated_at: str | None = Field(
        default=None, max_length=32)


class IntelligenceBackfillRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class LegacyDeleteRequest(BaseModel):
    confirmation: Literal['DELETE_UNASSIGNED_DATA']


def manual_prediction(model, subject, body, account_id, settings,
                      collection_provider, validator, classifier=classify_email,
                      usage_recorder=None,
                      usage_operation=TokenOperation.MANUAL_PREDICTION.value,
                      sender='[MANUAL]', source_timestamp=None):
    """Use cloud temporarily in normal mode while the local API model loads."""
    if not settings.local_only and getattr(model, 'load_reason', None) == 'model_loading':
        return ensure_prediction_analysis(classifier(
            sender, subject, body, account_id=account_id,
            source_timestamp=source_timestamp,
            collection_provider=collection_provider, validator=validator,
            settings=settings, usage_recorder=usage_recorder,
            usage_operation=usage_operation,
        ))
    attempted=Event()
    local_query=local_retrieval_expected(model)
    local_query_measurement=estimate_text_tokens(
        format_email_text(subject,body))
    try:
        def run_local():
            attempted.set()
            return predict_with_token_count(
                model, subject, body, account_id=account_id)
        prediction,input_tokens=LOCAL_CALLS.run(
            run_local, settings.classification_budget_seconds)
        prediction=ensure_prediction_analysis(prediction)
    except WorkCancelled:
        if attempted.is_set() and usage_recorder is not None:
            usage_recorder.record(
                'local-manual', provider='local',
                model_version=getattr(model, 'model_version', None)
                or 'local-unavailable',
                operation=usage_operation,
                outcome=TokenOutcome.CANCELLED.value,
                measurement=unavailable_measurement(),
            )
            if local_query:
                usage_recorder.record(
                    'local-manual-retrieval', provider='embedding',
                    model_version=EMBEDDING_MODEL_VERSION,
                    operation=TokenOperation.QUERY_EMBEDDING.value,
                    outcome=TokenOutcome.CANCELLED.value,
                    measurement=local_query_measurement,
                )
        raise
    except TimeoutError:
        if attempted.is_set() and usage_recorder is not None:
            usage_recorder.record(
                'local-manual', provider='local',
                model_version=getattr(model, 'model_version', None)
                or 'local-unavailable',
                operation=usage_operation,
                outcome=TokenOutcome.TIMEOUT.value,
                measurement=unavailable_measurement(),
            )
            if local_query:
                usage_recorder.record(
                    'local-manual-retrieval', provider='embedding',
                    model_version=EMBEDDING_MODEL_VERSION,
                    operation=TokenOperation.QUERY_EMBEDDING.value,
                    outcome=TokenOutcome.TIMEOUT.value,
                    measurement=local_query_measurement,
                )
        raise
    if usage_recorder is not None:
        usage_recorder.record(
            'local-manual', provider='local',
            model_version=prediction.model_version or 'local-unavailable',
            operation=usage_operation,
            outcome=(TokenOutcome.SUCCESS.value
                     if prediction.outcome in ('CLASSIFIED','ABSTAIN')
                     else TokenOutcome.FAILED.value),
            measurement=(tokenizer_usage_measurement(input_tokens)
                         if input_tokens is not None
                         else unavailable_measurement()),
        )
        if local_query:
            usage_recorder.record(
                'local-manual-retrieval', provider='embedding',
                model_version=EMBEDDING_MODEL_VERSION,
                operation=TokenOperation.QUERY_EMBEDDING.value,
                outcome=(TokenOutcome.FAILED.value
                         if prediction.retrieval_status == 'unavailable'
                         else TokenOutcome.SUCCESS.value),
                measurement=local_query_measurement,
            )
    return prediction

def create_app(*, settings=None, model_factory=MailMindModel,
               vector_factory=vector_db.create_vector_collection, search_vector_factory=None,
               oauth_factory=None):
    vector_lock, auth_lock = Lock(), Lock()

    @asynccontextmanager
    async def lifespan(application):
        try:
            config = settings or Settings.from_environment(load_file=True)
            await run_in_threadpool(initialize_database, config.db_path)
            application.state.settings = config
            application.state.accounts = AccountManager(config)
            await run_in_threadpool(application.state.accounts.restart)
            application.state.collection = None
            application.state.search_collection = None
            application.state.auth_executor = BackgroundJobs(max_workers=2)
            application.state.auth_futures = {}
            model_options = {'model_path': config.model_path,
                             'vector_service': vector_db.VectorService(collection, lambda metadata: current_vector(metadata, config.db_path),policy=load_policy(config.retrieval_policy_path))}
            if model_factory is MailMindModel:
                model_options['settings'] = config
            if model_factory is MailMindModel and not config.local_only:
                # Normal mode evaluates the trained three-class checkpoint in
                # shadow mode. The legacy binary checkpoint cannot represent
                # UPDATES and must never be advertised as the active model.
                application.state.model = DeferredMailMindModel(
                    model_path=config.shadow_model_path, settings=config)
            else:
                application.state.model = await run_in_threadpool(model_factory, **model_options)
            yield
        except Exception as error:
            log_event('api_lifecycle_failed', error=error)
            raise RuntimeError('MailMind initialization or lifecycle failed') from None
        finally:
            if hasattr(application.state,'auth_executor'):
                application.state.auth_executor.shutdown(wait=False,cancel_futures=True)
                await run_in_threadpool(application.state.accounts.restart)
            model = getattr(application.state, 'model', None)
            if hasattr(model, 'close'):
                await run_in_threadpool(model.close)
            application.state.model = None
            application.state.collection = None
            application.state.search_collection = None

    application = FastAPI(title='MailMind Local API', lifespan=lifespan)

    @application.middleware('http')
    async def safe_errors(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as error:
            log_event('api_request_failed', error=error)
            response = JSONResponse(status_code=500, content={'detail': 'An internal error occurred. Please try again.'})
        response.headers['Cache-Control'] = 'no-store'
        return response

    @application.exception_handler(AccessDenied)
    async def denied(request, error):
        return JSONResponse(status_code=401, content={'detail': 'Sign in to this local app first.'})

    @application.exception_handler(WorkCancelled)
    async def cancelled(request, error):
        return JSONResponse(status_code=409, content={'detail': 'The account changed or stopped. Refresh and try again.'})

    @application.exception_handler(TransitionBusy)
    async def busy(request, error):
        return JSONResponse(status_code=409, content={'detail': 'Finish or retry the current account action first.'})

    @application.exception_handler(sqlite3.OperationalError)
    async def storage_busy(request, error):
        log_event('storage_unavailable', error=error)
        return JSONResponse(status_code=503, content={'detail': 'Storage is busy or unavailable. This action did not finish. Please retry.'})

    def collection():
        with vector_lock:
            if application.state.collection is None:
                application.state.collection = vector_factory(application.state.settings.data_dir / 'chroma_db',**({'settings':application.state.settings} if vector_factory is vector_db.create_vector_collection else {}))
            return application.state.collection

    def search_collection():
        with vector_lock:
            if application.state.search_collection is None:
                factory = search_vector_factory or (vector_db.create_search_collection
                    if vector_factory is vector_db.create_vector_collection else vector_factory)
                options = {'settings':application.state.settings} if factory is vector_db.create_search_collection else {}
                application.state.search_collection = factory(application.state.settings.data_dir / 'search_chroma_db',**options)
            return application.state.search_collection

    def allowed_origin(request):
        if request.headers.get('origin') not in application.state.settings.frontend_origins:
            raise HTTPException(403, 'Use the local MailMind page for this action.')

    def session(request: Request):
        csrf = None
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            allowed_origin(request)
            csrf = request.headers.get('x-csrf-token', '')
        context, _ = application.state.accounts.session(request.cookies.get(COOKIE), csrf)
        return context

    @application.get('/')
    def root():
        return {'message': 'MailMind API is online.', 'model_loaded': bool(application.state.model.model_loaded)}

    @application.post('/session')
    def open_local_session(request: Request):
        # Only the configured local frontend origin can create a browser session.
        allowed_origin(request)
        token, csrf = application.state.accounts.open_session()
        response = JSONResponse({'csrf_token': csrf})
        response.set_cookie(COOKIE, token, httponly=True, samesite='strict', max_age=43200, path='/')
        return response

    @application.get('/session')
    def get_session(request: Request, context=Depends(session)):
        _, csrf = application.state.accounts.session(request.cookies.get(COOKIE))
        with application.state.accounts.guard(context, connected=False) as conn:
            state = application.state.accounts.state(conn)
            return {'csrf_token': csrf, 'email': state['account_id'], 'connected': bool(state['connected']),
                    'purge_pending': bool(state['purge_pending']), 'generation': state['generation']}

    @application.get('/emails')
    def emails(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=1000000),
               search: str = Query('', max_length=200), category: Literal['IMPORTANT','UPDATES','SPAM','NEEDS_REVIEW'] | None = None,
               email_id: str | None = Query(None, min_length=1, max_length=256),
               context=Depends(session)):
        if email_id is not None:
            with application.state.accounts.guard(context) as conn:
                exists = get_email(
                    email_id, account_id=context.account_id, db_conn=conn)
                index_counts = {
                    row['indexing_state']: row['count'] for row in conn.execute(
                        'SELECT indexing_state,COUNT(*) AS count FROM email_search_index WHERE account_id=? GROUP BY indexing_state',
                        (context.account_id,))
                }
                rows = get_recent_emails(
                    limit, account_id=context.account_id, db_conn=conn,
                    offset=offset, ranked_ids=[email_id] if exists else [],
                    include_analysis=application.state.settings.explanations_visible,
                )
            total = 1 if exists else 0
            return {
                'emails': rows, 'total': total, 'limit': limit,
                'offset': offset, 'has_more': offset + limit < total,
                'status': 'success', 'account_id': context.account_id,
                'generation': context.generation, 'search_mode': 'text',
                'semantic_available': False,
                'semantic_index': {
                    key: index_counts.get(key, 0)
                    for key in ('pending', 'indexed', 'failed')
                },
            }
        query=search.strip()
        search_mode='text'
        semantic_available=False
        with application.state.accounts.guard(context) as conn:
            lexical=lexical_search_ids(context.account_id,conn,query,category) if len(query) >= MIN_SEMANTIC_QUERY else None
            index_counts={row['indexing_state']:row['count'] for row in conn.execute(
                'SELECT indexing_state,COUNT(*) AS count FROM email_search_index WHERE account_id=? GROUP BY indexing_state',
                (context.account_id,))}
        semantic=[]
        query_attempted=Event()
        query_measurement=(estimate_text_tokens(query)
                           if lexical is not None else None)
        query_recorder=TokenRecorder(
            context.account_id,
            usage_request_prefix(
                'semantic-query', context.account_id, secrets.token_hex(16)),
            db_path=application.state.settings.db_path,
            enabled=application.state.settings.token_collection_enabled,
        )
        if lexical is not None:
            try:
                def run_semantic_query():
                    query_attempted.set()
                    try:
                        return semantic_candidates(query,context.account_id,search_collection())
                    except Exception as error:
                        # The isolated indexer can update Chroma from another
                        # process after the API cached its collection handle.
                        # Reopen once when Rust reports a stale handle; real
                        # outages still use the normal lexical fallback.
                        if type(error).__name__ != 'InternalError':
                            raise
                        with vector_lock:
                            application.state.search_collection = None
                            if search_vector_factory is None:
                                with vector_write_lock(application.state.settings.data_dir):
                                    vector_db.reset_persistent_client(
                                        application.state.settings.data_dir/'search_chroma_db',
                                        settings=application.state.settings)
                        return semantic_candidates(query,context.account_id,search_collection())
                semantic=SEARCH_CALLS.run(
                    run_semantic_query,SEARCH_QUERY_TIMEOUT_SECONDS)
                semantic_available=True
                if query_attempted.is_set():
                    query_recorder.record(
                        'query', provider='embedding',
                        model_version=EMBEDDING_MODEL_VERSION,
                        operation=TokenOperation.QUERY_EMBEDDING.value,
                        outcome=TokenOutcome.SUCCESS.value,
                        measurement=query_measurement,
                    )
            except Exception as error:
                if query_attempted.is_set():
                    query_recorder.record(
                        'query', provider='embedding',
                        model_version=EMBEDDING_MODEL_VERSION,
                        operation=TokenOperation.QUERY_EMBEDDING.value,
                        outcome=(TokenOutcome.TIMEOUT.value
                                 if isinstance(error, TimeoutError)
                                 else TokenOutcome.FAILED.value),
                        measurement=query_measurement,
                    )
                log_event('semantic_search_fallback',error=error)
        with application.state.accounts.guard(context) as conn:
            if lexical is not None:
                allowed_semantic=filter_ranked_ids(context.account_id,conn,
                    [identity for identity,_distance in semantic],category)
                allowed=set(allowed_semantic)
                semantic=[row for row in semantic if row[0] in allowed]
                ranked_ids=hybrid_rank(lexical,semantic)
            else:
                ranked_ids=None
            total=len(ranked_ids) if ranked_ids is not None else count_emails(
                context.account_id,conn,search=query,category=category)
            if semantic:
                search_mode='hybrid'
            rows=get_recent_emails(
                limit,account_id=context.account_id,db_conn=conn,offset=offset,
                search=query,category=category,ranked_ids=ranked_ids,
                include_analysis=application.state.settings.explanations_visible,
            )
        return {'emails':rows,'total':total,'limit':limit,'offset':offset,'has_more':offset+limit<total,
                'status':'success','account_id':context.account_id,'generation':context.generation,
                'search_mode':search_mode,'semantic_available':semantic_available,
                'semantic_index':{key:index_counts.get(key,0) for key in ('pending','indexed','failed')}}

    @application.get('/emails/{email_id}/history')
    def email_history(email_id: str, context=Depends(session)):
        with application.state.accounts.guard(context) as conn:
            if not get_email(email_id,account_id=context.account_id,db_conn=conn):
                raise HTTPException(404,'This email does not belong to the connected account.')
            response={'account_id':context.account_id,'generation':context.generation,
                    'feedback':[dict(row) for row in conn.execute('SELECT revision_id,label,indexing_state,created_at FROM feedback_history WHERE account_id=? AND email_id=? ORDER BY revision_id DESC LIMIT 50',(context.account_id,email_id))],
                    'processing':[dict(row) for row in conn.execute('SELECT stage,outcome,error_code,created_at FROM processing_attempts WHERE account_id=? AND email_id=? ORDER BY attempt_id DESC LIMIT 50',(context.account_id,email_id))]}
            if application.state.settings.explanations_visible:
                detail=get_recent_emails(
                    1,account_id=context.account_id,db_conn=conn,
                    ranked_ids=[email_id],include_analysis=True,
                )[0]
                response['analysis']=detail.get('analysis')
                classifications=[]
                for row in conn.execute(
                        'SELECT attempt_id,category,outcome,source,model_version,metadata,created_at FROM prediction_attempts WHERE account_id=? AND email_id=? ORDER BY attempt_id DESC LIMIT 50',
                        (context.account_id,email_id)):
                    metadata=json.loads(row['metadata'])
                    classifications.append({
                        'attempt_id':row['attempt_id'],
                        'category':row['category'],
                        'outcome':row['outcome'],
                        'source':row['source'],
                        'model_version':row['model_version'],
                        'created_at':row['created_at'],
                        'analysis':({
                            key:value for key,value in metadata.get('analysis',{}).items()
                            if key != 'actions'
                        } if isinstance(metadata,dict)
                           and isinstance(metadata.get('analysis'),dict)
                           else None),
                    })
                response['classification']=classifications
            return response

    @application.get('/telemetry')
    def telemetry(context=Depends(session)):
        with application.state.accounts.guard(context,connected=False) as conn:
            totals=dashboard_totals(context.account_id,conn)
            samples=[]
            latest=None
            for row in conn.execute('SELECT category,outcome,source,created_at,metadata FROM prediction_attempts WHERE account_id=? ORDER BY attempt_id DESC LIMIT 50',(context.account_id,)):
                if latest is None:
                    latest={key:row[key] for key in ('category','outcome','source','created_at')}
                try:
                    value=json.loads(row['metadata']).get('elapsed_ms')
                    if type(value) in (int,float) and math.isfinite(value) and value >= 0:
                        samples.append(value)
                except (ValueError,TypeError,AttributeError):
                    pass
            model=application.state.model
            version=getattr(model,'model_version',None)
            return {'account_id':context.account_id,'generation':context.generation,'totals':totals,
                    'classification_timing':{'mean_ms':sum(samples)/len(samples) if samples else None,'sample_count':len(samples),'window':'latest_50_attempts'},
                    'local_model':{'ready':bool(model.model_loaded),'status':'ready' if model.model_loaded else 'loading' if getattr(model,'load_reason',None) == 'model_loading' else 'unavailable',
                                   'version':version if isinstance(version,str) else None,
                                   'evaluation_scope':getattr(model,'training_scope',None) if isinstance(getattr(model,'training_scope',None),str) else None},
                    'mode':{'local_only':application.state.settings.local_only,'external_notifications':'disabled' if application.state.settings.local_only else 'configured_unverified','gmail':'disabled' if application.state.settings.local_only else 'network_required'},
                    'providers':{'gemini':'disabled_local_only' if application.state.settings.local_only else 'configured_unverified' if os.getenv('GEMINI_API_KEY') else 'unconfigured',
                                 'groq':'disabled_local_only' if application.state.settings.local_only else 'configured_unverified' if os.getenv('GROQ_API_KEY') else 'unconfigured'},
                    'latest_prediction':latest}

    @application.post('/predict')
    def predict(payload: EmailRequest, context=Depends(session)):
        usage_recorder=TokenRecorder(
            context.account_id,
            usage_request_prefix(
                'manual-prediction', context.account_id, secrets.token_hex(16)),
            db_path=application.state.settings.db_path,
            enabled=application.state.settings.token_collection_enabled,
        )
        with application.state.accounts.external(
                context,connected=not application.state.settings.local_only):
            try:
                result = manual_prediction(
                    application.state.model, payload.subject, payload.body,
                    context.account_id, application.state.settings, collection,
                    lambda metadata: current_vector(
                        metadata, application.state.settings.db_path),
                    usage_recorder=usage_recorder,
                )
            except TimeoutError:
                result = Prediction(reason='local_inference_budget_exhausted')
            result=ensure_prediction_analysis(result)
            return {
                **result.to_dict(
                    include_analysis=application.state.settings.explanations_visible),
                'status':('success' if result.outcome == 'CLASSIFIED'
                          else result.outcome.lower()),
            }

    def require_actions():
        if not application.state.settings.action_extraction_enabled:
            raise HTTPException(404, 'The Action Center is disabled.')

    def enforce_intelligence_rate(conn, account_id, scope, resource_id):
        try:
            claim_mutation_slot(
                account_id, scope, resource_id,
                cooldown_seconds=application.state.settings.poll_interval_seconds,
                db_conn=conn,
            )
        except MutationRateLimited as error:
            raise HTTPException(
                429,
                'This operation was requested recently. Please wait.',
                headers={'Retry-After': str(error.retry_after)},
            ) from None

    def raise_action_error(error):
        if isinstance(error, LookupError):
            raise HTTPException(
                404, 'This action does not belong to the connected account.')
        if isinstance(error, (ActionRevisionConflict, ActionTransitionError)):
            raise HTTPException(409, str(error))
        if isinstance(error, ValueError):
            raise HTTPException(422, str(error))
        raise error

    @application.get('/actions')
    def actions(
            status: Literal['open','completed','dismissed','snoozed'] | None = None,
            due_from: str | None = Query(None, max_length=64),
            due_to: str | None = Query(None, max_length=64),
            email_id: str | None = Query(None, max_length=256),
            limit: int = Query(50, ge=1, le=200),
            offset: int = Query(0, ge=0, le=1000000),
            context=Depends(session)):
        require_actions()
        try:
            with application.state.accounts.guard(context) as conn:
                rows=list_actions(
                    context.account_id,status=status,due_from=due_from,
                    due_to=due_to,email_id=email_id,limit=limit,
                    offset=offset,db_conn=conn,
                )
        except (LookupError,ValueError) as error:
            raise_action_error(error)
        return {
            'account_id':context.account_id,
            'generation':context.generation,
            'actions':rows,
            'limit':limit,
            'offset':offset,
        }

    @application.get('/actions/summary')
    def actions_summary(context=Depends(session)):
        require_actions()
        with application.state.accounts.guard(context) as conn:
            result=action_summary(context.account_id,db_conn=conn)
        return {
            'account_id':context.account_id,
            'generation':context.generation,
            **result,
        }

    @application.patch('/actions/{action_id}')
    def change_action(
            action_id: int, payload: ActionUpdateRequest,
            context=Depends(session)):
        require_actions()
        try:
            with application.state.accounts.guard(context) as conn:
                result=update_action_status(
                    context.account_id,action_id,status=payload.status,
                    expected_revision=payload.expected_revision,db_conn=conn,
                )
        except (LookupError,ValueError,ActionRevisionConflict,
                ActionTransitionError) as error:
            raise_action_error(error)
        return result

    @application.post('/actions/{action_id}/snooze')
    def snooze_action(
            action_id: int, payload: ActionSnoozeRequest,
            context=Depends(session)):
        require_actions()
        try:
            with application.state.accounts.guard(context) as conn:
                result=update_action_status(
                    context.account_id,action_id,
                    status=ActionStatus.SNOOZED.value,
                    expected_revision=payload.expected_revision,
                    snoozed_until=payload.snoozed_until,db_conn=conn,
                )
        except (LookupError,ValueError,ActionRevisionConflict,
                ActionTransitionError) as error:
            raise_action_error(error)
        return result

    @application.post('/actions/{action_id}/reminders')
    def add_action_reminder(
            action_id: int, payload: ActionReminderRequest,
            context=Depends(session)):
        require_actions()
        config=application.state.settings
        if not config.action_reminders_enabled:
            raise HTTPException(404, 'Action reminders are disabled.')
        if (payload.channel == ReminderChannel.TELEGRAM.value
                and not config.telegram_action_reminders_enabled):
            raise HTTPException(403, 'Telegram action reminders are disabled.')
        try:
            with application.state.accounts.guard(context) as conn:
                action=get_action(
                    context.account_id,action_id,db_conn=conn)
                if action is None:
                    raise LookupError
                if action['revision'] != payload.expected_revision:
                    raise ActionRevisionConflict(
                        'Action changed; refresh and retry')
                enforce_intelligence_rate(
                    conn, context.account_id, 'reminder', str(action_id))
                reminder=schedule_reminder(
                    context.account_id,action_id,
                    remind_at=payload.remind_at,channel=payload.channel,
                    db_conn=conn,
                )
        except (LookupError,ValueError,ActionRevisionConflict,
                ActionTransitionError) as error:
            raise_action_error(error)
        return reminder

    @application.post('/emails/{email_id}/reanalyze')
    def reanalyze_email(
            email_id: str, payload: ActionReanalysisRequest,
            context=Depends(session)):
        require_actions()
        config=application.state.settings
        with application.state.accounts.guard(context) as conn:
            stored=get_email(
                email_id,account_id=context.account_id,db_conn=conn)
            if stored is None:
                raise HTTPException(
                    404, 'This email does not belong to the connected account.')
            current_analysis=conn.execute(
                """SELECT updated_at FROM email_analysis
                   WHERE account_id=? AND email_id=?""",
                (context.account_id,email_id)).fetchone()
            current_revision=(
                current_analysis['updated_at']
                if current_analysis is not None else None)
            if current_revision != payload.expected_analysis_updated_at:
                raise HTTPException(
                    409, 'Analysis changed; refresh and try again.')
            enforce_intelligence_rate(
                conn, context.account_id, 'reanalysis', email_id)
            latest=conn.execute(
                """SELECT created_at FROM token_usage_events
                   WHERE account_id=? AND email_id=?
                     AND operation='action_reanalysis'
                   ORDER BY usage_id DESC LIMIT 1""",
                (context.account_id,email_id)).fetchone()
            if latest is not None:
                previous=datetime.fromisoformat(
                    latest['created_at'].replace('Z','+00:00'))
                if ((datetime.now(timezone.utc)-previous).total_seconds()
                        < config.poll_interval_seconds):
                    raise HTTPException(
                        429, 'This email was re-analyzed recently. Please wait.')
        recorder=TokenRecorder(
            context.account_id,
            usage_request_prefix(
                'action-reanalysis',context.account_id,
                email_id,secrets.token_hex(8)),
            email_id=email_id,db_path=config.db_path,
            enabled=config.token_collection_enabled,
        )
        with application.state.accounts.external(
                context,connected=not config.local_only):
            try:
                result=manual_prediction(
                    application.state.model,stored['subject'],stored['body'],
                    context.account_id,config,collection,
                    lambda metadata:current_vector(
                        metadata,config.db_path),
                    usage_recorder=recorder,
                    usage_operation=TokenOperation.ACTION_REANALYSIS.value,
                    sender=stored['sender'],
                    source_timestamp=stored['created_at'],
                )
            except TimeoutError:
                raise HTTPException(
                    504, 'Re-analysis exceeded its time budget.')
        result=ensure_prediction_analysis(result)
        if result.outcome != 'CLASSIFIED':
            raise HTTPException(
                409, 'Re-analysis did not produce a reliable classification.')
        action_result=None
        analysis_revision=payload.expected_analysis_updated_at
        with application.state.accounts.guard(context) as conn:
            current_analysis=conn.execute(
                """SELECT updated_at FROM email_analysis
                   WHERE account_id=? AND email_id=?""",
                (context.account_id,email_id)).fetchone()
            current_revision=(
                current_analysis['updated_at']
                if current_analysis is not None else None)
            if current_revision != payload.expected_analysis_updated_at:
                raise HTTPException(
                    409, 'Analysis changed; refresh and try again.')
            log_email_to_db(
                email_id,stored['sender'],stored['subject'],stored['body'],
                result,Prediction(),account_id=context.account_id,db_conn=conn,
            )
            try:
                saved_analysis=save_analysis_result(
                    context.account_id,email_id,result.analysis,db_conn=conn)
                analysis_revision=saved_analysis['updated_at']
            except Exception as error:
                log_event('analysis_persistence_deferred',error=error)
                conn.execute(
                    """INSERT INTO processing_attempts(
                       account_id,email_id,stage,outcome,error_code,created_at)
                       VALUES (?,?,'analysis','retry',
                               'analysis_persistence_failed',?)""",
                    (context.account_id,email_id,utc_timestamp()))
            conn.execute('SAVEPOINT manual_action_reanalysis')
            try:
                action_result=persist_analysis_actions(
                    context.account_id,email_id,result.analysis,
                    source_created_at=stored['created_at'],
                    source_text=format_email_text(
                        stored['subject'],stored['body']),
                    timezone_name=config.default_timezone,
                    reminders_enabled=config.action_reminders_enabled,
                    db_conn=conn,
                )
                conn.execute(
                    """INSERT INTO processing_attempts(
                       account_id,email_id,stage,outcome,created_at)
                       VALUES (?,?,'actions','complete',?)""",
                    (context.account_id,email_id,utc_timestamp()))
                conn.execute('RELEASE SAVEPOINT manual_action_reanalysis')
            except Exception as error:
                conn.execute(
                    'ROLLBACK TO SAVEPOINT manual_action_reanalysis')
                conn.execute('RELEASE SAVEPOINT manual_action_reanalysis')
                log_event('action_persistence_deferred',error=error)
                conn.execute(
                    """INSERT INTO processing_attempts(
                       account_id,email_id,stage,outcome,error_code,created_at)
                       VALUES (?,?,'actions','retry',
                               'action_persistence_failed',?)""",
                    (context.account_id,email_id,utc_timestamp()))
        return {
            **result.to_dict(
                include_analysis=config.explanations_visible),
            'actions':action_result,
            'analysis_revision':analysis_revision,
            'status':'success',
        }

    @application.get('/analytics/tokens')
    def token_analytics(
            window: Literal['day','week','month'] = Query('day'),
            context=Depends(session)):
        if not application.state.settings.token_analytics_visible:
            raise HTTPException(404, 'Token analytics are disabled.')
        with application.state.accounts.guard(context) as conn:
            result=aggregate_token_usage(
                context.account_id, window=window,
                timezone_name=application.state.settings.default_timezone,
                db_conn=conn,
            )
        return {
            'account_id':context.account_id,
            'generation':context.generation,
            **result,
        }

    @application.get('/diagnostics/intelligence')
    def intelligence_diagnostic_summary(context=Depends(session)):
        with application.state.accounts.guard(context) as conn:
            result=intelligence_diagnostics(
                context.account_id,
                timezone_name=application.state.settings.default_timezone,
                db_conn=conn,
            )
        return {
            'account_id':context.account_id,
            'generation':context.generation,
            **result,
        }

    @application.post('/intelligence/backfill', status_code=202)
    def start_intelligence_backfill(
            payload: IntelligenceBackfillRequest,
            context=Depends(session)):
        require_actions()
        with application.state.accounts.guard(context) as conn:
            result=queue_intelligence_backfill(
                context.account_id,
                limit=payload.limit,
                max_pending_tasks=(
                    application.state.settings.max_pending_tasks),
                db_conn=conn,
            )
            if not result['admitted']:
                if not result['eligible']:
                    raise HTTPException(
                        409,
                        'No saved emails currently need intelligence backfill.')
                raise HTTPException(
                    409,
                    'The worker queue is full. Wait for current work to finish.')
        job_id=enqueue_cycle(application.state.accounts,context)
        return {
            'job_id':job_id,
            'status':'queued',
            **result,
            'message':(
                f"Queued {result['admitted']} saved email"
                f"{'s' if result['admitted'] != 1 else ''} for analysis. "
                'Backfill never sends historical alerts, marks mail read, '
                'or creates automatic reminders.'
            ),
        }

    @application.post('/feedback')
    def feedback(payload: FeedbackRequest, context=Depends(session)):
        with application.state.accounts.guard(context) as conn:
            stored = get_email(payload.email_id, account_id=context.account_id, db_conn=conn)
            if stored is None:
                raise HTTPException(404, 'This email does not belong to the connected account.')
            check_revision(conn,context,payload.email_id,payload.expected_revision_id)
            revision = update_human_label(payload.email_id, payload.label, account_id=context.account_id, db_conn=conn)
        # SQLite is authoritative. Derived vector indexing is retried by the worker so
        # a slow or unavailable embedding store never delays the user's save.
        return JSONResponse(status_code=202, content={'status':'pending', 'revision_id':revision,
            'indexing_state':'pending', 'message':'Correction saved. Indexing is queued.'})

    def check_revision(conn,context,email_id,expected):
        current=conn.execute('SELECT MAX(revision_id) FROM feedback_history WHERE account_id=? AND email_id=?',(context.account_id,email_id)).fetchone()[0] or 0
        if expected is not None and current != expected:
            raise HTTPException(409,'Feedback changed since you opened this email. Refresh and try again.')

    @application.delete('/feedback/{email_id}')
    def undo_feedback(email_id: str, expected_revision_id: int | None = Query(None,ge=0), context=Depends(session)):
        with application.state.accounts.guard(context) as conn:
            stored=get_email(email_id,account_id=context.account_id,db_conn=conn)
            if stored is None:
                raise HTTPException(404,'This email does not belong to the connected account.')
            check_revision(conn,context,email_id,expected_revision_id)
            if stored['human_label'] is None:
                raise HTTPException(409,'This email has no active feedback to undo.')
            revision=withdraw_human_label(email_id,account_id=context.account_id,db_conn=conn)
        return JSONResponse(status_code=202,content={'revision_id':revision,'indexing_state':'pending',
            'message':'Feedback removed. Previous feedback history is kept. Index cleanup is queued.'})

    @application.post('/feedback/reconcile')
    def reconcile(limit: int = Query(20, ge=1, le=50), context=Depends(session)):
        return reconcile_feedback(application.state.accounts, context, collection, limit)

    @application.get('/status')
    def status(context=Depends(session)):
        with application.state.accounts.guard(context, connected=False) as conn:
            state = application.state.accounts.state(conn)
            backfill=(
                backfill_summary(context.account_id,db_conn=conn)
                if context.account_id else {
                    'eligible':0,'queued':0,'running':0,'retry':0,
                    'complete':0,'dead':0,
                })
            backfill['enabled'] = bool(
                application.state.settings.action_extraction_enabled)
            ingestion = get_ingestion_state(context.account_id, conn)
            if ingestion:
                # An opaque provider cursor is internal worker state.
                ingestion.pop('page_token', None)
                ingestion.pop('history_id', None)
            active_pending=conn.execute(
                'SELECT COUNT(*) FROM processing_tasks WHERE account_id=? AND status IN (?,?,?)',
                (context.account_id,'queued','retry','running')).fetchone()[0]
            pending_by_source={row['source']:row['count'] for row in conn.execute(
                """SELECT source,COUNT(*) AS count FROM processing_tasks
                   WHERE account_id=? AND status IN ('queued','retry','running')
                   GROUP BY source""",(context.account_id,))}
            processing_counts={row['status']:row['count'] for row in conn.execute(
                'SELECT status,COUNT(*) AS count FROM processing_tasks WHERE account_id=? GROUP BY status',
                (context.account_id,))}
            search_index_counts={row['indexing_state']:row['count'] for row in conn.execute(
                'SELECT indexing_state,COUNT(*) AS count FROM email_search_index WHERE account_id=? GROUP BY indexing_state',
                (context.account_id,))}
            backlog_pending=pending_by_source.get('backlog',0)
            live_pending=pending_by_source.get('live',0)
            batch_target=application.state.settings.max_pending_tasks
            batch_remaining=min(batch_target,ingestion['backlog_remaining']) if ingestion else batch_target
            batch_admitted=max(0,batch_target-batch_remaining)
            workflow_total=sum(processing_counts.values())
            workflow_finished=processing_counts.get('complete',0)+processing_counts.get('dead',0)
            ingestion_paused=bool(ingestion and ingestion['has_more']
                                  and not ingestion['backlog_authorized'])
            fetch_next_available=bool(ingestion and ingestion['initial_batch_complete']
                                      and ingestion['has_more']
                                      and not ingestion['backlog_authorized']
                                      and backlog_pending == 0)
            return {**{key: bool(state[key]) for key in ('connected', 'is_polling', 'auth_in_progress', 'purge_pending')},
                    'generation': state['generation'], 'account_id':context.account_id, 'ingestion':ingestion,
                    'ingestion_paused':ingestion_paused,
                    'active_pending_tasks':active_pending,
                    'live_pending_tasks':live_pending,
                    'backlog_pending_tasks':backlog_pending,
                    'current_batch_target_tasks':batch_target,
                    'current_batch_admitted_tasks':batch_admitted,
                    'workflow_total_tasks':workflow_total,
                    'workflow_finished_tasks':workflow_finished,
                    'fetch_next_available':fetch_next_available,
                    'live_monitoring':bool(state['connected'] and not application.state.settings.local_only),
                    'max_pending_tasks':application.state.settings.max_pending_tasks,
                    'resume_pending_tasks':application.state.settings.resume_pending_tasks,
                    'worker':dict(health) if (health:=conn.execute('SELECT heartbeat_at,last_success_at,last_error_at,last_error_code FROM worker_health WHERE account_id=?',(context.account_id,)).fetchone()) else None,
                    'auto_mark_read':application.state.settings.auto_mark_read,
                    'ingestion_failures':[dict(row) for row in conn.execute('SELECT email_id,status,attempt_count,next_retry_at,error_code FROM ingestion_failures WHERE account_id=? ORDER BY updated_at DESC,email_id LIMIT 50',(context.account_id,))],
                    'processing_counts':processing_counts,
                    'notification_counts':{row['status']:row['count'] for row in conn.execute('SELECT status,COUNT(*) AS count FROM notification_outbox WHERE account_id=? GROUP BY status',(context.account_id,))},
                    'feedback_index_pending':conn.execute("SELECT COUNT(*) FROM feedback_history f WHERE account_id=? AND indexing_state!='indexed' AND revision_id=(SELECT MAX(revision_id) FROM feedback_history x WHERE x.account_id=f.account_id AND x.email_id=f.email_id)", (context.account_id,)).fetchone()[0],
                    'semantic_search_index':{key:search_index_counts.get(key,0) for key in ('pending','indexed','failed')},
                    'intelligence_backfill':backfill,
                    'poll_interval_seconds':application.state.settings.poll_interval_seconds,
                    'worker_lease_seconds':application.state.settings.worker_lease_seconds,
                    'batch_size':application.state.settings.batch_size}

    @application.get('/user')
    def user(context=Depends(session)):
        with application.state.accounts.guard(context, connected=False) as conn:
            state = application.state.accounts.state(conn)
            return {'email': state['account_id'], 'connected': bool(state['connected'])}

    @application.post('/logout')
    def logout(context=Depends(session)):
        application.state.accounts.logout(context)
        response = JSONResponse({'message': 'Signed out. Processing stopped. Saved mail and Google credentials are kept.'})
        response.delete_cookie(COOKIE, path='/')
        return response

    @application.post('/disconnect')
    def disconnect(context=Depends(session)):
        application.state.accounts.disconnect(context)
        return {'message': 'Processing stopped. Local Google credentials removed. Saved mail is kept. Google permission is not revoked.'}

    @application.delete('/account-data')
    def purge(context=Depends(session)):
        try:
            application.state.accounts.purge(context, collection, search_collection)
        except (WorkCancelled, AccessDenied, sqlite3.OperationalError):
            raise
        except ValueError:
            raise HTTPException(409, 'There is no account to delete.') from None
        except Exception as error:
            log_event('account_purge_failed', error=error)
            raise HTTPException(503, 'Deletion did not finish. Processing is paused. Retry deleting this account data.') from None
        return {'message': "This account's saved mail, feedback, jobs, and local Google credentials were deleted. Other accounts and old unassigned data are kept."}

    @application.delete('/legacy-data')
    def purge_legacy(payload: LegacyDeleteRequest, context=Depends(session)):
        try:
            application.state.accounts.purge_legacy(context, collection(), search_collection)
        except (WorkCancelled, AccessDenied, sqlite3.OperationalError):
            raise
        except Exception as error:
            log_event('legacy_purge_failed', error=error)
            raise HTTPException(503, 'Old-data deletion did not finish. Please retry.') from None
        return {'message': 'Old unassigned mail, its archive, vectors, and old shared credential files were deleted.'}

    @application.post('/inbox/sync',status_code=202)
    def sync_new_messages(context=Depends(session)):
        settings=application.state.settings
        if settings.local_only:
            raise HTTPException(403,'Gmail inbox checks are disabled in local-only mode.')
        now=time.time()
        with application.state.accounts.guard(context) as conn:
            ingestion=ensure_ingestion_state(context.account_id,conn,batch_limit=settings.max_pending_tasks)
            if now-ingestion['last_manual_sync_at'] < settings.poll_interval_seconds:
                raise HTTPException(429,'A new-mail sync was requested recently. Please wait a few seconds.')
            conn.execute('UPDATE ingestion_state SET last_manual_sync_at=? WHERE account_id=?',
                         (now,context.account_id))
        job_id=enqueue_cycle(application.state.accounts,context)
        return {'job_id':job_id,'status':'queued',
                'message':'Checking Gmail for new messages. New mail will receive live priority.'}

    @application.post('/ingestion/fetch-next',status_code=202)
    @application.post('/ingestion/resume',status_code=202)
    def fetch_next_backlog(context=Depends(session)):
        settings=application.state.settings
        if settings.local_only:
            raise HTTPException(403,'Gmail inbox extraction is disabled in local-only mode.')
        with application.state.accounts.guard(context) as conn:
            ingestion=get_ingestion_state(context.account_id,conn)
            if not ingestion or not ingestion['initial_batch_complete']:
                raise HTTPException(409,'The initial inbox batch is not complete.')
            if ingestion['backlog_authorized']:
                raise HTTPException(409,'A backlog batch is already authorized.')
            if not ingestion['has_more']:
                raise HTTPException(409,'There are no older inbox messages waiting.')
            pending=conn.execute(
                """SELECT COUNT(*) FROM processing_tasks WHERE account_id=? AND source='backlog'
                   AND status IN ('queued','retry','running')""",(context.account_id,)).fetchone()[0]
            if pending:
                raise HTTPException(409,'Finish the current backlog batch before fetching the next 100.')
            conn.execute("""UPDATE ingestion_state SET backlog_authorized=1,
                backlog_remaining=?,status='empty' WHERE account_id=?""",
                (settings.max_pending_tasks,context.account_id))
        job_id=enqueue_cycle(application.state.accounts,context)
        return {'job_id':job_id,'status':'queued',
                'message':'The next 100 older messages are authorized. New live mail still has priority.'}

    @application.get('/jobs/{job_id}')
    def job(job_id: str,context=Depends(session)):
        with application.state.accounts.guard(context,connected=False) as conn:
            auth=conn.execute('SELECT * FROM auth_jobs WHERE job_id=? AND session_hash=? AND generation=?',
                (job_id,context.session_hash,context.generation)).fetchone()
            if auth:
                return {key:auth[key] for key in ('job_id','status','processing_job_id','error_code','created_at','updated_at')}
            if job_id.isdigit():
                task=conn.execute('SELECT job_id,status,kind,error_code,created_at,updated_at FROM worker_jobs WHERE job_id=? AND account_id=? AND generation=?',(int(job_id),context.account_id,context.generation)).fetchone()
                if task:
                    return dict(task)
            raise HTTPException(404,'This job does not belong to the current session/account.')

    @application.post('/notifications/{email_id}/resolve')
    def resolve_delivery(email_id: str,payload: DeliveryResolution,context=Depends(session)):
        with application.state.accounts.guard(context) as conn:
            row=conn.execute('SELECT status FROM notification_outbox WHERE account_id=? AND email_id=?',(context.account_id,email_id)).fetchone()
            if not row:
                raise HTTPException(404,'This alert does not belong to the connected account.')
            if row['status'] not in ('unknown','blocked','dead','retry'):
                raise HTTPException(409,'Only unresolved alerts can be changed.')
            state='sent' if payload.action == 'confirmed_sent' else 'queued'
            if payload.action == 'confirmed_sent' and row['status'] != 'unknown':
                raise HTTPException(409,'Only an unknown delivery can be confirmed sent.')
            stamp=utc_timestamp()
            conn.execute('UPDATE notification_outbox SET status=?,error_code=?,next_retry_at=0,attempt_count=0,updated_at=? WHERE account_id=? AND email_id=?',
                (state,'user_confirmed_sent' if state == 'sent' else 'user_requested_retry',stamp,context.account_id,email_id))
            conn.execute("UPDATE processing_tasks SET status='queued',stage=?,attempt_count=0,next_retry_at=0,error_code=NULL WHERE account_id=? AND email_id=?",('mark_read' if state == 'sent' else 'notify',context.account_id,email_id))
            conn.execute('INSERT INTO processing_attempts(account_id,email_id,stage,outcome,error_code,created_at) VALUES (?,?,?,?,?,?)',
                (context.account_id,email_id,'notify',state,'user_resolution',stamp))
        return {'message':'Delivery decision saved. An explicit retry of an unknown alert may send a duplicate.' if state != 'sent' else 'Delivery confirmed by you. The worker can finish the remaining step.'}

    @application.post('/tasks/{email_id}/retry')
    def retry_task(email_id: str,context=Depends(session)):
        with application.state.accounts.guard(context) as conn:
            task=conn.execute('SELECT status,stage FROM processing_tasks WHERE account_id=? AND email_id=?',(context.account_id,email_id)).fetchone()
            if not task:
                failed=conn.execute('SELECT 1 FROM ingestion_failures WHERE account_id=? AND email_id=?',(context.account_id,email_id)).fetchone()
                if not failed:
                    raise HTTPException(404,'This message is not in the account retry queue.')
                conn.execute('DELETE FROM ingestion_failures WHERE account_id=? AND email_id=?',(context.account_id,email_id))
            else:
                if task['stage'] == 'notify' or task['status'] not in ('retry','dead'):
                    raise HTTPException(409,'Use alert resolution for a failed notification; completed or running tasks cannot be repeated.')
                conn.execute("UPDATE processing_tasks SET status='queued',attempt_count=0,next_retry_at=0,error_code=NULL WHERE account_id=? AND email_id=?",(context.account_id,email_id))
                conn.execute(
                    """UPDATE intelligence_backfill_items
                       SET status='queued',error_code=NULL,updated_at=?
                       WHERE account_id=? AND email_id=?""",
                    (utc_timestamp(),context.account_id,email_id))
        return {'message':'Retry saved. Completed notification steps stay completed.'}

    @application.post('/authenticate',status_code=202)
    def authenticate(context=Depends(session)):
        with auth_lock:
            return start_auth(context)

    def start_auth(context):
        if application.state.settings.local_only: raise HTTPException(403,'Google authentication is disabled in local-only mode.')
        manager=application.state.accounts
        # Bound outstanding browser flows, including cancelled in-flight attempts.
        pending={key:value for key,value in application.state.auth_futures.items() if not value.done()}
        application.state.auth_futures=pending
        if pending:
            raise HTTPException(409,'Google sign-in is already in progress. Finish or cancel it before trying again.')
        attempt=manager.begin_auth(context)
        job_id=secrets.token_hex(16)
        with manager.guard(attempt,connected=False) as conn:
            stamp=utc_timestamp()
            conn.execute("INSERT INTO auth_jobs(job_id,generation,session_hash,status,created_at,updated_at) VALUES (?,?,?,'running',?,?)",(job_id,attempt.generation,attempt.session_hash,stamp,stamp))
        def finish_sign_in():
            try:
                with manager.external(attempt,connected=False):
                    if oauth_factory is None:
                        from src.email_client import authenticate_gmail
                        candidate=authenticate_gmail()
                    else:
                        candidate=oauth_factory()
                if candidate is None:
                    raise ValueError('Sign-in did not complete')
                connected=manager.finish_auth(attempt,candidate)
                processing_job=enqueue_cycle(manager,connected)
                with manager.guard(connected) as conn:
                    conn.execute("UPDATE auth_jobs SET status='complete',processing_job_id=?,updated_at=? WHERE job_id=?",(processing_job,utc_timestamp(),job_id))
            except (WorkCancelled,AccessDenied):
                manager.fail_auth(attempt)
            except Exception as error:
                log_event('auth_job_failed',error=error)
                manager.fail_auth(attempt)
                try:
                    with manager.guard(attempt,connected=False) as conn:
                        conn.execute("UPDATE auth_jobs SET status='failed',error_code='sign_in_failed',updated_at=? WHERE job_id=?",(utc_timestamp(),job_id))
                except (WorkCancelled,AccessDenied):
                    pass
        application.state.auth_futures[job_id]=application.state.auth_executor.submit(finish_sign_in)
        return {'job_id':job_id,'status':'running','message':'Google sign-in started. Finish it in the browser. Inbox work runs separately.'}

    origins = (settings or Settings()).frontend_origins
    application.add_middleware(CORSMiddleware, allow_origins=list(origins), allow_credentials=True,
                               allow_methods=['GET', 'POST', 'PATCH', 'DELETE'], allow_headers=['Content-Type', 'X-CSRF-Token'])
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]'])
    return application


app = create_app()

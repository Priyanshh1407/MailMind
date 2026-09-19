"""Local API: pair the browser, then explicitly connect one Google account."""
from contextlib import asynccontextmanager
import os
import json
import math
import hmac
import secrets
import sqlite3
import time
from threading import Lock
from src.background_jobs import BackgroundJobs
from src.database import utc_timestamp
from src.work_queue import enqueue_cycle
from src.provider_policy import LOCAL_CALLS
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
from src.db_utils import get_recent_emails, get_email, update_human_label, get_ingestion_state, count_emails, dashboard_totals, withdraw_human_label
from src.local_llm import DeferredMailMindModel, MailMindModel
from src.llm_api import classify_email
from src.logging_utils import log_event
from src import vector_db
from src.prediction import Category, Prediction
from src.retrieval_policy import load_policy
from src.feedback import reconcile_feedback, current_vector

COOKIE = 'mailmind_session'


class PairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)


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


class LegacyDeleteRequest(BaseModel):
    confirmation: Literal['DELETE_UNASSIGNED_DATA']


def manual_prediction(model, subject, body, account_id, settings, collection_provider, validator,
                      classifier=classify_email):
    """Use cloud temporarily in normal mode while the local API model loads."""
    if not settings.local_only and getattr(model, 'load_reason', None) == 'model_loading':
        return classifier('[MANUAL]', subject, body, account_id=account_id,
                          collection_provider=collection_provider, validator=validator,
                          settings=settings)
    return LOCAL_CALLS.run(lambda:model.predict(subject, body, account_id=account_id),
                           settings.classification_budget_seconds)

def create_app(*, settings=None, model_factory=MailMindModel,
               vector_factory=vector_db.create_vector_collection, oauth_factory=None):
    vector_lock, pair_lock, auth_lock = Lock(), Lock(), Lock()
    failed_pairs = []

    @asynccontextmanager
    async def lifespan(application):
        try:
            config = settings or Settings.from_environment(load_file=True)
            if config.access_key and len(config.access_key) < 24:
                raise ValueError('Use a local access key with at least 24 characters')
            await run_in_threadpool(initialize_database, config.db_path)
            application.state.settings = config
            application.state.accounts = AccountManager(config)
            await run_in_threadpool(application.state.accounts.restart)
            application.state.access_key = config.access_key or secrets.token_urlsafe(32)
            if not config.access_key:
                print('\nMailMind local pairing code (paste into the browser):\n' + application.state.access_key + '\n', flush=True)
            application.state.collection = None
            application.state.auth_executor = BackgroundJobs(max_workers=2)
            application.state.auth_futures = {}
            model_options = {'model_path': config.model_path,
                             'vector_service': vector_db.VectorService(collection, lambda metadata: current_vector(metadata, config.db_path),policy=load_policy(config.retrieval_policy_path))}
            if model_factory is MailMindModel:
                model_options['settings'] = config
            if model_factory is MailMindModel and not config.local_only:
                application.state.model = DeferredMailMindModel(model_path=config.model_path, settings=config)
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
            application.state.access_key = None

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
    def pair(payload: PairRequest, request: Request):
        allowed_origin(request)
        with pair_lock:
            now = time.monotonic()
            failed_pairs[:] = [stamp for stamp in failed_pairs if now-stamp < 60]
            if len(failed_pairs) >= 10:
                raise HTTPException(429, 'Too many attempts. Wait one minute.')
            if not hmac.compare_digest(payload.code.encode('utf-8'), application.state.access_key.encode('utf-8')):
                failed_pairs.append(now)
                raise HTTPException(401, 'The pairing code is wrong.')
        token, csrf = application.state.accounts.pair()
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
               context=Depends(session)):
        with application.state.accounts.guard(context) as conn:
            total=count_emails(context.account_id,conn,search=search,category=category)
            return {'emails':get_recent_emails(limit,account_id=context.account_id,db_conn=conn,offset=offset,search=search,category=category),
                    'total':total,'limit':limit,'offset':offset,'has_more':offset+limit<total,
                    'status':'success','account_id':context.account_id,'generation':context.generation}

    @application.get('/emails/{email_id}/history')
    def email_history(email_id: str, context=Depends(session)):
        with application.state.accounts.guard(context) as conn:
            if not get_email(email_id,account_id=context.account_id,db_conn=conn):
                raise HTTPException(404,'This email does not belong to the connected account.')
            return {'account_id':context.account_id,'generation':context.generation,
                    'feedback':[dict(row) for row in conn.execute('SELECT revision_id,label,indexing_state,created_at FROM feedback_history WHERE account_id=? AND email_id=? ORDER BY revision_id DESC LIMIT 50',(context.account_id,email_id))],
                    'processing':[dict(row) for row in conn.execute('SELECT stage,outcome,error_code,created_at FROM processing_attempts WHERE account_id=? AND email_id=? ORDER BY attempt_id DESC LIMIT 50',(context.account_id,email_id))]}

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
        with application.state.accounts.external(context,connected=not application.state.settings.local_only):
            try:
                result = manual_prediction(application.state.model, payload.subject, payload.body, context.account_id,
                                           application.state.settings, collection,
                                           lambda metadata: current_vector(metadata, application.state.settings.db_path))
            except TimeoutError:
                result = Prediction(reason='local_inference_budget_exhausted')
            return {**result.to_dict(), 'status': 'success' if result.outcome == 'CLASSIFIED' else result.outcome.lower()}

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
            ingestion = get_ingestion_state(context.account_id, conn)
            if ingestion:
                # An opaque provider cursor is internal worker state.
                ingestion.pop('page_token', None)
            return {**{key: bool(state[key]) for key in ('connected', 'is_polling', 'auth_in_progress', 'purge_pending')},
                    'generation': state['generation'], 'account_id':context.account_id, 'ingestion':ingestion,
                    'worker':dict(health) if (health:=conn.execute('SELECT heartbeat_at,last_success_at,last_error_at,last_error_code FROM worker_health WHERE account_id=?',(context.account_id,)).fetchone()) else None,
                    'auto_mark_read':application.state.settings.auto_mark_read,
                    'ingestion_failures':[dict(row) for row in conn.execute('SELECT email_id,status,attempt_count,next_retry_at,error_code FROM ingestion_failures WHERE account_id=? ORDER BY updated_at DESC,email_id LIMIT 50',(context.account_id,))],
                    'processing_counts':{row['status']:row['count'] for row in conn.execute('SELECT status,COUNT(*) AS count FROM processing_tasks WHERE account_id=? GROUP BY status',(context.account_id,))},
                    'notification_counts':{row['status']:row['count'] for row in conn.execute('SELECT status,COUNT(*) AS count FROM notification_outbox WHERE account_id=? GROUP BY status',(context.account_id,))},
                    'feedback_index_pending':conn.execute("SELECT COUNT(*) FROM feedback_history f WHERE account_id=? AND indexing_state!='indexed' AND revision_id=(SELECT MAX(revision_id) FROM feedback_history x WHERE x.account_id=f.account_id AND x.email_id=f.email_id)", (context.account_id,)).fetchone()[0],
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
            application.state.accounts.purge(context, collection)
        except (WorkCancelled, AccessDenied, sqlite3.OperationalError):
            raise
        except ValueError:
            raise HTTPException(409, 'There is no account to delete.') from None
        except Exception as error:
            log_event('account_purge_failed', error=error)
            raise HTTPException(503, 'Deletion did not finish. Processing is paused. Retry deleting this account data.') from None
        return {'message': 'This account’s saved mail, feedback, jobs, and local Google credentials were deleted. Other accounts and old unassigned data are kept.'}

    @application.delete('/legacy-data')
    def purge_legacy(payload: LegacyDeleteRequest, context=Depends(session)):
        try:
            application.state.accounts.purge_legacy(context, collection())
        except (WorkCancelled, AccessDenied, sqlite3.OperationalError):
            raise
        except Exception as error:
            log_event('legacy_purge_failed', error=error)
            raise HTTPException(503, 'Old-data deletion did not finish. Please retry.') from None
        return {'message': 'Old unassigned mail, its archive, vectors, and old shared credential files were deleted.'}

    @application.post('/process',status_code=202)
    def queue_processing(context=Depends(session)):
        if application.state.settings.local_only: raise HTTPException(403,'Gmail inbox checks are disabled in local-only mode. Saved-task processing uses the local worker.')
        job_id=enqueue_cycle(application.state.accounts,context)
        return {'job_id':job_id,'status':'queued','message':'Inbox processing queued for the background worker.'}

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
        if len(pending) >= 2:
            raise HTTPException(503,'Previous Google sign-in is still closing. Try again shortly.')
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
                               allow_methods=['GET', 'POST', 'DELETE'], allow_headers=['Content-Type', 'X-CSRF-Token'])
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]'])
    return application


app = create_app()

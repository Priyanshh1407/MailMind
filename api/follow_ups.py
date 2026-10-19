"""Routes for the Follow-ups tab. MailMind reads Gmail here; it never sends mail."""
from contextlib import contextmanager
from datetime import datetime
from email.utils import parseaddr
import json
import secrets
import time
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.account_state import AccessDenied, WorkCancelled
from src.background_jobs import BackgroundJobs
from src.database import utc_timestamp
from src.follow_up_store import (cached_style, compute_style, list_sent, public_follow_up, read_thread, reply_states,
                                 save_sent, sent_page)
from src.follow_ups import OUTPUT_SCHEMA, SYSTEM_INSTRUCTION, draft, prepare_thread, validate_draft
from src.intelligence_contract import DEFAULT_TIMEZONE, TokenOperation
from src.intelligence_safety import MutationRateLimited, claim_mutation_slot
from src.logging_utils import log_event
from src.token_usage import TokenRecorder, usage_request_prefix

DRAFT_COOLDOWN_SECONDS = 5
REFRESH_COOLDOWN_SECONDS = 20


class DraftRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=256)
    length: Literal['short', 'standard'] = 'standard'


class RefreshRequest(BaseModel):
    more: bool = False


class FollowUpUpdate(BaseModel):
    status: Literal['copied', 'dismissed']


def register(application, session):
    jobs = BackgroundJobs(max_workers=2)
    state = {}  # account_id -> {'refreshing': bool, 'page_token': str|None, 'refreshed_at': str|None}

    def enabled():
        settings = application.state.settings
        if not settings.follow_ups_enabled:
            raise HTTPException(404, 'Follow-ups are switched off (MAILMIND_FOLLOW_UPS_ENABLED).')
        if settings.local_only:
            raise HTTPException(403, 'Follow-ups read Gmail and use an AI provider, so they are off in local-only mode.')

    def tools(context):
        manager = application.state.accounts

        @contextmanager
        def guard():
            with manager.guard(context):
                pass
            yield
            with manager.guard(context):
                pass

        @contextmanager
        def conn_factory():
            with manager.guard(context) as conn:
                yield conn

        def service():
            from src.email_client import refresh_gmail
            gmail = refresh_gmail(manager, context)
            if gmail is None:
                raise PermissionError('gmail_unavailable')
            return gmail
        return guard, conn_factory, service

    def refresh(context, more):
        account = context.account_id
        entry = state.setdefault(account, {'refreshing': False, 'page_token': None, 'refreshed_at': None, 'error': None})
        guard, conn_factory, service = tools(context)
        try:
            gmail = service()
            rows, token = list_sent(gmail, page_token=entry['page_token'] if more else None, guard=guard)
            states = reply_states(gmail, list(dict.fromkeys(row['thread_id'] for row in rows)), account, guard=guard)
            with conn_factory() as conn:
                save_sent(conn, account, rows, states)
            entry.update(page_token=token, refreshed_at=utc_timestamp(), error=None)
        except (WorkCancelled, AccessDenied):
            pass
        except Exception as error:
            log_event('sent_refresh_failed', error=error)
            entry['error'] = 'gmail_unavailable' if isinstance(error, PermissionError) else 'sent_refresh_failed'
        finally:
            entry['refreshing'] = False

    def run_draft(context, follow_up_id, message_id, length):
        manager, settings = application.state.accounts, application.state.settings
        guard, conn_factory, service = tools(context)
        started = time.monotonic()
        tokens = {'total': 0}
        recorder = TokenRecorder(context.account_id,
                                 usage_request_prefix('follow-up', context.account_id, secrets.token_hex(16)),
                                 db_path=settings.db_path, enabled=settings.token_collection_enabled)
        original = recorder.record

        def record(*args, **kwargs):
            measurement = kwargs.get('measurement')
            if measurement is not None and isinstance(measurement.total_tokens, int):
                tokens['total'] += measurement.total_tokens
            return original(*args, **kwargs)
        recorder.record = record

        def save(**fields):
            fields['updated_at'] = utc_timestamp()
            with conn_factory() as conn:
                conn.execute(f'UPDATE follow_ups SET {",".join(f"{key}=?" for key in fields)} '
                             'WHERE follow_up_id=? AND account_id=?', (*fields.values(), follow_up_id, context.account_id))
        try:
            with conn_factory() as conn:
                row = conn.execute('SELECT thread_id FROM sent_messages WHERE account_id=? AND message_id=?',
                                   (context.account_id, message_id)).fetchone()
                style = cached_style(conn, context.account_id)
            gmail = service()
            messages = read_thread(gmail, row['thread_id'], guard=guard)
            if not messages:
                raise ValueError('thread_empty')
            thread, summary, masker, names, info = prepare_thread(messages, context.account_id)
            save(summary_json=json.dumps(summary, ensure_ascii=False), recipient=info['recipient'][:200])
            if style is None:
                mine = next((item['from'] for item in reversed(messages)
                             if parseaddr(item['from'])[1].casefold() == context.account_id.casefold()), '')
                style = compute_style(gmail, conn_factory, context.account_id, parseaddr(mine)[0], guard=guard)

            def call(contents):
                from src.llm_api import call_structured
                value, _source, _model = call_structured(
                    contents, system=SYSTEM_INSTRUCTION, schema=OUTPUT_SCHEMA, schema_name='mailmind_follow_up',
                    settings=settings, operation=TokenOperation.FOLLOW_UP_DRAFT.value, validate=validate_draft,
                    usage_recorder=recorder, before_request=guard, budget_seconds=25, max_output_tokens=1024,
                    record_key='follow-up', max_chars=8000)
                return value
            result = draft(thread, style, length, masker, names, call=call,
                           today=datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date())
            save(status='drafted', draft_text=result['text'], issues_json=json.dumps(result['issues']),
                 revised=int(result['revised']), references_json=json.dumps(result['references'], ensure_ascii=False),
                 total_tokens=tokens['total'], latency_ms=round((time.monotonic() - started) * 1000))
        except (WorkCancelled, AccessDenied):
            try:
                save(status='failed', error_code='account_changed')
            except (WorkCancelled, AccessDenied):
                pass
        except Exception as error:
            log_event('follow_up_failed', error=error)
            code = getattr(error, 'code', None) or ('gmail_unavailable' if isinstance(error, PermissionError) else 'follow_up_failed')
            try:
                save(status='failed', error_code=str(code)[:80], total_tokens=tokens['total'],
                     latency_ms=round((time.monotonic() - started) * 1000))
            except (WorkCancelled, AccessDenied):
                pass

    @application.get('/follow-ups/sent')
    def sent(search: str = Query('', max_length=100), limit: int = Query(25, ge=1, le=50),
             offset: int = Query(0, ge=0, le=10000), context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context, connected='read') as conn:
            items, total = sent_page(conn, context.account_id, search=search.strip(), limit=limit, offset=offset)
        entry = state.get(context.account_id, {})
        return {'account_id': context.account_id, 'generation': context.generation, 'messages': items, 'total': total,
                'limit': limit, 'offset': offset, 'refreshing': bool(entry.get('refreshing')),
                'refreshed_at': entry.get('refreshed_at'), 'has_more': bool(entry.get('page_token')),
                'error_code': entry.get('error')}

    @application.post('/follow-ups/sent/refresh', status_code=202)
    def refresh_sent(payload: RefreshRequest | None = None, context=Depends(session)):
        enabled()
        more = bool(payload and payload.more)
        with application.state.accounts.guard(context) as conn:
            try:
                claim_mutation_slot(context.account_id, 'follow_up', 'refresh' + ('-more' if more else ''),
                                    cooldown_seconds=REFRESH_COOLDOWN_SECONDS if not more else 3, db_conn=conn)
            except MutationRateLimited:
                return {'status': 'recent', 'message': 'Your sent mail was fetched moments ago.'}
        entry = state.setdefault(context.account_id, {'refreshing': False, 'page_token': None, 'refreshed_at': None, 'error': None})
        if entry['refreshing']:
            return {'status': 'running', 'message': 'Already fetching your sent mail.'}
        entry['refreshing'] = True
        try:
            jobs.submit(lambda: refresh(context, more))
        except RuntimeError:
            entry['refreshing'] = False
            raise HTTPException(503, 'MailMind is busy. Try again shortly.') from None
        return {'status': 'running', 'message': 'Fetching your sent mail...'}

    @application.post('/follow-ups/drafts', status_code=202)
    def create_draft(payload: DraftRequest, context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context) as conn:
            if conn.execute('SELECT 1 FROM sent_messages WHERE account_id=? AND message_id=?',
                            (context.account_id, payload.message_id)).fetchone() is None:
                raise HTTPException(404, 'This sent email is not in the connected account.')
            try:
                claim_mutation_slot(context.account_id, 'follow_up', 'draft', cooldown_seconds=DRAFT_COOLDOWN_SECONDS,
                                    db_conn=conn)
            except MutationRateLimited:
                raise HTTPException(429, 'One draft at a time: wait a few seconds and try again.') from None
            stamp = utc_timestamp()
            thread_id = conn.execute('SELECT thread_id FROM sent_messages WHERE account_id=? AND message_id=?',
                                     (context.account_id, payload.message_id)).fetchone()[0]
            follow_up_id = conn.execute(
                """INSERT INTO follow_ups(account_id,message_id,thread_id,length,status,created_at,updated_at)
                   VALUES (?,?,?,?,'running',?,?)""",
                (context.account_id, payload.message_id, thread_id, payload.length, stamp, stamp)).lastrowid
        try:
            jobs.submit(lambda: run_draft(context, follow_up_id, payload.message_id, payload.length))
        except RuntimeError:
            with application.state.accounts.guard(context) as conn:
                conn.execute("UPDATE follow_ups SET status='failed',error_code='runner_busy' WHERE follow_up_id=?",
                             (follow_up_id,))
            raise HTTPException(503, 'MailMind is busy writing other drafts. Try again shortly.') from None
        return {'follow_up_id': follow_up_id, 'status': 'running', 'message': 'Reading the thread...'}

    @application.get('/follow-ups/drafts/{follow_up_id}')
    def get_draft(follow_up_id: int, context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context, connected='read') as conn:
            row = conn.execute('SELECT * FROM follow_ups WHERE follow_up_id=? AND account_id=?',
                               (follow_up_id, context.account_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'This draft does not belong to the connected account.')
        return {'account_id': context.account_id, 'generation': context.generation, **public_follow_up(row)}

    @application.patch('/follow-ups/drafts/{follow_up_id}')
    def update_draft(follow_up_id: int, payload: FollowUpUpdate, context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context) as conn:
            changed = conn.execute("UPDATE follow_ups SET status=?,updated_at=? WHERE follow_up_id=? AND account_id=? "
                                   "AND status IN ('drafted','copied')",
                                   (payload.status, utc_timestamp(), follow_up_id, context.account_id)).rowcount
        if not changed:
            raise HTTPException(409, 'Only a finished draft can be marked.')
        return {'follow_up_id': follow_up_id, 'status': payload.status, 'message': 'Saved.'}

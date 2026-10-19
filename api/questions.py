"""Routes for "Ask your inbox": a question becomes a background job with a live trace."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import secrets
import time

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from src.account_state import AccessDenied, WorkCancelled
from src.background_jobs import BackgroundJobs
from src.database import utc_timestamp
from src.inbox_agent import (BUDGET_SECONDS, MAX_QUESTION_CHARS, OUTPUT_SCHEMA, SYSTEM_INSTRUCTION, Tools, ask,
                             salvage_step, validate_step)
from src.intelligence_contract import TokenOperation
from src.intelligence_safety import MutationRateLimited, claim_mutation_slot
from src.logging_utils import log_event
from src.token_usage import TokenRecorder, usage_request_prefix

QUESTION_COOLDOWN_SECONDS = 3
HISTORY_LIMIT = 20


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)

    @model_validator(mode='after')
    def nonempty(self):
        self.question = ' '.join(self.question.split())
        if len(self.question) < 3:
            raise ValueError('Ask a question of at least three characters')
        return self


def public_question(row):
    return {'question_id': row['question_id'], 'question': row['question'], 'status': row['status'],
            'answer': json.loads(row['answer_json']) if row['answer_json'] else None,
            'trace': json.loads(row['trace_json']), 'total_tokens': row['total_tokens'],
            'latency_ms': row['latency_ms'], 'error_code': row['error_code'],
            'created_at': row['created_at'], 'updated_at': row['updated_at']}


def register(application, session, search_collection):
    # Questions get their own runner so a slow answer never delays Google sign-in.
    jobs = BackgroundJobs(max_workers=2)

    def enabled():
        if not application.state.settings.inbox_questions_enabled:
            raise HTTPException(404, 'Ask your inbox is switched off (MAILMIND_INBOX_QUESTIONS_ENABLED).')

    def run_question(context, question_id, question):
        manager, settings = application.state.accounts, application.state.settings

        @contextmanager
        def read_db():
            # Every read is fenced: an account switch or stop ends the job.
            with manager.guard(context, connected='read') as conn:
                yield conn

        @contextmanager
        def fenced():
            # Around a provider call: check the account before and after, but
            # never hold the database during the call. (Holding it locked
            # SQLite for the whole request and made other requests fail.)
            with manager.guard(context, connected='read'):
                pass
            yield
            with manager.guard(context, connected='read'):
                pass

        def semantic(query):
            from src.email_search import semantic_candidates
            return semantic_candidates(query, context.account_id, search_collection())

        def save(**fields):
            fields['updated_at'] = utc_timestamp()
            assignments = ','.join(f'{key}=?' for key in fields)
            with manager.guard(context, connected='read') as conn:
                conn.execute(f'UPDATE inbox_questions SET {assignments} WHERE question_id=? AND account_id=?',
                             (*fields.values(), question_id, context.account_id))

        recorder = TokenRecorder(context.account_id,
                                 usage_request_prefix('inbox-question', context.account_id, secrets.token_hex(16)),
                                 db_path=settings.db_path, enabled=settings.token_collection_enabled)
        tokens = {'total': 0}
        original_record = recorder.record

        def record(*args, **kwargs):
            measurement = kwargs.get('measurement')
            if measurement is not None and isinstance(measurement.total_tokens, int):
                tokens['total'] += measurement.total_tokens
            return original_record(*args, **kwargs)
        recorder.record = record
        steps = {'count': 0}

        def call(contents, remaining):
            from src.llm_api import call_structured
            steps['count'] += 1
            value, _source, _model = call_structured(
                contents, system=SYSTEM_INSTRUCTION, schema=OUTPUT_SCHEMA, schema_name='mailmind_inbox_agent',
                settings=settings, operation=TokenOperation.INBOX_QUESTION.value, validate=validate_step,
                usage_recorder=recorder, before_request=fenced, budget_seconds=max(3, min(remaining, 20)),
                max_output_tokens=1536, record_key=f'step-{steps["count"]}', max_chars=16000, salvage=salvage_step)
            return value

        started = time.monotonic()
        try:
            status, answer, agent = ask(
                question, tools=Tools(read_db, context.account_id, semantic=semantic,
                                      ledger_enabled=settings.ledger_enabled, risk_enabled=settings.risk_analysis_enabled),
                call=call, on_step=lambda trace: save(trace_json=json.dumps(trace)), budget_seconds=BUDGET_SECONDS)
            save(status=status, answer_json=json.dumps(answer, ensure_ascii=False), trace_json=json.dumps(agent.trace),
                 total_tokens=tokens['total'], latency_ms=round((time.monotonic() - started) * 1000))
        except (WorkCancelled, AccessDenied):
            try:
                save(status='cancelled', error_code='account_changed')
            except (WorkCancelled, AccessDenied):
                pass
        except Exception as error:
            log_event('inbox_question_failed', error=error)
            code = getattr(error, 'code', 'question_failed')
            try:
                save(status='failed', error_code=str(code)[:80], total_tokens=tokens['total'],
                     latency_ms=round((time.monotonic() - started) * 1000))
            except (WorkCancelled, AccessDenied):
                pass

    @application.post('/questions', status_code=202)
    def create_question(payload: QuestionRequest, context=Depends(session)):
        enabled()
        settings = application.state.settings
        if settings.local_only:
            raise HTTPException(403, 'Ask your inbox needs an AI provider and is off in local-only mode.')
        with application.state.accounts.guard(context, connected='read') as conn:
            try:
                claim_mutation_slot(context.account_id, 'question', 'ask', cooldown_seconds=QUESTION_COOLDOWN_SECONDS,
                                    db_conn=conn)
            except MutationRateLimited:
                raise HTTPException(429, 'One question at a time: wait a few seconds and ask again.') from None
            # A row older than the time budget was cut off (a restart): close it.
            stale = utc_timestamp(datetime.now(timezone.utc) - timedelta(seconds=BUDGET_SECONDS + 30))
            conn.execute("""UPDATE inbox_questions SET status='failed',error_code='interrupted',updated_at=?
                            WHERE account_id=? AND status='running' AND created_at<?""",
                         (utc_timestamp(), context.account_id, stale))
            running = conn.execute("SELECT COUNT(*) FROM inbox_questions WHERE account_id=? AND status='running'",
                                   (context.account_id,)).fetchone()[0]
            if running:
                raise HTTPException(409, 'MailMind is still answering your last question.')
            stamp = utc_timestamp()
            question_id = conn.execute(
                """INSERT INTO inbox_questions(account_id,generation,question,status,created_at,updated_at)
                   VALUES (?,?,?,'running',?,?)""",
                (context.account_id, context.generation, payload.question, stamp, stamp)).lastrowid
        try:
            jobs.submit(lambda: run_question(context, question_id, payload.question))
        except RuntimeError:
            with application.state.accounts.guard(context, connected='read') as conn:
                conn.execute("UPDATE inbox_questions SET status='failed',error_code='runner_busy',updated_at=? "
                             'WHERE question_id=?', (utc_timestamp(), question_id))
            raise HTTPException(503, 'MailMind is busy answering other questions. Try again shortly.') from None
        return {'question_id': question_id, 'status': 'running', 'message': 'Searching your saved mail...'}

    @application.get('/questions/{question_id}')
    def get_question(question_id: int, context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context, connected='read', readonly=True) as conn:
            row = conn.execute('SELECT * FROM inbox_questions WHERE question_id=? AND account_id=?',
                               (question_id, context.account_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'This question does not belong to the connected account.')
        return {'account_id': context.account_id, 'generation': context.generation, **public_question(row)}

    @application.get('/questions')
    def list_questions(limit: int = Query(HISTORY_LIMIT, ge=1, le=50), context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context, connected='read', readonly=True) as conn:
            rows = conn.execute('SELECT * FROM inbox_questions WHERE account_id=? ORDER BY question_id DESC LIMIT ?',
                                (context.account_id, limit)).fetchall()
        return {'account_id': context.account_id, 'generation': context.generation,
                'questions': [public_question(row) for row in rows]}

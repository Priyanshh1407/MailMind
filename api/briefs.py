"""Routes for the daily brief and its proposals (nothing runs until approved)."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import secrets
import time
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException

from src.account_state import AccessDenied, WorkCancelled
from src.background_jobs import BackgroundJobs
from src.chief_of_staff import fallback_brief, gather, multi_agent_brief
from src.database import utc_timestamp
from src.intelligence_contract import DEFAULT_TIMEZONE, TokenOperation
from src.intelligence_safety import MutationRateLimited, claim_mutation_slot
from src.logging_utils import log_event
from src.token_usage import TokenRecorder, usage_request_prefix

REGENERATE_COOLDOWN_SECONDS = 60
STALE_SECONDS = 180


def local_today():
    return datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date().isoformat()


def public_brief(row):
    return {'brief_id': row['brief_id'], 'brief_date': row['brief_date'], 'status': row['status'],
            'brief': json.loads(row['brief_json']) if row['brief_json'] else None,
            'trace': json.loads(row['trace_json']), 'total_tokens': row['total_tokens'], 'latency_ms': row['latency_ms'],
            'error_code': row['error_code'], 'created_at': row['created_at'], 'updated_at': row['updated_at']}


def register(application, session):
    jobs = BackgroundJobs(max_workers=1)

    def enabled():
        if not application.state.settings.daily_brief_enabled:
            raise HTTPException(404, 'The daily brief is switched off (MAILMIND_DAILY_BRIEF_ENABLED).')

    def due(settings):
        hour, minute = (int(part) for part in settings.daily_brief_time.split(':'))
        now = datetime.now(ZoneInfo(DEFAULT_TIMEZONE))
        return (now.hour, now.minute) >= (hour, minute)

    def run(context, brief_id):
        manager, settings = application.state.accounts, application.state.settings

        @contextmanager
        def read_db():
            with manager.guard(context, connected='read') as conn:
                yield conn

        @contextmanager
        def fenced():
            with manager.guard(context, connected='read'):
                pass
            yield

        tokens = {'total': 0}
        recorder = TokenRecorder(context.account_id, usage_request_prefix('brief', context.account_id, secrets.token_hex(16)),
                                 db_path=settings.db_path, enabled=settings.token_collection_enabled)
        original = recorder.record

        def record(*args, **kwargs):
            measurement = kwargs.get('measurement')
            if measurement is not None and isinstance(measurement.total_tokens, int):
                tokens['total'] += measurement.total_tokens
            return original(*args, **kwargs)
        recorder.record = record
        steps = {'n': 0}

        def call(system, schema, name, contents):
            from src.llm_api import call_structured
            steps['n'] += 1
            value, _source, _model = call_structured(
                contents, system=system, schema=schema, schema_name=name, settings=settings,
                operation=TokenOperation.DAILY_BRIEF.value, validate=lambda value: _check(value, schema),
                usage_recorder=recorder, before_request=fenced, budget_seconds=20, max_output_tokens=1536,
                record_key=f'brief-{steps["n"]}', max_chars=16000)
            return value

        def save(**fields):
            fields['updated_at'] = utc_timestamp()
            with read_db() as conn:
                conn.execute(f'UPDATE daily_briefs SET {",".join(f"{key}=?" for key in fields)} WHERE brief_id=? AND account_id=?',
                             (*fields.values(), brief_id, context.account_id))

        started = time.monotonic()
        try:
            with read_db() as conn:
                candidates = gather(conn, context.account_id, ledger=settings.ledger_enabled,
                                    risk=settings.risk_analysis_enabled, follow_ups=settings.follow_ups_enabled)
            try:
                if settings.local_only:
                    raise RuntimeError('cloud_disabled_local_only')
                brief, agent_run = multi_agent_brief(candidates, read_db, context.account_id, call=call)
                status, trace = ('ready' if brief['sections'] or brief['mode'] == 'empty' else 'partial'), agent_run.trace
                if not brief['sections'] and brief['mode'] != 'empty':
                    fallback = fallback_brief(candidates, 'no_verified_sentences')
                    brief = {**fallback, 'proposals': brief['proposals']}
            except (WorkCancelled, AccessDenied):
                raise
            except Exception as error:
                log_event('brief_agents_unavailable', error=error)
                brief, status, trace = fallback_brief(candidates, getattr(error, 'code', None) or str(error)[:60]), 'fallback', []
            save(status=status, brief_json=json.dumps(brief, ensure_ascii=False), trace_json=json.dumps(trace),
                 total_tokens=tokens['total'], latency_ms=round((time.monotonic() - started) * 1000))
            if settings.brief_telegram_enabled and not settings.local_only and status != 'failed':
                _telegram_summary(brief, settings)
        except (WorkCancelled, AccessDenied):
            try:
                save(status='failed', error_code='account_changed')
            except (WorkCancelled, AccessDenied):
                pass
        except Exception as error:
            log_event('brief_failed', error=error)
            try:
                save(status='failed', error_code='brief_failed')
            except (WorkCancelled, AccessDenied):
                pass

    def start(context, conn, force=False):
        today = local_today()
        stale = utc_timestamp(datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS))
        conn.execute("UPDATE daily_briefs SET status='failed',error_code='interrupted',updated_at=? "
                     "WHERE account_id=? AND status='running' AND created_at<?", (utc_timestamp(), context.account_id, stale))
        if conn.execute("SELECT 1 FROM daily_briefs WHERE account_id=? AND status='running'", (context.account_id,)).fetchone():
            return None
        if not force and conn.execute('SELECT 1 FROM daily_briefs WHERE account_id=? AND brief_date=?',
                                      (context.account_id, today)).fetchone():
            return None
        stamp = utc_timestamp()
        brief_id = conn.execute("""INSERT INTO daily_briefs(account_id,brief_date,status,created_at,updated_at)
                                   VALUES (?,?,'running',?,?)""", (context.account_id, today, stamp, stamp)).lastrowid
        return brief_id

    def launch(context, brief_id):
        try:
            jobs.submit(lambda: run(context, brief_id))
        except RuntimeError:
            with application.state.accounts.guard(context, connected='read') as conn:
                conn.execute("UPDATE daily_briefs SET status='failed',error_code='runner_busy' WHERE brief_id=?", (brief_id,))

    @application.get('/briefs/today')
    def today(context=Depends(session)):
        enabled()
        settings = application.state.settings
        brief_id = None
        with application.state.accounts.guard(context, connected='read') as conn:
            if due(settings) and context.account_id:
                brief_id = start(context, conn)
            row = conn.execute('SELECT * FROM daily_briefs WHERE account_id=? ORDER BY brief_id DESC LIMIT 1',
                               (context.account_id,)).fetchone()
        if brief_id:
            launch(context, brief_id)
        return {'account_id': context.account_id, 'generation': context.generation,
                'scheduled_time': settings.daily_brief_time, 'brief': public_brief(row) if row else None}

    @application.post('/briefs', status_code=202)
    def regenerate(context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context, connected='read') as conn:
            try:
                claim_mutation_slot(context.account_id, 'brief', 'regenerate', cooldown_seconds=REGENERATE_COOLDOWN_SECONDS, db_conn=conn)
            except MutationRateLimited:
                raise HTTPException(429, 'A brief was made moments ago. Try again in a minute.') from None
            brief_id = start(context, conn, force=True)
        if brief_id is None:
            raise HTTPException(409, 'A brief is already being written.')
        launch(context, brief_id)
        return {'brief_id': brief_id, 'status': 'running', 'message': 'Writing your brief...'}

    @application.post('/briefs/{brief_id}/proposals/{index}/{decision}')
    def decide(brief_id: int, index: int, decision: str, context=Depends(session)):
        # A proposal runs through the existing action and scam-review paths,
        # and only when the user approves it here.
        enabled()
        if decision not in ('approve', 'dismiss'):
            raise HTTPException(404, 'Unknown decision.')
        from src.action_center import ActionRevisionConflict, ActionTransitionError, get_action, update_action_status
        from src.email_risk import mark_reviewed
        with application.state.accounts.guard(context) as conn:
            row = conn.execute('SELECT * FROM daily_briefs WHERE brief_id=? AND account_id=?',
                               (brief_id, context.account_id)).fetchone()
            if row is None or not row['brief_json']:
                raise HTTPException(404, 'This brief does not belong to the connected account.')
            brief = json.loads(row['brief_json'])
            proposals = brief.get('proposals') or []
            if not 0 <= index < len(proposals) or proposals[index]['state'] != 'proposed':
                raise HTTPException(409, 'This proposal was already decided.')
            proposal = proposals[index]
            result = {}
            if decision == 'approve':
                kind = proposal['type']
                if kind in ('complete_action', 'snooze_action'):
                    action = get_action(context.account_id, proposal['action_id'], db_conn=conn)
                    if action is None:
                        raise HTTPException(409, 'That task no longer exists.')
                    try:
                        if kind == 'complete_action':
                            update_action_status(context.account_id, proposal['action_id'], status='completed',
                                                 expected_revision=action['revision'], db_conn=conn)
                        else:
                            update_action_status(context.account_id, proposal['action_id'], status='snoozed',
                                                 snoozed_until=datetime.now(timezone.utc) + timedelta(days=1),
                                                 expected_revision=action['revision'], db_conn=conn)
                    except (ActionRevisionConflict, ActionTransitionError, LookupError):
                        raise HTTPException(409, 'That task changed since the brief was written.') from None
                elif kind == 'mark_risky_reviewed':
                    mark_reviewed(conn, context.account_id, proposal['email_id'])
                elif kind == 'draft_follow_up':
                    result = {'open': 'follow-ups', 'message_id': proposal['message_id']}
            proposal['state'] = 'approved' if decision == 'approve' else 'dismissed'
            conn.execute('UPDATE daily_briefs SET brief_json=?,updated_at=? WHERE brief_id=?',
                         (json.dumps(brief, ensure_ascii=False), utc_timestamp(), brief_id))
        return {'brief_id': brief_id, 'index': index, 'state': proposal['state'], **result,
                'message': 'Done.' if decision == 'approve' else 'Dismissed.'}


def _check(value, schema):
    if not isinstance(value, dict) or set(value) != set(schema['required']):
        raise ValueError('Invalid brief step')
    return value


def _telegram_summary(brief, settings):
    """A redacted count-only summary: no email text leaves for Telegram."""
    from src.notifier import send_telegram_alert
    counts = brief.get('counts') or {}
    parts = [f'{counts.get("overdue", 0)} overdue', f'{counts.get("action", 0)} due this week',
             f'{counts.get("risk", 0)} possible scams', f'{counts.get("waiting", 0)} waiting for replies']
    try:
        send_telegram_alert('MailMind', 'Your daily brief is ready', ', '.join(parts) + '. Open MailMind to read it.',
                            settings=settings)
    except Exception as error:
        log_event('brief_telegram_failed', error=error)

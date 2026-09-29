'''Durable mutation gates and privacy-safe intelligence diagnostics.'''
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import math
import time

from .action_center import action_summary
from .database import connection
from .token_usage import aggregate_token_usage


MUTATION_SCOPES = frozenset({'reminder', 'reanalysis'})


class MutationRateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(math.ceil(retry_after)))
        super().__init__('Intelligence mutation rate limit exceeded')


def _database(db_conn, db_path):
    return nullcontext(db_conn) if db_conn is not None else connection(db_path)


def claim_mutation_slot(account_id, scope, resource_id, *,
                        cooldown_seconds, now=None, db_path=None,
                        db_conn=None):
    if not isinstance(account_id, str) or not account_id:
        raise ValueError('account_id is required')
    if scope not in MUTATION_SCOPES:
        raise ValueError('Invalid mutation scope')
    if not isinstance(resource_id, str) or not 1 <= len(resource_id) <= 256:
        raise ValueError('Invalid mutation resource')
    if (type(cooldown_seconds) is not int
            or not 1 <= cooldown_seconds <= 86400):
        raise ValueError('Invalid mutation cooldown')
    current = time.time() if now is None else now
    if not isinstance(current, (int, float)) or not math.isfinite(current):
        raise ValueError('Invalid mutation time')
    current = float(current)
    resource_hash = hashlib.sha256(resource_id.encode('utf-8')).hexdigest()
    with _database(db_conn, db_path) as conn:
        changed = conn.execute(
            '''INSERT INTO intelligence_mutation_limits(
                   account_id,scope,resource_hash,last_at)
               VALUES (?,?,?,?)
               ON CONFLICT(account_id,scope,resource_hash) DO UPDATE
               SET last_at=excluded.last_at
               WHERE intelligence_mutation_limits.last_at<=?''',
            (account_id, scope, resource_hash, current,
             current - cooldown_seconds),
        ).rowcount
        if changed:
            return {
                'scope': scope, 'resource_hash': resource_hash,
                'last_at': current,
            }
        previous = conn.execute(
            '''SELECT last_at FROM intelligence_mutation_limits
               WHERE account_id=? AND scope=? AND resource_hash=?''',
            (account_id, scope, resource_hash),
        ).fetchone()
        retry_after = (
            previous['last_at'] + cooldown_seconds - current
            if previous is not None else cooldown_seconds
        )
        raise MutationRateLimited(retry_after)


def intelligence_diagnostics(account_id, *, timezone_name, now=None,
                             db_path=None, db_conn=None):
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError('Diagnostic time must include a timezone')
    with _database(db_conn, db_path) as conn:
        actions = action_summary(
            account_id, now=current, db_conn=conn)
        usage = aggregate_token_usage(
            account_id, window='day', timezone_name=timezone_name,
            now=current, db_conn=conn)
    return {
        'actions': {
            'open': actions['status_counts']['open'],
            'due_soon': actions['due_soon'],
            'overdue': actions['overdue'],
        },
        'reminders': {
            key: actions['reminder_counts'][key]
            for key in ('scheduled', 'retry', 'dead')
        },
        'tokens': {
            'window': usage['window'],
            'timezone': usage['timezone'],
            'start_at': usage['start_at'],
            'end_at': usage['end_at'],
            'totals': usage['totals'],
            'provider_billed_tokens': usage['provider_billed_tokens'],
            'local_processed_tokens': usage['local_processed_tokens'],
            'providers': usage['providers'],
            'operations': usage['operations'],
            'count_methods': usage['count_methods'],
            'outcomes': usage['outcomes'],
        },
    }

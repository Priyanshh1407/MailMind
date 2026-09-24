"""Print a privacy-safe snapshot of the live MailMind worker state."""
import hashlib
import json
import os
import time

from src.config import Settings
from src.database import connection


def snapshot():
    settings = Settings.from_environment(load_file=True)
    with connection(settings.db_path) as conn:
        state = conn.execute(
            "SELECT account_id,generation,connected,auth_in_progress,purge_pending,"
            "is_polling,worker_token,lease_until,heartbeat_at,last_success_at,"
            "last_error_at,last_error_code FROM runtime_state WHERE singleton=1"
        ).fetchone()
        account_id = state['account_id'] if state else None
        task_rows = conn.execute(
            "SELECT status,stage,COALESCE(error_code,'none') AS error_code,COUNT(*) AS count "
            "FROM processing_tasks WHERE account_id=? GROUP BY status,stage,error_code "
            "ORDER BY status,stage,error_code", (account_id,)
        ).fetchall() if account_id else []
        due = conn.execute(
            "SELECT COUNT(*) FROM processing_tasks WHERE account_id=? "
            "AND status IN ('queued','retry') AND next_retry_at<=?", (account_id,time.time())
        ).fetchone()[0] if account_id else 0
        ingestion = conn.execute(
            "SELECT status,last_checked_at,has_more,listing_error FROM ingestion_state "
            "WHERE account_id=?", (account_id,)
        ).fetchone() if account_id else None
        health = conn.execute(
            "SELECT heartbeat_at,last_success_at,last_error_at,last_error_code "
            "FROM worker_health WHERE account_id=?", (account_id,)
        ).fetchone() if account_id else None
        search_index = {row['indexing_state']:row['count'] for row in conn.execute(
            'SELECT indexing_state,COUNT(*) AS count FROM email_search_index WHERE account_id=? GROUP BY indexing_state',
            (account_id,)).fetchall()} if account_id else {}
        schema_version = conn.execute('PRAGMA user_version').fetchone()[0]
    credentials_present = bool(account_id and settings.data_dir.joinpath(
        'oauth', hashlib.sha256(account_id.encode()).hexdigest()+'.json').is_file())
    return {
        'mode': 'local_only' if settings.local_only else 'normal',
        'schema_version': schema_version,
        'runtime': None if state is None else {
            'account_selected': bool(account_id),
            'connected': bool(state['connected']),
            'auth_in_progress': bool(state['auth_in_progress']),
            'purge_pending': bool(state['purge_pending']),
            'cycle_running': bool(state['is_polling']),
            'lease_active': bool(state['worker_token'] and state['lease_until'] > time.time()),
            'heartbeat_at': state['heartbeat_at'],
            'last_success_at': state['last_success_at'],
            'last_error_at': state['last_error_at'],
            'last_error_code': state['last_error_code'],
        },
        'credentials_present': credentials_present,
        'providers_configured': {
            'gemini': bool(os.getenv('GEMINI_API_KEY')),
            'groq': bool(os.getenv('GROQ_API_KEY')),
            'telegram': bool(os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID')),
        },
        'ingestion': dict(ingestion) if ingestion else None,
        'worker_health': dict(health) if health else None,
        'due_tasks': due,
        'task_groups': [dict(row) for row in task_rows],
        'semantic_search_index': {key:search_index.get(key,0) for key in ('pending','indexed','failed')},
    }


if __name__ == '__main__':
    print(json.dumps(snapshot(),indent=2))

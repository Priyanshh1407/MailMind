'''Trim only excess queued copies; original messages remain in Gmail.'''
import json

from src.config import Settings
from src.database import connection


def enforce():
    settings=Settings.from_environment(load_file=True)
    with connection(settings.db_path) as conn:
        state=conn.execute(
            'SELECT account_id,connected,is_polling FROM runtime_state WHERE singleton=1'
        ).fetchone()
        if not state or state['connected'] or state['is_polling']:
            raise RuntimeError('Disconnect or stop inbox processing before trimming the queue')
        account_id=state['account_id']
        if not account_id:
            return {'removed':0,'pending':0,'limit':settings.max_pending_tasks}
        conn.execute('''UPDATE processing_tasks SET status='queued',owner_token=NULL,
            error_code=NULL,next_retry_at=0 WHERE account_id=? AND status='running'
            AND stage='classify' ''',(account_id,))
        pending=conn.execute('''SELECT COUNT(*) FROM processing_tasks
            WHERE account_id=? AND stage<>'complete' ''',(account_id,)).fetchone()[0]
        excess=max(0,pending-settings.max_pending_tasks)
        rows=conn.execute('''SELECT email_id FROM processing_tasks
            WHERE account_id=? AND status='queued' ORDER BY created_at,email_id
            LIMIT ?''',(account_id,excess)).fetchall()
        conn.executemany('DELETE FROM email_logs WHERE account_id=? AND email_id=?',
                         ((account_id,row['email_id']) for row in rows))
        conn.execute('UPDATE ingestion_state SET page_token=NULL WHERE account_id=?',
                     (account_id,))
        remaining=conn.execute('''SELECT COUNT(*) FROM processing_tasks
            WHERE account_id=? AND stage<>'complete' ''',(account_id,)).fetchone()[0]
    return {'removed':len(rows),'pending':remaining,'limit':settings.max_pending_tasks}


if __name__ == '__main__':
    print(json.dumps(enforce(),indent=2))

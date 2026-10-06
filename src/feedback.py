"""SQLite is authoritative; the vector store is a retryable derived index."""
from threading import Event

from .database import connection
from .vector_db import add_email_to_vector_db
from .logging_utils import log_event
from .email_text import format_email_text
from .intelligence_contract import TokenOperation, TokenOutcome
from .token_usage import (
    EMBEDDING_MODEL_VERSION,
    TokenRecorder,
    estimate_text_tokens,
    usage_request_prefix,
)

def current_vector(metadata, db_path):
    with connection(db_path) as conn:
        row = conn.execute('''SELECT revision_id,label,indexing_state FROM feedback_history
            WHERE account_id=? AND email_id=? ORDER BY revision_id DESC LIMIT 1''',
            (metadata.get('account_id'), metadata.get('email_id'))).fetchone()
        return bool(row and row['label'] is not None and row['indexing_state'] == 'indexed' and row['revision_id'] == metadata.get('revision_id') and row['label'] == metadata.get('label'))


class CurrentFeedback:
    """Checks that a retrieved correction is still your current label, and
    finds who sent the corrected email. Both read SQLite, the source of truth,
    so retrieved vector text alone can never act as a correction."""

    def __init__(self, db_path, account_id):
        self.db_path = db_path
        self.account_id = account_id

    def __call__(self, metadata):
        return current_vector(metadata, self.db_path)

    def sender_of(self, email_id):
        with connection(self.db_path) as conn:
            row = conn.execute('SELECT sender FROM email_logs WHERE account_id=? AND email_id=?',
                               (self.account_id, email_id)).fetchone()
        return row['sender'] if row else None

def reconcile_feedback(manager, context, collection_provider, limit=20, *, max_attempts=None):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError('Reconciliation limit must be between 1 and 50')
    if max_attempts is not None and (not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 50):
        raise ValueError('Maximum reconciliation attempts must be between 1 and 50')
    attempt_filter = '' if max_attempts is None else ' AND f.attempt_count<?'
    parameters = (context.account_id, *((max_attempts,) if max_attempts is not None else ()), limit)
    with manager.guard(context) as conn:
        rows = conn.execute('''SELECT f.*,e.subject,e.body FROM feedback_history f
            JOIN email_logs e USING(account_id,email_id)
            WHERE f.account_id=? AND f.indexing_state!='indexed'
            AND f.revision_id=(SELECT MAX(revision_id) FROM feedback_history x
                WHERE x.account_id=f.account_id AND x.email_id=f.email_id)
            ''' + attempt_filter + '''
            ORDER BY f.revision_id LIMIT ?''', parameters).fetchall()
    indexed, failed = 0, 0
    from .vector_lock import vector_write_lock
    from .provider_policy import INDEX_CALLS
    from .account_state import WorkCancelled, AccessDenied
    for row in rows:
        state='indexed'
        embedding_attempted=Event()
        measurement=(estimate_text_tokens(
            format_email_text(row['subject'],row['body']))
            if row['label'] is not None else None)
        usage_recorder=TokenRecorder(
            context.account_id,
            usage_request_prefix(
                'feedback-index', context.account_id, row['email_id'],
                row['revision_id'], row['attempt_count']),
            email_id=row['email_id'], db_path=manager.settings.db_path,
            enabled=manager.settings.token_collection_enabled,
        )
        def index(row=row):
            # Keep the write barrier until the ACTUAL write ends, even if waiting
            # times out. Purge stays pending instead of racing a late upsert.
            with vector_write_lock(manager.settings.data_dir):
                with manager.guard(context) as conn:
                    latest=conn.execute('SELECT revision_id,indexing_state FROM feedback_history WHERE account_id=? AND email_id=? ORDER BY revision_id DESC LIMIT 1',(context.account_id,row['email_id'])).fetchone()
                    if not latest or latest['revision_id'] != row['revision_id'] or latest['indexing_state'] == 'indexed':
                        return False
                if row['label'] is None:
                    from hashlib import sha256
                    identity=sha256((context.account_id+'\0'+row['email_id']).encode()).hexdigest()
                    collection_provider().delete(ids=[identity])
                else:
                    embedding_attempted.set()
                    add_email_to_vector_db(row['email_id'],row['subject'],row['body'],row['label'],account_id=context.account_id,
                        collection=collection_provider(),revision_id=row['revision_id'])
                return True
        try:
            changed=INDEX_CALLS.run(index,2)
            if not changed:
                continue
            if embedding_attempted.is_set():
                usage_recorder.record(
                    'document', provider='embedding',
                    model_version=EMBEDDING_MODEL_VERSION,
                    operation=TokenOperation.DOCUMENT_EMBEDDING.value,
                    outcome=TokenOutcome.SUCCESS.value,
                    measurement=measurement,
                )
        except (WorkCancelled,AccessDenied):
            if embedding_attempted.is_set():
                usage_recorder.record(
                    'document', provider='embedding',
                    model_version=EMBEDDING_MODEL_VERSION,
                    operation=TokenOperation.DOCUMENT_EMBEDDING.value,
                    outcome=TokenOutcome.CANCELLED.value,
                    measurement=measurement,
                )
            raise
        except Exception as error:
            if embedding_attempted.is_set():
                usage_recorder.record(
                    'document', provider='embedding',
                    model_version=EMBEDDING_MODEL_VERSION,
                    operation=TokenOperation.DOCUMENT_EMBEDDING.value,
                    outcome=(TokenOutcome.TIMEOUT.value
                             if isinstance(error, TimeoutError)
                             else TokenOutcome.FAILED.value),
                    measurement=measurement,
                )
            state='failed'
            failed+=1
            log_event('feedback_index_failed',error=error)
        with manager.guard(context) as conn:
            latest=conn.execute('SELECT MAX(revision_id) FROM feedback_history WHERE account_id=? AND email_id=?',(context.account_id,row['email_id'])).fetchone()[0]
            if latest == row['revision_id']:
                conn.execute('UPDATE feedback_history SET indexing_state=?,attempt_count=attempt_count+1 WHERE revision_id=?',(state,row['revision_id']))
                if state == 'indexed':
                    indexed+=1
    return {'indexed':indexed,'failed':failed}

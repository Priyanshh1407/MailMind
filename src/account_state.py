"""Shared local sessions and account lifecycle; SQLite coordinates both processes."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import os
import secrets
import time

from .config import LEGACY_ACCOUNT
from .database import connection, utc_timestamp
from .vector_lock import vector_write_lock


class AccessDenied(Exception):
    pass


class WorkCancelled(Exception):
    pass


class TransitionBusy(Exception):
    pass


@dataclass(frozen=True)
class AccountContext:
    account_id: str | None
    generation: int
    session_hash: str | None = None
    restore_connected: bool = False


class AccountManager:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def transaction(self):
        with connection(self.settings.db_path, self.settings.busy_timeout_ms) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def state(conn):
        return dict(conn.execute("SELECT * FROM runtime_state WHERE singleton=1").fetchone())

    @staticmethod
    def _cancel(conn):
        conn.execute('UPDATE runtime_state SET worker_token=NULL,lease_until=0')
        conn.execute("UPDATE auth_jobs SET status='cancelled',updated_at=? WHERE status='running'",(utc_timestamp(),))
        conn.execute("UPDATE worker_jobs SET status='cancelled',updated_at=? WHERE account_id=(SELECT account_id FROM runtime_state WHERE singleton=1) AND status IN ('queued','running')", (utc_timestamp(),))
        conn.execute('UPDATE ingestion_state SET page_token=NULL WHERE account_id=(SELECT account_id FROM runtime_state WHERE singleton=1)')

    def restart(self):
        # A new API owner invalidates old browser sessions. Saved mail and credentials stay intact.
        with self.transaction() as conn:
            conn.execute("DELETE FROM local_sessions")
            # Transient health belongs to the previous process lifetime. Durable
            # worker_jobs still retain its audited failures for troubleshooting.
            conn.execute("UPDATE worker_health SET last_error_at=NULL,last_error_code=NULL WHERE account_id=(SELECT account_id FROM runtime_state WHERE singleton=1)")
            conn.execute("""UPDATE runtime_state SET generation=generation+1,connected=0,
                auth_in_progress=0,is_polling=0,last_error_at=NULL,last_error_code=NULL""")
            self._cancel(conn)

    def open_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as conn:
            state = self.state(conn)
            conn.execute("DELETE FROM local_sessions WHERE expires_at<=?", (time.time(),))
            conn.execute("INSERT INTO local_sessions VALUES (?,?,?,?)", (self.digest(token), csrf, time.time()+43200, state['generation']))
        return token, csrf

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token, csrf=None):
        if not token:
            raise AccessDenied()
        with self.transaction() as conn:
            state = self.state(conn)
            key = self.digest(token)
            row = conn.execute("SELECT * FROM local_sessions WHERE token_hash=?", (key,)).fetchone()
            if not row or row['expires_at'] <= time.time() or row['generation'] != state['generation']:
                raise AccessDenied()
            if csrf is not None and not hmac.compare_digest(csrf.encode('utf-8'), row['csrf'].encode('utf-8')):
                raise AccessDenied()
            return AccountContext(state['account_id'], state['generation'], key), row['csrf']

    @contextmanager
    def guard(self, context, *, connected=True):
        # Hold this lock through a side effect. A successful stop cannot race a
        # later effect. An already started external request cannot be undone.
        with self.transaction() as conn:
            state = self.state(conn)
            if state['generation'] != context.generation or state['account_id'] != context.account_id:
                raise WorkCancelled()
            if context.session_hash:
                row = conn.execute("SELECT expires_at FROM local_sessions WHERE token_hash=? AND generation=?", (context.session_hash, context.generation)).fetchone()
                if not row or row[0] <= time.time():
                    raise AccessDenied()
            if connected and (not state['connected'] or state['purge_pending'] or state['auth_in_progress']):
                raise WorkCancelled()
            yield conn

    def worker_context(self):
        with self.transaction() as conn:
            state = self.state(conn)
            if not state['connected'] or state['auth_in_progress'] or state['purge_pending']:
                return None
            return AccountContext(state['account_id'], state['generation'])

    def indexer_context(self):
        """Derived-index work is allowed only for a connected Google account."""
        with self.transaction() as conn:
            state = self.state(conn)
            if (not state['connected'] or not state['account_id'] or state['account_id'] == LEGACY_ACCOUNT
                    or state['auth_in_progress'] or state['purge_pending']):
                return None
            return AccountContext(state['account_id'], state['generation'])

    @contextmanager
    def external(self, context, *, connected=True):
        """Check before/after slow work without holding a SQLite write lock.

        In-flight requests cannot be undone. Late results cannot commit or start
        another operation after the generation changes.
        """
        with self.guard(context, connected=connected):
            pass
        yield
        with self.guard(context, connected=connected):
            pass

    def _transition(self, context, *, keep_session=True, auth=False, purge=False):
        with self.guard(context, connected=False) as conn:
            state = self.state(conn)
            if auth and (state['auth_in_progress'] or state['purge_pending']):
                raise TransitionBusy()
            generation = state['generation'] + 1
            conn.execute("UPDATE runtime_state SET generation=?,connected=0,auth_in_progress=?,purge_pending=?,is_polling=0", (generation, int(auth), int(purge or state['purge_pending'])))
            self._cancel(conn)
            conn.execute("DELETE FROM local_sessions WHERE token_hash!=?", (context.session_hash or '',))
            if keep_session and context.session_hash:
                conn.execute("UPDATE local_sessions SET generation=? WHERE token_hash=?", (generation, context.session_hash))
            else:
                conn.execute("DELETE FROM local_sessions")
            return AccountContext(context.account_id, generation, context.session_hash if keep_session else None)

    def logout(self, context):
        self._transition(context, keep_session=False)

    def pause(self, context):
        # A background credential failure pauses Google work without signing
        # the owner out of the local page. Only explicit OAuth can resume it.
        with self.guard(context) as conn:
            conn.execute('UPDATE runtime_state SET generation=generation+1,connected=0,is_polling=0')
            conn.execute('UPDATE local_sessions SET generation=generation+1')
            self._cancel(conn)

    def credential_path(self, account_id):
        if not account_id or account_id == LEGACY_ACCOUNT:
            raise ValueError("A verified Google account is required")
        return self.settings.data_dir / 'oauth' / (self.digest(account_id) + '.json')

    def write_credentials(self, context, value):
        path = self.credential_path(context.account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.'+secrets.token_hex(8)+'.tmp')
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(value)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def disconnect(self, context):
        context = self._transition(context)
        with self.guard(context, connected=False):
            if context.account_id:
                self.credential_path(context.account_id).unlink(missing_ok=True)
        return context

    def begin_auth(self, context):
        with self.transaction() as conn:
            state = self.state(conn)
            restore_connected = bool(state['connected'])
        attempt = self._transition(context, auth=True)
        return AccountContext(attempt.account_id, attempt.generation, attempt.session_hash, restore_connected)

    def finish_auth(self, context, candidate):
        account_id, credential_json = candidate
        if not account_id or '@' not in account_id or account_id == LEGACY_ACCOUNT:
            raise ValueError("Google did not return a verified account")
        with self.guard(context, connected=False) as conn:
            if not self.state(conn)['auth_in_progress']:
                raise WorkCancelled()
            new_context = AccountContext(account_id, context.generation, context.session_hash)
            self.write_credentials(new_context, credential_json)
            conn.execute("INSERT INTO accounts(account_id) VALUES (?) ON CONFLICT DO NOTHING", (account_id,))
            conn.execute("UPDATE runtime_state SET account_id=?,connected=1,auth_in_progress=0", (account_id,))
            return new_context

    def fail_auth(self, context):
        try:
            with self.guard(context, connected=False) as conn:
                state = self.state(conn)
                if state['auth_in_progress']:
                    conn.execute("UPDATE runtime_state SET connected=?,auth_in_progress=0",
                                 (int(context.restore_connected),))
        except (WorkCancelled, AccessDenied):
            pass

    def purge(self, context, collection, search_collection=None):
        if not context.account_id:
            raise ValueError("No account to delete")
        context = self._transition(context, purge=True)
        # Pending is committed first. Failure stays paused and can be retried.
        with vector_write_lock(self.settings.data_dir), self.guard(context, connected=False) as conn:
            target = collection() if callable(collection) else collection
            target.delete(where={"account_id": context.account_id})
            if search_collection is not None:
                search_target = search_collection() if callable(search_collection) else search_collection
                search_target.delete(where={'account_id': context.account_id})
            conn.execute("DELETE FROM action_reminders WHERE account_id=?", (context.account_id,))
            conn.execute("DELETE FROM email_actions WHERE account_id=?", (context.account_id,))
            conn.execute("DELETE FROM email_analysis WHERE account_id=?", (context.account_id,))
            conn.execute("DELETE FROM token_usage_events WHERE account_id=?", (context.account_id,))
            conn.execute("DELETE FROM email_logs WHERE account_id=?", (context.account_id,))
            conn.execute("DELETE FROM worker_jobs WHERE account_id=?", (context.account_id,))
            conn.execute("DELETE FROM ingestion_state WHERE account_id=?", (context.account_id,))
            conn.execute('DELETE FROM ingestion_failures WHERE account_id=?', (context.account_id,))
            conn.execute('DELETE FROM worker_health WHERE account_id=?',(context.account_id,))
            conn.execute(
                'DELETE FROM intelligence_mutation_limits WHERE account_id=?',
                (context.account_id,))
            conn.execute(
                'DELETE FROM intelligence_backfill_items WHERE account_id=?',
                (context.account_id,))
            self.credential_path(context.account_id).unlink(missing_ok=True)
            conn.execute("UPDATE runtime_state SET purge_pending=0")
        return context

    def purge_legacy(self, context, collection, search_collection=None):
        with vector_write_lock(self.settings.data_dir), self.guard(context, connected=False) as conn:
            records = collection.get(include=['metadatas'])
            ids = [key for key, meta in zip(records.get('ids', []), records.get('metadatas', [])) if not meta or meta.get('account_id') in (None, LEGACY_ACCOUNT)]
            if ids:
                collection.delete(ids=ids)
            if search_collection is not None:
                search_target = search_collection() if callable(search_collection) else search_collection
                search_target.delete(where={'account_id': LEGACY_ACCOUNT})
            conn.execute("DELETE FROM action_reminders WHERE account_id=?", (LEGACY_ACCOUNT,))
            conn.execute("DELETE FROM email_actions WHERE account_id=?", (LEGACY_ACCOUNT,))
            conn.execute("DELETE FROM email_analysis WHERE account_id=?", (LEGACY_ACCOUNT,))
            conn.execute("DELETE FROM token_usage_events WHERE account_id=?", (LEGACY_ACCOUNT,))
            conn.execute("DELETE FROM email_logs WHERE account_id=?", (LEGACY_ACCOUNT,))
            conn.execute("DELETE FROM worker_jobs WHERE account_id=?", (LEGACY_ACCOUNT,))
            conn.execute("DELETE FROM ingestion_state WHERE account_id=?", (LEGACY_ACCOUNT,))
            conn.execute('DELETE FROM ingestion_failures WHERE account_id=?', (LEGACY_ACCOUNT,))
            conn.execute("DROP TABLE IF EXISTS email_logs_legacy_v0")
            conn.execute(
                'DELETE FROM intelligence_mutation_limits WHERE account_id=?',
                (LEGACY_ACCOUNT,))
            conn.execute(
                'DELETE FROM intelligence_backfill_items WHERE account_id=?',
                (LEGACY_ACCOUNT,))
            self.settings.legacy_token_path.unlink(missing_ok=True)
            (self.settings.data_dir / 'user_profile.json').unlink(missing_ok=True)

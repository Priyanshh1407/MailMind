import contextlib
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.config import LEGACY_ACCOUNT
from src.database import connection, initialize_database, normalize_legacy_timestamp, utc_timestamp, _migration_1, _migration_2
from src.db_utils import get_recent_emails, log_email_to_db, update_human_label, clear_all_emails
from src.prediction import Prediction
from scripts.add_manual_email import insert_new_email


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mailmind-phase1-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "logs.db"

    def legacy(self, date="2026-09-16 10:00:00"):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""CREATE TABLE email_logs(email_id TEXT PRIMARY KEY,sender TEXT,subject TEXT,body TEXT,
                prediction TEXT,local_prediction TEXT,human_label TEXT,created_at TEXT,extra_legacy TEXT)""")
            conn.execute("INSERT INTO email_logs VALUES (?,?,?,?,?,?,?,?,?)",
                         ("synthetic-old",None,"Original subject","Synthetic body","IMPORTANT","CONNECTION_FAILED","IGNORE",date,"preserve this too"))
            conn.commit()

    def test_fresh_schema_and_pragmas(self):
        initialize_database(self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_legacy_archive_preserves_every_original_field(self):
        self.legacy()
        initialize_database(self.path)
        with connection(self.path) as conn:
            archive = dict(conn.execute("SELECT * FROM email_logs_legacy_v0").fetchone())
            self.assertIsNone(archive["sender"])
            self.assertEqual(archive["human_label"], "IGNORE")
            self.assertEqual(archive["extra_legacy"], "preserve this too")
            migrated = dict(conn.execute("SELECT * FROM email_logs").fetchone())
            self.assertEqual(migrated["account_id"], LEGACY_ACCOUNT)
            self.assertIsNone(migrated["human_label"])
            self.assertIsNone(migrated["local_prediction"])
            self.assertEqual(migrated["created_at"], "2026-09-16T10:00:00.000000Z")
        initialize_database(self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM prediction_attempts").fetchone()[0], 1)

    def test_bad_legacy_timestamp_rolls_back_without_losing_original(self):
        self.legacy("not-a-date")
        with self.assertRaises(ValueError):
            initialize_database(self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT created_at FROM email_logs").fetchone()[0], "not-a-date")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='accounts'").fetchone())

    def test_version_one_upgrades_and_seeds_history(self):
        with connection(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _migration_1(conn)
            conn.execute("PRAGMA user_version=1")
            conn.execute("INSERT INTO email_logs(email_id,human_label,created_at) VALUES ('synthetic-v1','UPDATES',?)", (utc_timestamp(),))
        initialize_database(self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT label FROM feedback_history").fetchone()[0], "UPDATES")

    def test_version_two_upgrades_without_changing_account_mail(self):
        with connection(self.path) as conn:
            _migration_1(conn)
            _migration_2(conn)
            conn.execute('PRAGMA user_version=2')
        with connection(self.path) as conn:
            conn.execute("INSERT INTO accounts VALUES ('a@example.test')")
            conn.execute("INSERT INTO email_logs(account_id,email_id,body,created_at) VALUES ('a@example.test','synthetic-v2','original v2 body',?)",(utc_timestamp(),))
        initialize_database(self.path)
        initialize_database(self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],7)
            self.assertEqual(conn.execute('SELECT account_id FROM email_logs').fetchone()[0],'a@example.test')
            self.assertEqual(conn.execute('SELECT connected FROM runtime_state').fetchone()[0],0)

    def test_future_version_is_rejected(self):
        with connection(self.path) as conn:
            conn.execute("PRAGMA user_version=99")
        with self.assertRaises(ValueError):
            initialize_database(self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_incomplete_versioned_database_is_rejected(self):
        with connection(self.path) as conn:
            conn.execute("PRAGMA user_version=4")
        with self.assertRaisesRegex(ValueError,'incomplete'):
            initialize_database(self.path)

    def test_older_partial_schema_is_preserved(self):
        with connection(self.path) as conn:
            conn.execute("CREATE TABLE email_logs(email_id TEXT PRIMARY KEY,subject TEXT,created_at TEXT)")
            conn.execute("INSERT INTO email_logs VALUES ('synthetic-partial','subject','2026-09-17 00:00:00')")
        initialize_database(self.path)
        self.assertEqual(get_recent_emails(db_path=self.path)[0]['subject'],'subject')
        with connection(self.path) as conn:
            self.assertEqual(len(conn.execute('PRAGMA table_info(email_logs_legacy_v0)').fetchall()),3)

    def test_missing_dates_preserve_raw_null_with_explicit_sort_sentinel(self):
        self.legacy(None)
        initialize_database(self.path)
        with connection(self.path) as conn:
            self.assertIsNone(conn.execute('SELECT created_at FROM email_logs_legacy_v0').fetchone()[0])
            self.assertEqual(conn.execute('SELECT created_at FROM email_logs').fetchone()[0],'1970-01-01T00:00:00.000000Z')

    def test_initial_feedback_is_recorded_in_history(self):
        initialize_database(self.path)
        log_email_to_db('synthetic','sender','subject','body','SPAM','SPAM',human_label='IMPORTANT',db_path=self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute('SELECT label FROM feedback_history').fetchone()[0],'IMPORTANT')

    def test_account_message_keys_do_not_collide(self):
        initialize_database(self.path)
        for account in ("account-a", "account-b"):
            log_email_to_db("same-id","synthetic",account,"body","IMPORTANT","SPAM",account_id=account,db_path=self.path)
        self.assertEqual(len(get_recent_emails(db_path=self.path)), 2)
        self.assertEqual(get_recent_emails(account_id="account-b",db_path=self.path)[0]["subject"], "account-b")

    def test_invalid_labels_rollback_entire_write(self):
        initialize_database(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            log_email_to_db("synthetic","sender","subject","body","IMPORTANT","SPAM",human_label="INVALID",account_id="bad-account",db_path=self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM email_logs").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT * FROM accounts WHERE account_id='bad-account'").fetchone())

    def test_closed_connection_after_success_and_failure(self):
        with connection(self.path) as conn:
            conn.execute("CREATE TABLE sample(value TEXT)")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        with self.assertRaises(RuntimeError):
            with connection(self.path) as failed:
                failed.execute("INSERT INTO sample VALUES ('must rollback')")
                raise RuntimeError("synthetic")
        with self.assertRaises(sqlite3.ProgrammingError):
            failed.execute("SELECT 1")
        with connection(self.path) as check:
            self.assertEqual(check.execute("SELECT count(*) FROM sample").fetchone()[0], 0)

    def test_busy_wait_is_bounded(self):
        initialize_database(self.path)
        with connection(self.path) as owner:
            owner.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                with connection(self.path, busy_timeout_ms=20) as other:
                    other.execute("BEGIN IMMEDIATE")

    def test_prediction_and_feedback_histories_remain_separate(self):
        initialize_database(self.path)
        args=("synthetic","sender","subject","body")
        log_email_to_db(*args,"SPAM","SPAM",db_path=self.path)
        update_human_label("synthetic","IMPORTANT",db_path=self.path)
        log_email_to_db(*args,"UPDATES","IMPORTANT",db_path=self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT prediction,human_label FROM email_logs").fetchone()[:], ("SPAM","IMPORTANT"))
            self.assertEqual(conn.execute("SELECT count(*) FROM prediction_attempts").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM feedback_history").fetchone()[0], 1)

    def test_first_successful_category_fills_row_after_provider_error(self):
        initialize_database(self.path)
        args=('synthetic','sender','subject','body')
        log_email_to_db(*args,Prediction(outcome='ERROR',source='groq',reason='provider_invalid_request'),None,db_path=self.path)
        log_email_to_db(*args,Prediction(category='UPDATES',outcome='CLASSIFIED',source='gemini'),None,db_path=self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute('SELECT prediction FROM email_logs').fetchone()[0],'UPDATES')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM prediction_attempts').fetchone()[0],2)

    def test_manual_records_use_utc_and_explicit_type(self):
        email_id=insert_new_email("Manual <manual@example.test>","subject","body","UPDATES",db_path=self.path)
        row=get_recent_emails(db_path=self.path)[0]
        self.assertTrue(email_id.startswith("manual-"))
        self.assertEqual(row["message_type"],"manual")
        self.assertTrue(row["created_at"].endswith("Z"))
        self.assertEqual(normalize_legacy_timestamp("17-09-2026 10:00"),"2026-09-17T04:30:00.000000Z")
        with self.assertRaises(ValueError):
            utc_timestamp(datetime(2026,9,17))

    def test_ordering_and_account_clear(self):
        initialize_database(self.path)
        for item in ("b","a"):
            log_email_to_db(item,"sender","subject","body","IMPORTANT","SPAM",account_id="account-a",db_path=self.path)
        log_email_to_db("other","sender","subject","body","IMPORTANT","SPAM",account_id="account-b",db_path=self.path)
        with connection(self.path) as conn:
            conn.execute("UPDATE email_logs SET created_at='2026-09-17T10:00:00.000000Z'")
        self.assertEqual([row["id"] for row in get_recent_emails(account_id="account-a",db_path=self.path)], ["a","b"])
        clear_all_emails(account_id="account-a",db_path=self.path)
        self.assertEqual([row["id"] for row in get_recent_emails(db_path=self.path)], ["other"])
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM prediction_attempts").fetchone()[0], 1)

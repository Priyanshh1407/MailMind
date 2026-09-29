"""Schema 11/12 and account-scoped intelligence repository tests."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src import database
from src.account_state import AccountManager
from src.action_center import (
    ActionRevisionConflict,
    ActionTransitionError,
    create_action,
    get_action,
    list_actions,
    list_reminders,
    schedule_reminder,
    update_action_status,
)
from src.config import Settings
from src.database import connection, initialize_database, utc_timestamp
from src.email_analysis import get_email_analysis, save_email_analysis
from src.token_usage import (
    TokenRequestConflict,
    aggregate_token_usage,
    list_token_usage,
    record_token_usage,
    usage_window,
)


A = "phase1-a@example.test"
B = "phase1-b@example.test"


class EmptyCollection:
    def delete(self, **kwargs):
        return None


class IntelligencePhaseOneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mailmind-intelligence-phase1-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.path = self.settings.db_path

    def schema_ten(self):
        migrations = (
            database._migration_1, database._migration_2, database._migration_3,
            database._migration_4, database._migration_5, database._migration_6,
            database._migration_7, database._migration_8, database._migration_9,
            database._migration_10,
        )
        with connection(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for number, migration in enumerate(migrations, 1):
                migration(conn)
                conn.execute(f"PRAGMA user_version={number}")

    def initialize(self):
        initialize_database(self.path)

    def seed_email(self, account=A, email_id="synthetic-message", *, created_at=None):
        stamp = created_at or utc_timestamp(datetime(2026, 9, 27, 3, 30, tzinfo=timezone.utc))
        with connection(self.path) as conn:
            conn.execute("INSERT INTO accounts(account_id) VALUES (?) ON CONFLICT DO NOTHING", (account,))
            conn.execute("""INSERT INTO email_logs(
                account_id,email_id,sender,subject,body,message_type,processing_state,created_at)
                VALUES (?,?,?,'Synthetic subject','Synthetic body','synthetic','complete',?)""",
                (account, email_id, f"Synthetic <{account}>", stamp))
        return email_id

    def action(self, account=A, email_id="synthetic-message", **overrides):
        values = {
            "action_type": "approval_required",
            "title": "Approve the synthetic checklist",
            "description": "Review and approve the controlled test checklist.",
            "evidence": "approve the controlled test checklist by 5 PM",
            "due_at": "2026-10-02T17:00:00+05:30",
            "due_precision": "relative",
            "confidence": "high",
            "extraction_source": "gemini",
        }
        values.update(overrides)
        return create_action(account, email_id, db_path=self.path, **values)

    def token(self, request_id, *, account=A, email_id="synthetic-message",
              provider="gemini", operation="classification_analysis",
              input_tokens=10, output_tokens=5, total_tokens=15,
              count_method="provider_reported", outcome="success",
              created_at=None, metadata=None):
        return record_token_usage(
            account_id=account, email_id=email_id, request_id=request_id,
            provider=provider, model_version="synthetic-model-v1",
            operation=operation, input_tokens=input_tokens,
            output_tokens=output_tokens, total_tokens=total_tokens,
            count_method=count_method, outcome=outcome, created_at=created_at,
            metadata=metadata, db_path=self.path,
        )

    def test_fresh_schema_has_versioned_tables_constraints_and_indexes(self):
        self.initialize()
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 14)
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertTrue({
                "email_analysis", "token_usage_events", "email_actions",
                "action_reminders", "intelligence_mutation_limits",
                "intelligence_backfill_items",
            }.issubset(tables))
            indexes = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )}
            self.assertTrue({
                "token_usage_account_time", "token_usage_provider_time",
                "token_usage_operation_time", "token_usage_email",
                "email_actions_status_due", "email_actions_email",
                "action_reminders_due", "action_reminders_action",
                "intelligence_backfill_status",
            }.issubset(indexes))
            self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())

    def test_schema_ten_upgrade_is_lossless_and_repeatable(self):
        self.schema_ten()
        self.seed_email()
        with connection(self.path) as conn:
            stamp = utc_timestamp()
            conn.execute("""UPDATE email_search_index
                SET indexing_state='indexed',attempt_count=2
                WHERE account_id=? AND email_id=?""", (A, "synthetic-message"))
            conn.execute("""INSERT INTO feedback_history(
                account_id,email_id,label,created_at) VALUES (?,?,?,?)""",
                (A, "synthetic-message", "IMPORTANT", stamp))
            conn.execute("""INSERT INTO processing_tasks(
                account_id,email_id,status,stage,created_at,updated_at)
                VALUES (?,?,'queued','classify',?,?)""",
                (A, "synthetic-message", stamp, stamp))
            conn.execute("""INSERT INTO ingestion_state(
                account_id,status,fetched_count,failed_count,pages_count,scanned_count,
                warning_count,truncated_count,has_more,listing_error,last_checked_at)
                VALUES (?,'success',1,0,1,1,0,0,0,0,?)""", (A, stamp))
        self.initialize()
        self.initialize()
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 14)
            self.assertEqual(conn.execute(
                "SELECT subject FROM email_logs WHERE account_id=?", (A,)
            ).fetchone()[0], "Synthetic subject")
            self.assertEqual(conn.execute(
                "SELECT label FROM feedback_history WHERE account_id=?", (A,)
            ).fetchone()[0], "IMPORTANT")
            self.assertEqual(conn.execute(
                "SELECT status FROM processing_tasks WHERE account_id=?", (A,)
            ).fetchone()[0], "queued")
            self.assertEqual(conn.execute(
                "SELECT fetched_count FROM ingestion_state WHERE account_id=?", (A,)
            ).fetchone()[0], 1)
            self.assertEqual(tuple(conn.execute(
                "SELECT indexing_state,attempt_count FROM email_search_index "
                "WHERE account_id=?", (A,)
            ).fetchone()), ("indexed", 2))

    def test_interrupted_upgrade_rolls_back_both_new_migrations(self):
        self.schema_ten()
        with patch.object(database, "migration_12", side_effect=RuntimeError("synthetic stop")):
            with self.assertRaisesRegex(RuntimeError, "synthetic stop"):
                self.initialize()
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10)
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertNotIn("email_analysis", names)
            self.assertNotIn("token_usage_events", names)
            self.assertNotIn("email_actions", names)

    def test_database_setup_does_not_initialize_external_services(self):
        with patch("src.llm_api.get_client") as cloud, \
             patch("src.vector_db.create_vector_collection") as vectors:
            self.initialize()
        cloud.assert_not_called()
        vectors.assert_not_called()

    def test_analysis_is_bounded_account_owned_and_replaceable(self):
        self.initialize()
        self.seed_email(A)
        self.seed_email(B)
        result = save_email_analysis(
            A, "synthetic-message", analysis_version="analysis-v1",
            predicted_category="IMPORTANT",
            explanation_summary="Approval is requested before Friday.",
            signals=[
                {"signal": "approval_request", "evidence": "approve before Friday"},
                "deadline",
            ], source="gemini", model_version="synthetic-model-v1",
            retrieval_used=True, db_path=self.path,
        )
        self.assertTrue(result["retrieval_used"])
        self.assertEqual(len(result["signals"]), 2)
        save_email_analysis(
            A, "synthetic-message", analysis_version="analysis-v2",
            predicted_category=None, explanation_summary="Manual review is required.",
            signals=[], source="system", db_path=self.path,
        )
        updated = get_email_analysis(A, "synthetic-message", db_path=self.path)
        self.assertEqual(updated["analysis_version"], "analysis-v2")
        self.assertIsNone(updated["predicted_category"])
        with self.assertRaises(ValueError):
            save_email_analysis(
                A, "synthetic-message", analysis_version="analysis-v3",
                predicted_category="IMPORTANT",
                explanation_summary="x" * 241, signals=[],
                source="system", db_path=self.path,
            )
        with connection(self.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE email_analysis SET signals_json='not-json' "
                    "WHERE account_id=? AND email_id=?",
                    (A, "synthetic-message"),
                )
        self.assertIsNone(get_email_analysis(B, "synthetic-message", db_path=self.path))
        with self.assertRaises(LookupError):
            save_email_analysis(
                B, "missing", analysis_version="analysis-v1",
                predicted_category="IMPORTANT", explanation_summary="Synthetic.",
                signals=[], source="system", db_path=self.path,
            )
        with self.assertRaises(ValueError):
            save_email_analysis(
                A, "synthetic-message", analysis_version="analysis-v1",
                predicted_category="NEEDS_REVIEW", explanation_summary="Synthetic.",
                signals=[], source="system", db_path=self.path,
            )

    def test_actions_deduplicate_filter_and_enforce_account_ownership(self):
        self.initialize()
        self.seed_email(A)
        self.seed_email(B)
        first = self.action(A)
        duplicate = self.action(A)
        other = self.action(B)
        second = self.action(
            A, title="Reply to the synthetic sender", action_type="reply_required",
            description="Send a confirmation reply.", evidence="please reply",
            due_at=None, due_precision="unknown", confidence="medium",
        )
        self.assertTrue(first["inserted"])
        self.assertFalse(duplicate["inserted"])
        self.assertEqual(first["action_id"], duplicate["action_id"])
        self.assertNotEqual(first["action_id"], other["action_id"])
        self.assertEqual(len(list_actions(A, db_path=self.path)), 2)
        self.assertEqual(len(list_actions(A, status="open", db_path=self.path)), 2)
        self.assertEqual(len(list_actions(A, email_id="synthetic-message", db_path=self.path)), 2)
        self.assertEqual(
            [row["action_id"] for row in list_actions(
                A, due_from="2026-10-01T00:00:00Z",
                due_to="2026-10-03T00:00:00Z", db_path=self.path,
            )], [first["action_id"]],
        )
        self.assertIsNone(get_action(B, second["action_id"], db_path=self.path))

    def test_action_revisions_transitions_and_reminders_are_durable(self):
        self.initialize()
        self.seed_email()
        action = self.action()
        now = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)
        reminder = schedule_reminder(
            A, action["action_id"], remind_at="2026-10-02T10:30:00Z",
            now=now, db_path=self.path,
        )
        duplicate = schedule_reminder(
            A, action["action_id"], remind_at="2026-10-02T10:30:00Z",
            now=now, db_path=self.path,
        )
        self.assertTrue(reminder["inserted"])
        self.assertFalse(duplicate["inserted"])
        snoozed = update_action_status(
            A, action["action_id"], status="snoozed", expected_revision=0,
            snoozed_until="2026-09-28T04:00:00Z", now=now, db_path=self.path,
        )
        self.assertEqual((snoozed["status"], snoozed["revision"]), ("snoozed", 1))
        with self.assertRaises(ActionRevisionConflict):
            update_action_status(
                A, action["action_id"], status="open", expected_revision=0,
                now=now, db_path=self.path,
            )
        opened = update_action_status(
            A, action["action_id"], status="open", expected_revision=1,
            now=now, db_path=self.path,
        )
        completed = update_action_status(
            A, action["action_id"], status="completed", expected_revision=2,
            now=now, db_path=self.path,
        )
        self.assertIsNone(opened["snoozed_until"])
        self.assertIsNotNone(completed["completed_at"])
        self.assertEqual(list_reminders(A, action_id=action["action_id"], db_path=self.path)[0]["status"], "dismissed")
        with self.assertRaises(ActionTransitionError):
            schedule_reminder(
                A, action["action_id"], remind_at="2026-10-03T00:00:00Z",
                now=now, db_path=self.path,
            )

    def test_token_request_id_is_idempotent_and_account_private(self):
        self.initialize()
        self.seed_email(A)
        self.seed_email(B)
        first = self.token("synthetic-request-1")
        duplicate = self.token("synthetic-request-1", total_tokens=999)
        self.assertTrue(first["inserted"])
        self.assertFalse(duplicate["inserted"])
        self.assertEqual(duplicate["total_tokens"], 15)
        self.assertEqual(len(list_token_usage(A, db_path=self.path)), 1)
        self.assertEqual(list_token_usage(B, db_path=self.path), [])
        with self.assertRaises(TokenRequestConflict):
            self.token("synthetic-request-1", account=B)

    def test_token_validation_rejects_misleading_counts_and_wrong_ownership(self):
        self.initialize()
        self.seed_email(A)
        self.seed_email(B)
        with self.assertRaises(ValueError):
            self.token("negative", input_tokens=-1)
        with self.assertRaises(ValueError):
            self.token("missing", input_tokens=None, output_tokens=None,
                       total_tokens=None, count_method="provider_reported")
        with self.assertRaises(ValueError):
            self.token("unknown-with-zero", input_tokens=0, output_tokens=None,
                       total_tokens=None, count_method="unavailable")
        with self.assertRaises(ValueError):
            self.token("bad-total", input_tokens=10, output_tokens=5,
                       total_tokens=10)
        with self.assertRaises(ValueError):
            self.token("bad-operation", provider="embedding",
                       operation="classification_analysis")
        with self.assertRaises(LookupError):
            self.token("wrong-email", account=B, email_id="missing")
        with self.assertRaises(ValueError):
            self.token("private-metadata", metadata={"prompt": 1})

    def test_day_week_month_aggregation_uses_local_calendar_bounds(self):
        self.initialize()
        self.seed_email()
        before = datetime(2026, 9, 27, 18, 29, 59, tzinfo=timezone.utc)
        boundary = datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)
        now = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
        self.token("before-day", created_at=before)
        self.token("cloud-day", created_at=boundary)
        self.token(
            "local-day", provider="local", operation="local_shadow",
            input_tokens=7, output_tokens=0, total_tokens=7,
            count_method="tokenizer_counted", created_at=now,
        )
        self.token(
            "unknown-day", input_tokens=None, output_tokens=None,
            total_tokens=None, count_method="unavailable", outcome="timeout",
            created_at=now,
        )
        result = aggregate_token_usage(
            A, window="day", timezone_name="Asia/Kolkata", now=now,
            db_path=self.path,
        )
        self.assertEqual((result["start_at"], result["end_at"]),
                         ("2026-09-27T18:30:00.000000Z", "2026-09-28T18:30:00.000000Z"))
        self.assertEqual(result["totals"]["event_count"], 3)
        self.assertEqual(result["totals"]["unknown_events"], 1)
        self.assertEqual(result["provider_billed_tokens"], 15)
        self.assertEqual(result["local_processed_tokens"], 7)
        week = aggregate_token_usage(
            A, window="week", timezone_name="Asia/Kolkata", now=now,
            db_path=self.path,
        )
        self.assertEqual(week["start_at"], "2026-09-27T18:30:00.000000Z")
        month_start, month_end = usage_window(
            "month", timezone_name="Asia/Kolkata", now=now
        )
        self.assertEqual(month_start, "2026-08-31T18:30:00.000000Z")
        self.assertEqual(month_end, "2026-09-30T18:30:00.000000Z")
        with self.assertRaises(ValueError):
            usage_window("year", timezone_name="Asia/Kolkata", now=now)
        with self.assertRaises(ValueError):
            usage_window("day", timezone_name="Not/A_Zone", now=now)

    def test_email_delete_and_account_purge_remove_all_new_state(self):
        self.initialize()
        self.seed_email(A)
        save_email_analysis(
            A, "synthetic-message", analysis_version="analysis-v1",
            predicted_category="IMPORTANT", explanation_summary="Synthetic.",
            signals=[], source="system", db_path=self.path,
        )
        action = self.action()
        schedule_reminder(
            A, action["action_id"], remind_at="2026-10-02T10:30:00Z",
            now=datetime(2026, 9, 27, tzinfo=timezone.utc), db_path=self.path,
        )
        self.token("email-token")
        with connection(self.path) as conn:
            conn.execute(
                "DELETE FROM email_logs WHERE account_id=? AND email_id=?",
                (A, "synthetic-message"),
            )
            for table in (
                "email_analysis", "email_actions", "action_reminders",
                "token_usage_events",
            ):
                self.assertEqual(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (A,)
                ).fetchone()[0], 0)

        self.seed_email(A)
        save_email_analysis(
            A, "synthetic-message", analysis_version="analysis-v1",
            predicted_category="IMPORTANT", explanation_summary="Synthetic.",
            signals=[], source="system", db_path=self.path,
        )
        action = self.action()
        schedule_reminder(
            A, action["action_id"], remind_at="2026-10-02T10:30:00Z",
            now=datetime(2026, 9, 27, tzinfo=timezone.utc), db_path=self.path,
        )
        self.token("email-token-after-reseed")
        self.token("account-token", email_id=None)
        with connection(self.path) as conn:
            conn.execute("UPDATE runtime_state SET account_id=?,connected=1", (A,))
        manager = AccountManager(self.settings)
        context = manager.worker_context()
        manager.purge(context, EmptyCollection())
        with connection(self.path) as conn:
            for table in (
                "email_analysis", "email_actions", "action_reminders",
                "token_usage_events",
            ):
                self.assertEqual(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (A,)
                ).fetchone()[0], 0)

    def test_invalid_action_values_dates_and_revisions_fail_closed(self):
        self.initialize()
        self.seed_email()
        with self.assertRaises(ValueError):
            self.action(action_type="send_money_now")
        with self.assertRaises(ValueError):
            self.action(due_at=None, due_precision="exact_time")
        with self.assertRaises(ValueError):
            self.action(due_at="not-a-date", due_precision="exact_time")
        action = self.action()
        with self.assertRaises(ValueError):
            update_action_status(
                A, action["action_id"], status="completed",
                expected_revision=-1, db_path=self.path,
            )
        with self.assertRaises(ActionTransitionError):
            update_action_status(
                A, action["action_id"], status="open", expected_revision=0,
                db_path=self.path,
            )


if __name__ == "__main__":
    unittest.main()

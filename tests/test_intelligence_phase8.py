"""Feature-specific rollout, backfill, and validation tests for Phase 8."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.app import create_app
from src import database
from src.account_state import AccountManager
from src.action_center import list_actions, list_reminders
from src.config import Settings
from src.database import connection, initialize_database, utc_timestamp
from src.db_utils import log_email_to_db
from src.intelligence_backfill import (
    backfill_summary,
    queue_intelligence_backfill,
)
from src.pipeline import process_task
from src.prediction import (
    ActionCandidate,
    AnalysisSignal,
    EmailAnalysis,
    Prediction,
)
from src.token_usage import TokenMeasurement
from src.work_queue import claim_cycle, finish_cycle, newest_due_tasks


A = "phase8-owner@example.test"
B = "phase8-other@example.test"
ORIGIN = "http://localhost:5173"
SOURCE_TIME = "2026-09-29T03:30:00.000000Z"


class EmptyCollection:
    def delete(self, **kwargs):
        return None


def controlled_analysis(*actions):
    return EmailAnalysis(
        predicted_category="IMPORTANT",
        explanation_summary="A controlled approval request has a deadline.",
        signals=(
            AnalysisSignal(
                "approval_request", "approve the controlled rollout"),
            AnalysisSignal("deadline", "before Thursday"),
        ),
        source="gemini",
        model_version="synthetic-phase8-v1",
        actions=tuple(actions),
    )


def controlled_prediction(*actions):
    return Prediction(
        category="IMPORTANT",
        outcome="CLASSIFIED",
        source="gemini",
        model_version="synthetic-phase8-v1",
        analysis=controlled_analysis(*actions),
    )


class Phase8Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mailmind-phase8-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            data_dir=Path(self.temp.name),
            action_extraction_enabled=True,
            action_reminders_enabled=True,
            telegram_action_reminders_enabled=True,
            explanations_visible=True,
            token_collection_enabled=True,
            token_analytics_visible=True,
            auto_mark_read=True,
        )
        initialize_database(self.settings.db_path)

    def seed_email(self, email_id, *, account=A, source="backlog",
                   status="complete", created_at=SOURCE_TIME):
        stamp = utc_timestamp()
        with connection(self.settings.db_path) as conn:
            conn.execute(
                "INSERT INTO accounts(account_id) VALUES (?) "
                "ON CONFLICT DO NOTHING", (account,))
            conn.execute(
                """INSERT INTO email_logs(
                       account_id,email_id,sender,subject,body,message_type,
                       processing_state,created_at)
                   VALUES (?,?,?,'Controlled rollout approval',
                           'Please approve the controlled rollout before Thursday.',
                           'synthetic','complete',?)""",
                (account,email_id,f"Sender <{account}>",created_at))
            conn.execute(
                """INSERT INTO processing_tasks(
                       account_id,email_id,status,stage,read_required,
                       created_at,updated_at,source)
                   VALUES (?,?,?,'complete',1,?,?,?)""",
                (account,email_id,status,stamp,stamp,source))
        return email_id

    def connected_manager(self):
        manager = AccountManager(self.settings)
        token, _csrf = manager.open_session()
        browser, _ = manager.session(token)
        manager.finish_auth(manager.begin_auth(browser), (A, "{}"))
        browser, _ = manager.session(token)
        return manager, browser, token


class Phase8MigrationAndAdmissionTests(Phase8Base):
    def test_schema_14_upgrade_is_atomic_repeatable_and_required(self):
        with connection(self.settings.db_path) as conn:
            conn.execute("DROP TABLE intelligence_backfill_items")
            conn.execute("PRAGMA user_version=13")
        with patch.object(
                database, "migration_14",
                side_effect=RuntimeError("synthetic migration stop")):
            with self.assertRaisesRegex(
                    RuntimeError, "synthetic migration stop"):
                initialize_database(self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 13)
            self.assertIsNone(conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table'
                     AND name='intelligence_backfill_items'""").fetchone())

        initialize_database(self.settings.db_path)
        initialize_database(self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 14)
            self.assertIsNotNone(conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='index'
                     AND name='intelligence_backfill_status'""").fetchone())
            conn.execute("DROP TABLE intelligence_backfill_items")
        with self.assertRaisesRegex(ValueError, "schema is incomplete"):
            initialize_database(self.settings.db_path)

    def test_admission_is_bounded_idempotent_account_scoped_and_capacity_aware(self):
        for index in range(5):
            self.seed_email(
                f"mail-{index}",
                created_at=f"2026-09-{20 + index:02d}T03:30:00.000000Z")
        self.seed_email("other-mail", account=B)

        first = queue_intelligence_backfill(
            A, limit=2, max_pending_tasks=3,
            db_path=self.settings.db_path)
        second = queue_intelligence_backfill(
            A, limit=100, max_pending_tasks=3,
            db_path=self.settings.db_path)
        blocked = queue_intelligence_backfill(
            A, limit=1, max_pending_tasks=3,
            db_path=self.settings.db_path)
        self.assertEqual(
            (first["admitted"], second["admitted"], blocked["admitted"]),
            (2, 1, 0))
        self.assertEqual(blocked["capacity"], 0)
        summary = backfill_summary(A, db_path=self.settings.db_path)
        self.assertEqual(summary["queued"], 3)
        self.assertEqual(summary["eligible"], 2)
        self.assertEqual(
            backfill_summary(B, db_path=self.settings.db_path)["eligible"], 1)
        with connection(self.settings.db_path) as conn:
            rows = conn.execute(
                """SELECT account_id,email_id FROM intelligence_backfill_items
                   ORDER BY email_id""").fetchall()
        self.assertEqual({row["account_id"] for row in rows}, {A})
        self.assertEqual(
            {row["email_id"] for row in rows},
            {"mail-2", "mail-3", "mail-4"})
        with self.assertRaises(ValueError):
            queue_intelligence_backfill(
                A, limit=101, db_path=self.settings.db_path)


class Phase8WorkerTests(Phase8Base):
    def test_backfill_uses_existing_worker_without_historical_side_effects(self):
        self.seed_email("mail-1")
        queue_intelligence_backfill(
            A, limit=1, max_pending_tasks=100,
            db_path=self.settings.db_path)
        manager, _browser, _session = self.connected_manager()
        context = manager.worker_context()
        token, job = claim_cycle(manager, context)
        task = newest_due_tasks(manager, context, token, 1)[0]
        candidate = ActionCandidate(
            action_type="approval_required",
            title="Approve the controlled rollout",
            description="Review and approve the controlled rollout.",
            evidence="approve the controlled rollout",
            due_at="2026-10-01T11:30:00Z",
            due_precision="exact_time",
            confidence="high",
        )

        def classifier(*args, **kwargs):
            kwargs["usage_recorder"].record(
                "controlled-gemini",
                provider="gemini",
                model_version="synthetic-phase8-v1",
                operation="classification_analysis",
                outcome="success",
                measurement=TokenMeasurement(
                    input_tokens=12,
                    output_tokens=4,
                    total_tokens=16,
                    count_method="provider_reported",
                    metadata={},
                ),
            )
            return controlled_prediction(candidate)

        notifier = Mock()
        marker = Mock()
        completed = process_task(
            task, manager, context, token, Mock(), Mock(),
            lambda: EmptyCollection(),
            classifier=classifier,
            shadow=lambda *args, **kwargs: (
                Prediction(
                    category="IMPORTANT",
                    outcome="CLASSIFIED",
                    source="local",
                    model_version="synthetic-local",
                ),
                7,
            ),
            notifier=notifier,
            marker=marker,
            logger=log_email_to_db,
            validator=lambda metadata: True,
            job_id=job,
        )
        finish_cycle(manager, context, token, job)
        self.assertTrue(completed)
        notifier.assert_not_called()
        marker.assert_not_called()
        self.assertEqual(len(list_actions(
            A, db_path=self.settings.db_path)), 1)
        self.assertEqual(list_reminders(
            A, db_path=self.settings.db_path), [])
        with connection(self.settings.db_path) as conn:
            task_row = conn.execute(
                """SELECT status,stage,read_required
                   FROM processing_tasks
                   WHERE account_id=? AND email_id='mail-1'""",
                (A,)).fetchone()
            backfill = conn.execute(
                """SELECT status,error_code
                   FROM intelligence_backfill_items
                   WHERE account_id=? AND email_id='mail-1'""",
                (A,)).fetchone()
            counts = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (A,)
                ).fetchone()[0]
                for table in (
                    "email_analysis", "token_usage_events",
                    "notification_outbox", "action_reminders",
                )
            }
        self.assertEqual(tuple(task_row), ("complete", "complete", 0))
        self.assertEqual(tuple(backfill), ("complete", None))
        self.assertEqual(counts, {
            "email_analysis": 1,
            # Only the cloud call is metered: the local shadow is off by default.
            "token_usage_events": 1,
            "notification_outbox": 0,
            "action_reminders": 0,
        })

    def test_live_work_precedes_backfill_and_expired_backfill_recovers(self):
        self.seed_email("saved-mail")
        queue_intelligence_backfill(
            A, limit=1, db_path=self.settings.db_path)
        self.seed_email("live-mail", source="live", status="queued")
        with connection(self.settings.db_path) as conn:
            conn.execute(
                """UPDATE processing_tasks SET stage='classify'
                   WHERE account_id=? AND email_id='live-mail'""", (A,))
        manager, _browser, _session = self.connected_manager()
        context = manager.worker_context()
        token, _job = claim_cycle(manager, context)
        ordered = newest_due_tasks(manager, context, token, 2)
        self.assertEqual(
            [row["email_id"] for row in ordered],
            ["live-mail", "saved-mail"])

        with connection(self.settings.db_path) as conn:
            conn.execute(
                """UPDATE processing_tasks
                   SET status='running',owner_token=?
                   WHERE account_id=? AND email_id='saved-mail'""",
                (token,A))
            conn.execute(
                """UPDATE intelligence_backfill_items SET status='running'
                   WHERE account_id=? AND email_id='saved-mail'""", (A,))
            conn.execute("UPDATE runtime_state SET lease_until=0")
        replacement, replacement_job = claim_cycle(manager, context)
        with connection(self.settings.db_path) as conn:
            task_state = conn.execute(
                """SELECT status,error_code,owner_token
                   FROM processing_tasks
                   WHERE account_id=? AND email_id='saved-mail'""",
                (A,)).fetchone()
            backfill_state = conn.execute(
                """SELECT status,error_code
                   FROM intelligence_backfill_items
                   WHERE account_id=? AND email_id='saved-mail'""",
                (A,)).fetchone()
        finish_cycle(
            manager, context, replacement, replacement_job)
        self.assertEqual(
            tuple(task_state), ("retry", "interrupted", None))
        self.assertEqual(
            tuple(backfill_state), ("retry", "interrupted"))

    def test_purge_removes_backfill_rows_without_crossing_accounts(self):
        self.seed_email("mail-a")
        self.seed_email("mail-b", account=B)
        queue_intelligence_backfill(
            A, limit=1, db_path=self.settings.db_path)
        queue_intelligence_backfill(
            B, limit=1, db_path=self.settings.db_path)
        manager, browser, _session = self.connected_manager()
        manager.purge(browser, EmptyCollection())
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute(
                """SELECT COUNT(*) FROM intelligence_backfill_items
                   WHERE account_id=?""", (A,)).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                """SELECT COUNT(*) FROM intelligence_backfill_items
                   WHERE account_id=?""", (B,)).fetchone()[0], 1)


class Phase8ApiTests(Phase8Base):
    def setUp(self):
        super().setUp()
        for index in range(3):
            self.seed_email(f"mail-{index}")
        model = Mock(model_loaded=False, load_reason="missing_checkpoint")
        self.app = create_app(
            settings=self.settings,
            model_factory=Mock(return_value=model),
            vector_factory=Mock(return_value=EmptyCollection()),
        )
        self.client = TestClient(self.app, base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.headers["Origin"] = ORIGIN
        csrf = self.client.post("/session").json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = csrf
        context, _ = self.app.state.accounts.session(
            self.client.cookies.get("mailmind_session"))
        self.app.state.accounts.finish_auth(
            self.app.state.accounts.begin_auth(context), (A, "{}"))
        self.csrf = self.client.get("/session").json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf

    def test_backfill_api_is_explicit_bounded_observable_and_csrf_protected(self):
        self.client.headers["X-CSRF-Token"] = "wrong"
        rejected = self.client.post(
            "/intelligence/backfill", json={"limit": 2})
        self.client.headers["X-CSRF-Token"] = self.csrf
        invalid = self.client.post(
            "/intelligence/backfill", json={"limit": 101})
        first = self.client.post(
            "/intelligence/backfill", json={"limit": 2})
        status = self.client.get("/status")
        second = self.client.post(
            "/intelligence/backfill", json={"limit": 20})
        exhausted = self.client.post(
            "/intelligence/backfill", json={"limit": 1})

        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(
            (first.json()["requested"], first.json()["admitted"]),
            (2, 2))
        self.assertIn("never sends historical alerts", first.json()["message"])
        summary = status.json()["intelligence_backfill"]
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["queued"], 2)
        self.assertEqual(summary["eligible"], 1)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["admitted"], 1)
        self.assertEqual(exhausted.status_code, 409)

    def test_backfill_requires_connection_and_action_rollout(self):
        self.assertEqual(self.client.post("/disconnect").status_code, 200)
        disconnected = self.client.post(
            "/intelligence/backfill", json={"limit": 1})
        self.assertEqual(disconnected.status_code, 409)

        disabled_settings = Settings(
            data_dir=Path(self.temp.name) / "disabled",
            action_extraction_enabled=False,
        )
        disabled_app = create_app(
            settings=disabled_settings,
            model_factory=Mock(return_value=Mock(
                model_loaded=False, load_reason="missing_checkpoint")),
            vector_factory=Mock(return_value=EmptyCollection()),
        )
        with TestClient(
                disabled_app, base_url="http://localhost") as disabled_client:
            disabled_client.headers["Origin"] = ORIGIN
            csrf = disabled_client.post("/session").json()["csrf_token"]
            disabled_client.headers["X-CSRF-Token"] = csrf
            context, _ = disabled_app.state.accounts.session(
                disabled_client.cookies.get("mailmind_session"))
            disabled_app.state.accounts.finish_auth(
                disabled_app.state.accounts.begin_auth(context), (A, "{}"))
            disabled_client.headers["X-CSRF-Token"] = (
                disabled_client.get("/session").json()["csrf_token"])
            disabled_status = disabled_client.get(
                "/status").json()["intelligence_backfill"]
            response = disabled_client.post(
                "/intelligence/backfill", json={"limit": 1})
        self.assertFalse(disabled_status["enabled"])
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

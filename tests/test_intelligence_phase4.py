"""Phase 4 Action Center, lifecycle, and reminder integration tests."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.app import create_app
from src import llm_api, main
from src.account_state import AccountManager, WorkCancelled
from src.action_center import (
    ActionRevisionConflict,
    action_summary,
    automatic_reminder_time,
    create_action,
    get_action,
    list_actions,
    list_reminders,
    persist_analysis_actions,
    resolve_action_candidate,
    schedule_reminder,
    update_action_status,
)
from src.config import Settings
from src.database import connection, initialize_database, utc_timestamp
from src.db_utils import log_email_to_db
from src.intelligence_contract import ActionType, TokenOperation
from src.mime_parser import parse_gmail_message
from src.notifier import Delivery
from src.pipeline import process_task
from src.prediction import ActionCandidate, AnalysisSignal, EmailAnalysis, Prediction
from src.token_usage import tokenizer_usage_measurement
from src.work_queue import (
    claim_cycle,
    finish_cycle,
    newest_due_tasks,
    process_due_reminders,
    reconcile_email_analysis,
)


A = "phase4-actions@example.test"
B = "phase4-other@example.test"
ORIGIN = "http://localhost:5173"
SOURCE_TIME = "2026-09-28T03:30:00.000000Z"


class EmptyCollection:
    def delete(self, **kwargs):
        return None


def candidate(action_type="approval_required", *, title="Approve launch checklist",
              description="Review and approve the launch checklist.",
              evidence="approve the launch checklist", due_at=None,
              due_precision="unknown", confidence="medium"):
    return ActionCandidate(
        action_type=action_type,
        title=title,
        description=description,
        evidence=evidence,
        due_at=due_at,
        due_precision=due_precision,
        confidence=confidence,
    )


def analysis(*actions, source="gemini"):
    return EmailAnalysis(
        predicted_category="IMPORTANT",
        explanation_summary="The email contains a direct request.",
        signals=(AnalysisSignal("direct_request", "approve the launch checklist"),),
        source=source,
        model_version="synthetic-phase4-v1",
        actions=tuple(actions),
    )


class Phase4Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mailmind-phase4-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            data_dir=Path(self.temp.name),
            action_extraction_enabled=True,
            action_reminders_enabled=True,
            explanations_visible=True,
        )
        initialize_database(self.settings.db_path)

    def seed_email(self, email_id="mail-1", *, account=A,
                   subject="Approval needed", body=None,
                   created_at=SOURCE_TIME):
        body = body or (
            "Please approve the launch checklist. Reply before the deadline."
        )
        with connection(self.settings.db_path) as conn:
            conn.execute(
                "INSERT INTO accounts(account_id) VALUES (?) ON CONFLICT DO NOTHING",
                (account,),
            )
            conn.execute(
                """INSERT INTO email_logs(
                   account_id,email_id,sender,subject,body,message_type,
                   processing_state,created_at)
                   VALUES (?,?,? ,?,?, 'synthetic','classified',?)""",
                (account,email_id,"Sender <sender@example.test>",subject,body,
                 created_at),
            )
        return email_id

    def create_action(self, email_id="mail-1", *, account=A, **overrides):
        values = {
            "action_type": "approval_required",
            "title": "Approve launch checklist",
            "description": "Review and approve the launch checklist.",
            "evidence": "approve the launch checklist",
            "due_at": "2026-10-02T11:30:00.000000Z",
            "due_precision": "exact_time",
            "confidence": "high",
            "extraction_source": "gemini",
        }
        values.update(overrides)
        return create_action(
            account,email_id,db_path=self.settings.db_path,**values)

    def connected_manager(self, settings=None):
        manager = AccountManager(settings or self.settings)
        token, _csrf = manager.open_session()
        context, _ = manager.session(token)
        manager.finish_auth(manager.begin_auth(context),(A,"{}"))
        return manager, token


class Phase4CandidateTests(Phase4Base):
    def test_gmail_source_timestamp_is_preserved_and_sent_as_untrusted_data(self):
        milliseconds = str(int(datetime(
            2026,9,28,3,30,tzinfo=timezone.utc).timestamp() * 1000))
        payload = {
            "internalDate": milliseconds,
            "payload": {
                "mimeType": "text/plain",
                "headers": [],
                "body": {"data": base64.urlsafe_b64encode(
                    b"Synthetic body").decode("ascii")},
            },
        }
        parsed = parse_gmail_message(payload,"gmail-1")
        self.assertEqual(parsed["source_created_at"],SOURCE_TIME)
        wire, support = llm_api.build_classification_payload(
            "sender","subject","body",[],source_timestamp=SOURCE_TIME)
        self.assertEqual(support,0)
        self.assertEqual(json.loads(wire)["email"]["received_at"],SOURCE_TIME)
        self.assertIn("email.received_at",llm_api.SYSTEM_INSTRUCTION)
        with self.assertRaises(ValueError):
            llm_api.build_classification_payload(
                "s","s","b",[],source_timestamp="no-timezone")

    def test_every_action_type_survives_phase4_validation(self):
        types = [item.value for item in ActionType]
        for index, action_type in enumerate(types):
            email_id = self.seed_email(f"type-{index}")
            item = candidate(
                action_type,
                title=f"Synthetic task {index}",
                description=f"Handle synthetic task {index}.",
            )
            result = persist_analysis_actions(
                A,email_id,analysis(item),source_created_at=SOURCE_TIME,
                source_text="Please approve the launch checklist.",
                db_path=self.settings.db_path,
            )
            self.assertEqual(result["accepted_count"],1)
            self.assertEqual(list_actions(
                A,email_id=email_id,db_path=self.settings.db_path
            )[0]["action_type"],action_type)

    def test_confidence_evidence_limits_and_idempotent_deduplication(self):
        self.seed_email()
        low = candidate(title="Low confidence",confidence="low")
        medium = candidate(title="Medium confidence")
        high = candidate(
            title="High confidence",confidence="high",
            due_at="2026-10-02T17:00:00+05:30",
            due_precision="exact_time",
        )
        invalid = candidate(
            title="Invented evidence",evidence="not in the email")
        kwargs = dict(
            source_created_at=SOURCE_TIME,
            source_text="Please approve the launch checklist.",
            reminders_enabled=True,
            now=datetime(2026,9,28,4,tzinfo=timezone.utc),
            db_path=self.settings.db_path,
        )
        first = persist_analysis_actions(
            A,"mail-1",analysis(low,medium,high,invalid),**kwargs)
        second = persist_analysis_actions(
            A,"mail-1",analysis(low,medium,high,invalid),**kwargs)
        self.assertEqual(first["rejected_low_confidence"],1)
        self.assertEqual(first["rejected_invalid"],1)
        self.assertEqual(first["created_count"],2)
        self.assertEqual(second["created_count"],0)
        self.assertEqual(len(list_actions(A,db_path=self.settings.db_path)),2)
        reminders = list_reminders(A,db_path=self.settings.db_path)
        self.assertEqual(len(reminders),1)

    def test_relative_date_date_only_and_distant_deadline_resolution(self):
        source = "2026-09-28T00:00:00Z"
        relative = resolve_action_candidate(candidate(
            due_at="2026-09-30T00:00:00+05:30",
            due_precision="relative",confidence="high"),
            source_created_at=source)
        date_only = resolve_action_candidate(candidate(
            due_at="2026-10-01T00:00:00+05:30",
            due_precision="date_only",confidence="high"),
            source_created_at=source)
        distant = resolve_action_candidate(candidate(
            due_at="2028-01-01T10:00:00+05:30",
            due_precision="exact_time",confidence="high"),
            source_created_at=source)
        self.assertEqual(relative["due_at"],"2026-09-30T03:30:00.000000Z")
        self.assertEqual(date_only["due_at"],"2026-10-01T03:30:00.000000Z")
        self.assertEqual((distant["due_at"],distant["due_precision"]),
                         (None,"unknown"))

    def test_exact_date_only_unknown_and_overdue_reminder_rules(self):
        source = "2026-09-28T00:00:00Z"
        now = datetime(2026,9,28,1,tzinfo=timezone.utc)
        exact = {
            "confidence":"high","due_precision":"exact_time",
            "due_at":"2026-09-28T03:00:00Z",
        }
        date_only = {
            "confidence":"high","due_precision":"date_only",
            "due_at":"2026-09-29T03:30:00Z",
        }
        unknown = {
            "confidence":"high","due_precision":"unknown","due_at":None,
        }
        overdue = {
            "confidence":"high","due_precision":"exact_time",
            "due_at":"2026-09-28T00:30:00Z",
        }
        self.assertEqual(automatic_reminder_time(
            exact,source_created_at=source,now=now),
            "2026-09-28T02:30:00.000000Z")
        self.assertEqual(automatic_reminder_time(
            date_only,source_created_at=source,now=now),
            "2026-09-29T03:30:00.000000Z")
        self.assertIsNone(automatic_reminder_time(
            unknown,source_created_at=source,now=now))
        self.assertIsNone(automatic_reminder_time(
            overdue,source_created_at=source,now=now))

    def test_overdue_actions_are_visible_but_never_auto_scheduled(self):
        self.seed_email(created_at="2026-09-27T00:00:00.000000Z")
        overdue = candidate(
            confidence="high",due_precision="exact_time",
            due_at="2026-09-28T00:00:00Z")
        result = persist_analysis_actions(
            A,"mail-1",analysis(overdue),
            source_created_at="2026-09-27T00:00:00Z",
            source_text="Please approve the launch checklist.",
            reminders_enabled=True,
            now=datetime(2026,9,29,tzinfo=timezone.utc),
            db_path=self.settings.db_path)
        self.assertEqual(result["accepted_count"],1)
        self.assertEqual(result["reminder_count"],0)
        self.assertEqual(action_summary(
            A,now=datetime(2026,9,29,tzinfo=timezone.utc),
            db_path=self.settings.db_path)["overdue"],1)


class Phase4ApiTests(Phase4Base):
    def setUp(self):
        super().setUp()
        model = Mock(model_loaded=False,load_reason="missing_checkpoint")
        model.predict.return_value = Prediction(
            category="IMPORTANT",outcome="CLASSIFIED",source="local",
            model_version="synthetic-local")
        self.app = create_app(
            settings=self.settings,model_factory=Mock(return_value=model),
            vector_factory=Mock(return_value=EmptyCollection()))
        self.client = TestClient(self.app,base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.client.headers["Origin"] = ORIGIN
        csrf = self.client.post("/session").json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = csrf
        context, _ = self.app.state.accounts.session(
            self.client.cookies.get("mailmind_session"))
        self.app.state.accounts.finish_auth(
            self.app.state.accounts.begin_auth(context),(A,"{}"))
        self.csrf = self.client.get("/session").json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf
        self.seed_email()

    def test_list_summary_and_account_ownership_endpoints(self):
        first = self.create_action()
        self.seed_email("other-mail",account=B)
        create_action(
            B,"other-mail",action_type="general_task",title="Other",
            description="Other account task.",evidence="Other account task",
            due_at=None,due_precision="unknown",confidence="medium",
            extraction_source="system",db_path=self.settings.db_path)
        listing = self.client.get("/actions").json()
        summary = self.client.get("/actions/summary").json()
        self.assertEqual([row["action_id"] for row in listing["actions"]],
                         [first["action_id"]])
        self.assertEqual(summary["total"],1)
        self.assertEqual(summary["status_counts"]["open"],1)
        self.assertEqual(summary["account_id"],A)

    def test_complete_dismiss_snooze_reopen_and_snooze_expiry(self):
        completed = self.create_action()
        dismissed = self.create_action(
            email_id=self.seed_email("mail-2"),title="Dismiss me")
        snoozed = self.create_action(
            email_id=self.seed_email("mail-3"),title="Snooze me")
        response = self.client.patch(
            f"/actions/{completed['action_id']}",
            json={"status":"completed","expected_revision":0})
        self.assertEqual(response.status_code,200)
        self.assertIsNotNone(response.json()["completed_at"])
        response = self.client.patch(
            f"/actions/{completed['action_id']}",
            json={"status":"open","expected_revision":1})
        self.assertEqual(response.json()["status"],"open")
        response = self.client.patch(
            f"/actions/{dismissed['action_id']}",
            json={"status":"dismissed","expected_revision":0})
        self.assertEqual(response.json()["status"],"dismissed")
        response = self.client.patch(
            f"/actions/{dismissed['action_id']}",
            json={"status":"open","expected_revision":1})
        self.assertEqual(response.json()["status"],"open")
        future = (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
        response = self.client.post(
            f"/actions/{snoozed['action_id']}/snooze",
            json={"snoozed_until":future,"expected_revision":0})
        self.assertEqual(response.json()["status"],"snoozed")
        with connection(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE email_actions SET snoozed_until=? WHERE action_id=?",
                ("2026-09-27T00:00:00.000000Z",snoozed["action_id"]))
        manager = self.app.state.accounts
        context = manager.worker_context()
        token, job = claim_cycle(manager,context)
        result = process_due_reminders(
            manager,context,token,Mock(),
            now=datetime(2026,9,28,tzinfo=timezone.utc))
        finish_cycle(manager,context,token,job)
        self.assertEqual(result["snoozes_expired"],1)
        self.assertEqual(get_action(
            A,snoozed["action_id"],db_path=self.settings.db_path)["status"],
            "open")

    def test_stale_revisions_fail_for_status_snooze_and_reminder_mutations(self):
        item = self.create_action()
        self.client.patch(
            f"/actions/{item['action_id']}",
            json={"status":"completed","expected_revision":0})
        stale_patch = self.client.patch(
            f"/actions/{item['action_id']}",
            json={"status":"open","expected_revision":0})
        stale_snooze = self.client.post(
            f"/actions/{item['action_id']}/snooze",
            json={"snoozed_until":(
                datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                  "expected_revision":0})
        stale_reminder = self.client.post(
            f"/actions/{item['action_id']}/reminders",
            json={"remind_at":(
                datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                  "expected_revision":0,"channel":"dashboard"})
        self.assertEqual(stale_patch.status_code,409)
        self.assertEqual(stale_snooze.status_code,409)
        self.assertEqual(stale_reminder.status_code,409)

    def test_every_phase4_mutation_requires_csrf(self):
        item = self.create_action()
        self.client.headers["X-CSRF-Token"] = "wrong"
        requests = (
            self.client.patch(
                f"/actions/{item['action_id']}",
                json={"status":"completed","expected_revision":0}),
            self.client.post(
                f"/actions/{item['action_id']}/snooze",
                json={"snoozed_until":(
                    datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                      "expected_revision":0}),
            self.client.post(
                f"/actions/{item['action_id']}/reminders",
                json={"remind_at":(
                    datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                      "expected_revision":0}),
            self.client.post("/emails/mail-1/reanalyze",json={}),
        )
        self.assertEqual([response.status_code for response in requests],
                         [401,401,401,401])

    def test_patch_is_allowed_by_the_local_cors_policy(self):
        response = self.client.options(
            "/actions/1",
            headers={
                "Origin":ORIGIN,
                "Access-Control-Request-Method":"PATCH",
            })
        self.assertEqual(response.status_code,200)
        self.assertIn(
            "PATCH",response.headers["access-control-allow-methods"])

    def test_manual_reminders_are_future_only_and_telegram_is_opt_in(self):
        item = self.create_action()
        past = self.client.post(
            f"/actions/{item['action_id']}/reminders",
            json={"remind_at":"2020-01-01T00:00:00Z",
                  "expected_revision":0,"channel":"dashboard"})
        telegram = self.client.post(
            f"/actions/{item['action_id']}/reminders",
            json={"remind_at":(
                datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                  "expected_revision":0,"channel":"telegram"})
        dashboard = self.client.post(
            f"/actions/{item['action_id']}/reminders",
            json={"remind_at":(
                datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                  "expected_revision":0,"channel":"dashboard"})
        self.assertEqual(past.status_code,422)
        self.assertEqual(telegram.status_code,403)
        self.assertEqual(dashboard.status_code,200)
        self.assertEqual(dashboard.json()["channel"],"dashboard")

    def test_manual_reanalysis_is_one_email_bounded_metered_and_state_safe(self):
        existing = self.create_action()
        update_action_status(
            A,existing["action_id"],status="completed",expected_revision=0,
            db_path=self.settings.db_path)
        second = self.seed_email("mail-2")
        enriched = analysis(candidate(
            confidence="high",due_at="2026-10-02T17:00:00+05:30",
            due_precision="exact_time"))
        prediction = Prediction(
            category="IMPORTANT",outcome="CLASSIFIED",source="gemini",
            model_version="synthetic-phase4-v1",analysis=enriched)

        def reanalyze(*args, **kwargs):
            kwargs["usage_recorder"].record(
                "synthetic-provider",provider="local",
                model_version="synthetic-local",
                operation=kwargs["usage_operation"],outcome="success",
                measurement=tokenizer_usage_measurement(7))
            return prediction

        with patch("api.app.manual_prediction",side_effect=reanalyze) as call:
            response = self.client.post("/emails/mail-1/reanalyze",json={})
            repeated = self.client.post(
                "/emails/mail-1/reanalyze",
                json={"expected_analysis_updated_at":
                      response.json()["analysis_revision"]})
        self.assertEqual(response.status_code,200)
        self.assertEqual(repeated.status_code,429)
        self.assertEqual(call.call_args.kwargs["usage_operation"],
                         TokenOperation.ACTION_REANALYSIS.value)
        self.assertEqual(get_action(
            A,existing["action_id"],db_path=self.settings.db_path)["status"],
            "completed")
        self.assertEqual(list_actions(
            A,email_id=second,db_path=self.settings.db_path),[])
        with connection(self.settings.db_path) as conn:
            operation = conn.execute(
                "SELECT operation FROM token_usage_events"
            ).fetchone()[0]
        self.assertEqual(operation,"action_reanalysis")
        listed = list_actions(A,db_path=self.settings.db_path)[0]
        self.assertEqual(
            listed["analysis_revision"],response.json()["analysis_revision"])

    def test_reanalysis_rejects_a_stale_analysis_revision(self):
        from src.email_analysis import save_analysis_result
        saved = save_analysis_result(
            A,"mail-1",analysis(),db_path=self.settings.db_path)
        stale = self.client.post(
            "/emails/mail-1/reanalyze",
            json={"expected_analysis_updated_at":"2020-01-01T00:00:00Z"})
        self.assertEqual(stale.status_code,409)
        self.assertEqual(saved["updated_at"],
                         self.client.get("/emails").json()["emails"][0]
                         ["analysis"]["updated_at"])

    def test_internal_action_candidates_are_not_exposed_in_history(self):
        enriched = analysis(candidate())
        with connection(self.settings.db_path) as conn:
            log_email_to_db(
                "mail-1","Sender","Approval needed",
                "Please approve the launch checklist.",
                Prediction(category="IMPORTANT",outcome="CLASSIFIED",
                           source="gemini",analysis=enriched),
                Prediction(),account_id=A,db_conn=conn)
        response = self.client.get("/emails/mail-1/history").json()
        self.assertNotIn("actions",response["classification"][0]["analysis"])


class Phase4WorkerTests(Phase4Base):
    def setUp(self):
        super().setUp()
        self.seed_email()
        self.manager, self.session_token = self.connected_manager()
        self.context = self.manager.worker_context()

    def claim(self):
        ownership = claim_cycle(self.manager,self.context)
        self.assertIsNotNone(ownership)
        return ownership

    def due_reminder(self, *, channel="dashboard", title="Reminder task"):
        action = self.create_action(title=title)
        reminder = schedule_reminder(
            A,action["action_id"],channel=channel,
            remind_at="2026-09-28T01:00:00Z",
            now=datetime(2026,9,27,tzinfo=timezone.utc),
            db_path=self.settings.db_path)
        return action, reminder

    def test_dashboard_reminders_survive_restart_and_duplicate_cycles(self):
        _action, reminder = self.due_reminder()
        restarted = AccountManager(self.settings)
        context = restarted.worker_context()
        token, job = claim_cycle(restarted,context)
        first = process_due_reminders(
            restarted,context,token,Mock(),
            now=datetime(2026,9,29,tzinfo=timezone.utc))
        second = process_due_reminders(
            restarted,context,token,Mock(),
            now=datetime(2026,9,29,tzinfo=timezone.utc))
        finish_cycle(restarted,context,token,job)
        self.assertEqual(first["delivered"],1)
        self.assertEqual(second["processed"],0)
        with connection(self.settings.db_path) as conn:
            row = conn.execute(
                "SELECT status,attempt_count FROM action_reminders WHERE reminder_id=?",
                (reminder["reminder_id"],)).fetchone()
        self.assertEqual(tuple(row),("delivered",1))
        listed = list_actions(A,db_path=self.settings.db_path)[0]
        self.assertIsNone(listed["next_reminder_at"])
        self.assertEqual(listed["delivered_reminder_count"],1)

    def test_telegram_retry_delivery_and_expired_claim_recovery(self):
        self.manager.settings = replace(
            self.settings,telegram_action_reminders_enabled=True)
        self.context = self.manager.worker_context()
        _action, reminder = self.due_reminder(channel="telegram")
        token, job = self.claim()
        retry = process_due_reminders(
            self.manager,self.context,token,
            Mock(return_value=Delivery("retry","notification_transient")),
            now=datetime(2026,9,29,tzinfo=timezone.utc))
        self.assertEqual(retry["retry"],1)
        with connection(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE action_reminders SET next_retry_at=0 WHERE reminder_id=?",
                (reminder["reminder_id"],))
        delivered = process_due_reminders(
            self.manager,self.context,token,
            Mock(return_value=Delivery("sent",message_id="42")),
            now=datetime(2026,9,29,tzinfo=timezone.utc))
        self.assertEqual(delivered["delivered"],1)
        finish_cycle(self.manager,self.context,token,job)

        _second_action, second = self.due_reminder(
            channel="telegram",title="Interrupted Telegram")
        with connection(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE action_reminders SET status='claimed',owner_token='old' WHERE reminder_id=?",
                (second["reminder_id"],))
        token2, job2 = self.claim()
        with connection(self.settings.db_path) as conn:
            recovered = conn.execute(
                "SELECT status,error_code,owner_token FROM action_reminders WHERE reminder_id=?",
                (second["reminder_id"],)).fetchone()
        finish_cycle(self.manager,self.context,token2,job2)
        self.assertEqual(tuple(recovered),
                         ("dead","reminder_delivery_unknown",None))

    def test_dashboard_expired_claim_is_retried_without_duplicate_delivery(self):
        _action, reminder = self.due_reminder()
        with connection(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE action_reminders SET status='claimed',owner_token='old' WHERE reminder_id=?",
                (reminder["reminder_id"],))
        token, job = self.claim()
        with connection(self.settings.db_path) as conn:
            recovered = conn.execute(
                "SELECT status,owner_token FROM action_reminders WHERE reminder_id=?",
                (reminder["reminder_id"],)).fetchone()
        self.assertEqual(tuple(recovered),("retry",None))
        result = process_due_reminders(
            self.manager,self.context,token,Mock(),
            now=datetime(2026,9,29,tzinfo=timezone.utc))
        finish_cycle(self.manager,self.context,token,job)
        self.assertEqual(result["delivered"],1)
        self.assertEqual(list_reminders(
            A,db_path=self.settings.db_path)[0]["attempt_count"],1)

    def test_reminder_retries_are_bounded_and_unknown_is_never_retried(self):
        self.manager.settings = replace(
            self.settings,telegram_action_reminders_enabled=True,
            max_processing_attempts=2)
        self.context = self.manager.worker_context()
        _action, retrying = self.due_reminder(
            channel="telegram",title="Retry bounded")
        token, job = self.claim()
        first = process_due_reminders(
            self.manager,self.context,token,
            Mock(return_value=Delivery("retry","notification_transient")),
            now=datetime(2026,9,29,tzinfo=timezone.utc))
        with connection(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE action_reminders SET next_retry_at=0 WHERE reminder_id=?",
                (retrying["reminder_id"],))
        second = process_due_reminders(
            self.manager,self.context,token,
            Mock(return_value=Delivery("retry","notification_transient")),
            now=datetime(2026,9,29,tzinfo=timezone.utc))
        _other, unknown = self.due_reminder(
            channel="telegram",title="Ambiguous delivery")
        ambiguous = process_due_reminders(
            self.manager,self.context,token,
            Mock(return_value=Delivery("unknown","provider_timeout")),
            now=datetime(2026,9,29,tzinfo=timezone.utc))
        finish_cycle(self.manager,self.context,token,job)
        self.assertEqual(first["retry"],1)
        self.assertEqual(second["dead"],1)
        self.assertEqual(ambiguous["dead"],1)
        with connection(self.settings.db_path) as conn:
            rows = {
                row["reminder_id"]: (row["status"],row["error_code"])
                for row in conn.execute(
                    "SELECT reminder_id,status,error_code FROM action_reminders")
            }
        self.assertEqual(rows[retrying["reminder_id"]],
                         ("dead","reminder_retry_exhausted"))
        self.assertEqual(rows[unknown["reminder_id"]],
                         ("dead","reminder_delivery_unknown"))

    def test_logout_during_telegram_delivery_cannot_commit_late_result(self):
        self.manager.settings = replace(
            self.settings,telegram_action_reminders_enabled=True)
        self.context = self.manager.worker_context()
        _action, reminder = self.due_reminder(channel="telegram")
        token, _job = self.claim()
        browser, _ = self.manager.session(self.session_token)

        def logout(*args, **kwargs):
            self.manager.logout(browser)
            return Delivery("sent",message_id="late")

        with self.assertRaises(WorkCancelled):
            process_due_reminders(
                self.manager,self.context,token,logout,
                now=datetime(2026,9,29,tzinfo=timezone.utc))
        with connection(self.settings.db_path) as conn:
            status = conn.execute(
                "SELECT status FROM action_reminders WHERE reminder_id=?",
                (reminder["reminder_id"],)).fetchone()[0]
        self.assertEqual(status,"claimed")

    def test_account_switch_during_delivery_cannot_cross_account_boundary(self):
        self.manager.settings = replace(
            self.settings,telegram_action_reminders_enabled=True)
        self.context = self.manager.worker_context()
        _action, reminder = self.due_reminder(channel="telegram")
        token, _job = self.claim()
        browser, _ = self.manager.session(self.session_token)

        def switch(*args, **kwargs):
            attempt = self.manager.begin_auth(browser)
            self.manager.finish_auth(attempt,(B,"{}"))
            return Delivery("sent",message_id="late")

        with self.assertRaises(WorkCancelled):
            process_due_reminders(
                self.manager,self.context,token,switch,
                now=datetime(2026,9,29,tzinfo=timezone.utc))
        self.assertEqual(self.manager.worker_context().account_id,B)
        with connection(self.settings.db_path) as conn:
            row = conn.execute(
                "SELECT account_id,status FROM action_reminders WHERE reminder_id=?",
                (reminder["reminder_id"],)).fetchone()
        self.assertEqual(tuple(row),(A,"claimed"))

    def test_reminders_do_not_change_mail_read_workflow_queue_or_heartbeat(self):
        _action, _reminder = self.due_reminder()
        with connection(self.settings.db_path) as conn:
            stamp = utc_timestamp()
            conn.execute(
                """INSERT INTO processing_tasks(
                   account_id,email_id,status,stage,created_at,updated_at)
                   VALUES (?,?,'queued','classify',?,?)""",
                (A,"mail-1",stamp,stamp))
            before = dict(conn.execute(
                "SELECT status,stage,read_required FROM processing_tasks WHERE account_id=? AND email_id=?",
                (A,"mail-1")).fetchone())
        token, job = self.claim()
        process_due_reminders(
            self.manager,self.context,token,Mock(),
            now=datetime(2026,9,29,tzinfo=timezone.utc))
        finish_cycle(self.manager,self.context,token,job)
        with connection(self.settings.db_path) as conn:
            after = dict(conn.execute(
                "SELECT status,stage,read_required FROM processing_tasks WHERE account_id=? AND email_id=?",
                (A,"mail-1")).fetchone())
            heartbeat = conn.execute(
                "SELECT heartbeat_at FROM worker_health WHERE account_id=?",
                (A,)).fetchone()[0]
            notifications = conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE account_id=?",
                (A,)).fetchone()[0]
        self.assertEqual(before,after)
        self.assertIsNotNone(heartbeat)
        self.assertEqual(notifications,0)

    def test_existing_worker_cycle_processes_a_bounded_reminder_batch(self):
        action = self.create_action(title="Worker cycle reminder")
        reminder = schedule_reminder(
            A,action["action_id"],remind_at="2020-01-02T00:00:00Z",
            now=datetime(2020,1,1,tzinfo=timezone.utc),
            db_path=self.settings.db_path)
        config = replace(self.settings,local_only=True,batch_size=2)
        self.manager.settings = config
        with patch.object(
                main,"refresh_gmail",side_effect=AssertionError("No Gmail")), \
             patch.object(
                main,"mark_as_read",side_effect=AssertionError("No read")), \
             patch.object(
                main,"classify_email",side_effect=AssertionError("No AI")):
            result = main._run_agent(
                settings=config,manager=self.manager,model=Mock(),
                collection=EmptyCollection())
        self.assertEqual(result["reminders"]["processed"],1)
        with connection(self.settings.db_path) as conn:
            status = conn.execute(
                "SELECT status FROM action_reminders WHERE reminder_id=?",
                (reminder["reminder_id"],)).fetchone()[0]
        self.assertEqual(status,"delivered")

    def test_action_failure_keeps_classification_and_reconciles_without_ai_retry(self):
        with connection(self.settings.db_path) as conn:
            stamp = SOURCE_TIME
            conn.execute(
                """INSERT INTO processing_tasks(
                   account_id,email_id,status,stage,created_at,updated_at)
                   VALUES (?,?,'queued','classify',?,?)""",
                (A,"mail-1",stamp,stamp))
        token, job = self.claim()
        task = newest_due_tasks(self.manager,self.context,token,1)[0]
        enriched = analysis(candidate(
            confidence="high",due_at="2026-10-02T17:00:00+05:30",
            due_precision="exact_time"))
        cloud = Mock(return_value=Prediction(
            category="UPDATES",outcome="CLASSIFIED",source="gemini",
            model_version="synthetic-phase4-v1",
            analysis=replace(enriched,predicted_category="UPDATES")))
        shadow = Mock(return_value=(Prediction(
            category="UPDATES",outcome="CLASSIFIED",source="local",
            model_version="synthetic-local"),5))
        with patch("src.pipeline.persist_analysis_actions",
                   side_effect=RuntimeError("synthetic derived failure")):
            completed = process_task(
                task,self.manager,self.context,token,Mock(),Mock(),Mock(),
                classifier=cloud,shadow=shadow,notifier=Mock(),marker=Mock(),
                logger=log_email_to_db,validator=Mock(),job_id=job)
        self.assertTrue(completed)
        with connection(self.settings.db_path) as conn:
            saved = conn.execute(
                "SELECT prediction FROM email_logs WHERE account_id=? AND email_id=?",
                (A,"mail-1")).fetchone()[0]
            retry = conn.execute(
                """SELECT 1 FROM processing_attempts
                   WHERE stage='actions' AND outcome='retry'"""
            ).fetchone()
        self.assertEqual(saved,"UPDATES")
        self.assertIsNotNone(retry)
        self.assertEqual(reconcile_email_analysis(
            self.manager,self.context,token,limit=5),1)
        self.assertEqual(len(list_actions(
            A,db_path=self.settings.db_path)),1)
        cloud.assert_called_once()
        self.assertEqual(cloud.call_args.kwargs["source_timestamp"],SOURCE_TIME)
        finish_cycle(self.manager,self.context,token,job)

    def test_existing_inbox_is_not_automatically_backfilled(self):
        existing_analysis = analysis()
        with connection(self.settings.db_path) as conn:
            log_email_to_db(
                "mail-1","Sender","Approval needed",
                "Please approve the launch checklist.",
                Prediction(category="IMPORTANT",outcome="CLASSIFIED",
                           source="gemini",analysis=existing_analysis),
                Prediction(),account_id=A,db_conn=conn)
            from src.email_analysis import save_analysis_result
            save_analysis_result(A,"mail-1",existing_analysis,db_conn=conn)
        token, job = self.claim()
        self.assertEqual(reconcile_email_analysis(
            self.manager,self.context,token,limit=5),0)
        self.assertEqual(list_actions(A,db_path=self.settings.db_path),[])
        finish_cycle(self.manager,self.context,token,job)

    def test_account_purge_cascades_actions_and_reminders(self):
        self.due_reminder()
        browser, _ = self.manager.session(self.session_token)
        self.manager.purge(browser,EmptyCollection())
        with connection(self.settings.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM email_actions WHERE account_id=?",
                (A,)).fetchone()[0],0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM action_reminders WHERE account_id=?",
                (A,)).fetchone()[0],0)


class Phase4ConfigurationTests(unittest.TestCase):
    def test_telegram_reminders_require_dashboard_reminders_and_network_mode(self):
        with self.assertRaises(ValueError):
            Settings(telegram_action_reminders_enabled=True)
        with self.assertRaises(ValueError):
            Settings(
                local_only=True,action_extraction_enabled=True,
                action_reminders_enabled=True,
                telegram_action_reminders_enabled=True)
        valid = Settings(
            action_extraction_enabled=True,action_reminders_enabled=True,
            telegram_action_reminders_enabled=True)
        self.assertTrue(valid.telegram_action_reminders_enabled)


    def test_telegram_reminder_opt_in_loads_from_the_environment(self):
        with patch.dict(os.environ,{
                "MAILMIND_ACTION_EXTRACTION_ENABLED":"true",
                "MAILMIND_ACTION_REMINDERS_ENABLED":"true",
                "MAILMIND_TELEGRAM_ACTION_REMINDERS_ENABLED":"true",
                "MAILMIND_LOCAL_ONLY":"false",
        },clear=True):
            settings = Settings.from_environment()
        self.assertTrue(settings.action_extraction_enabled)
        self.assertTrue(settings.action_reminders_enabled)
        self.assertTrue(settings.telegram_action_reminders_enabled)


if __name__ == "__main__":
    unittest.main()

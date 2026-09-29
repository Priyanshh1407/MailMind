"""Integrated token metering tests using only synthetic data."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from threading import Event, Thread
import json
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from fastapi.testclient import TestClient

from api.app import create_app, manual_prediction
from src import llm_api
from src.account_state import AccountManager, WorkCancelled
from src.config import Settings
from src.database import connection, initialize_database
from src.db_utils import log_email_to_db, update_human_label
from src.email_search import reconcile_search_index
from src.feedback import reconcile_feedback
from src.intelligence_contract import TokenOperation
from src.local_llm import MailMindModel
from src.prediction import ID2LABEL, Prediction
from src.token_usage import (
    TokenRecorder,
    aggregate_token_usage,
    list_token_usage,
    record_token_usage,
    usage_request_prefix,
)


A = "phase2-metering@example.test"
B = "phase2-other@example.test"


class MeteredModel:
    model_loaded = True
    model_version = "synthetic-local-v1"
    load_reason = "ready"
    training_scope = None

    def predict(self, subject, body, *, sender="", account_id=None):
        return self.predict_with_usage(
            subject, body, sender=sender, account_id=account_id
        )[0]

    def predict_with_usage(self, subject, body, *, sender="", account_id=None):
        return (
            Prediction(
                category="UPDATES", outcome="CLASSIFIED", source="local",
                model_version=self.model_version,
            ),
            9,
        )


class IntelligencePhaseTwoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="mailmind-intelligence-phase2-"
        )
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            data_dir=Path(self.temp.name),
            gemini_models=("synthetic-gemini",),
            groq_model="synthetic-groq",
            token_analytics_visible=True,
        )
        initialize_database(self.settings.db_path)
        with connection(self.settings.db_path) as conn:
            conn.execute(
                "INSERT INTO accounts(account_id) VALUES (?)", (A,)
            )
            conn.execute(
                "INSERT INTO accounts(account_id) VALUES (?)", (B,)
            )
            conn.execute("""INSERT INTO email_logs(
                account_id,email_id,sender,subject,body,message_type,
                processing_state,created_at)
                VALUES (?,?,'Synthetic Sender','Synthetic subject',
                'Synthetic body','synthetic','complete',
                '2026-09-27T00:00:00.000000Z')""", (A, "synthetic-email"))
            conn.execute("""UPDATE email_search_index SET indexing_state='indexed'
                WHERE account_id=? AND email_id=?""", (A, "synthetic-email"))
        with llm_api._model_cooldown_lock:
            llm_api._model_cooldowns.clear()

    def recorder(self, name, *, email_id="synthetic-email"):
        return TokenRecorder(
            A, usage_request_prefix(name, A), email_id=email_id,
            db_path=self.settings.db_path,
        )

    def cloud(self, response, recorder, *, before_request=None):
        client = Mock()
        client.models.generate_content.return_value = response
        with (
            patch.object(llm_api, "get_client", return_value=client),
            patch.object(
                llm_api.vector_db, "search_similar_emails", return_value=[]
            ),
            patch.dict(
                llm_api.os.environ, {"GROQ_API_KEY": ""}, clear=False
            ),
        ):
            result = llm_api.classify_email(
                "Synthetic Sender", "Synthetic subject", "Synthetic body",
                account_id=A, settings=self.settings,
                usage_recorder=recorder, before_request=before_request,
            )
        return result

    def provider_events(self, provider):
        return [
            row for row in list_token_usage(A, db_path=self.settings.db_path)
            if row["provider"] == provider
        ]

    def test_exact_gemini_usage_metadata_is_recorded(self):
        response = SimpleNamespace(
            text='{"category":"IMPORTANT"}',
            usage_metadata=SimpleNamespace(
                prompt_token_count=101,
                candidates_token_count=7,
                total_token_count=111,
                cached_content_token_count=3,
                thoughts_token_count=3,
            ),
        )
        result = self.cloud(response, self.recorder("gemini-exact"))
        self.assertEqual((result.category, result.outcome),
                         ("IMPORTANT", "CLASSIFIED"))
        event = self.provider_events("gemini")[0]
        self.assertEqual(
            (event["input_tokens"], event["output_tokens"],
             event["total_tokens"], event["count_method"], event["outcome"]),
            (101, 7, 111, "provider_reported", "success"),
        )
        self.assertEqual(
            event["metadata"], {"cached_tokens": 3, "thinking_tokens": 3}
        )

    def test_absent_and_malformed_provider_usage_remain_unknown(self):
        absent = SimpleNamespace(
            text='{"category":"UPDATES"}', usage_metadata=None
        )
        malformed = SimpleNamespace(
            text='{"category":"UPDATES"}',
            usage_metadata={"prompt_token_count": "ten"},
        )
        self.cloud(absent, self.recorder("gemini-absent"))
        self.cloud(malformed, self.recorder("gemini-malformed"))
        events = self.provider_events("gemini")
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertEqual(event["count_method"], "unavailable")
            self.assertIsNone(event["input_tokens"])
            self.assertIsNone(event["output_tokens"])
            self.assertIsNone(event["total_tokens"])
        result = aggregate_token_usage(
            A, window="month", timezone_name="Asia/Kolkata",
            now=__import__("datetime").datetime(
                2026, 9, 27, tzinfo=__import__("datetime").timezone.utc
            ), db_path=self.settings.db_path,
        )
        gemini = next(
            row for row in result["providers"] if row["key"] == "gemini"
        )
        self.assertEqual(
            (gemini["event_count"], gemini["unknown_events"],
             gemini["total_tokens"]),
            (2, 2, 0),
        )

    def test_fallback_records_gemini_attempt_and_exact_groq_usage(self):
        quota = RuntimeError("synthetic quota")
        quota.code = 429
        client = Mock()
        client.models.generate_content.side_effect = quota
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content":
                                      '{"category":"IMPORTANT"}'}}],
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 6,
                "total_tokens": 86,
                "prompt_tokens_details": {"cached_tokens": 4},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        }
        with (
            patch.object(llm_api, "get_client", return_value=client),
            patch.object(
                llm_api.vector_db, "search_similar_emails", return_value=[]
            ),
            patch.dict(
                llm_api.os.environ,
                {"GROQ_API_KEY": "synthetic-key"}, clear=False
            ),
            patch("requests.post", return_value=response),
        ):
            result = llm_api.classify_email(
                "Synthetic Sender", "Synthetic subject", "Synthetic body",
                account_id=A, settings=self.settings,
                usage_recorder=self.recorder("fallback"),
            )
        self.assertEqual((result.category, result.source),
                         ("IMPORTANT", "groq"))
        cloud = sorted(
            [row for row in list_token_usage(
                A, db_path=self.settings.db_path
            ) if row["provider"] in ("gemini", "groq")],
            key=lambda row: row["provider"],
        )
        self.assertEqual(len(cloud), 2)
        gemini, groq = cloud
        self.assertEqual((gemini["provider"], gemini["outcome"],
                          gemini["count_method"]),
                         ("gemini", "failed", "unavailable"))
        self.assertEqual(
            (groq["provider"], groq["input_tokens"], groq["output_tokens"],
             groq["total_tokens"], groq["outcome"]),
            ("groq", 80, 6, 86, "success"),
        )
        self.assertEqual(
            groq["metadata"], {"cached_tokens": 4, "thinking_tokens": 2}
        )

    def test_timeout_records_once_and_late_completion_cannot_publish(self):
        started, release = Event(), Event()
        response = SimpleNamespace(
            text='{"category":"UPDATES"}',
            usage_metadata=SimpleNamespace(
                prompt_token_count=20,
                candidates_token_count=4,
                total_token_count=24,
            ),
        )
        client = Mock()

        def generate(**kwargs):
            started.set()
            release.wait(2)
            return response

        client.models.generate_content.side_effect = generate
        threads = []

        def timeout_run(function, timeout):
            thread = Thread(target=function, daemon=True)
            threads.append(thread)
            thread.start()
            self.assertTrue(started.wait(1))
            raise TimeoutError("synthetic timeout")

        with (
            patch.object(llm_api, "get_client", return_value=client),
            patch.object(
                llm_api.vector_db, "search_similar_emails", return_value=[]
            ),
            patch.object(
                llm_api.GEMINI_CALLS[0], "run", side_effect=timeout_run
            ),
            patch.dict(
                llm_api.os.environ, {"GROQ_API_KEY": ""}, clear=False
            ),
        ):
            result = llm_api.classify_email(
                "Synthetic Sender", "Synthetic subject", "Synthetic body",
                account_id=A, settings=self.settings,
                usage_recorder=self.recorder("late-timeout"),
            )
        self.assertEqual(result.outcome, "UNAVAILABLE")
        before = self.provider_events("gemini")
        self.assertEqual(len(before), 1)
        self.assertEqual(
            (before[0]["outcome"], before[0]["count_method"]),
            ("timeout", "unavailable"),
        )
        release.set()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(self.provider_events("gemini")), 1)

    def test_cancellation_after_request_records_captured_usage(self):
        calls = {"count": 0}

        @contextmanager
        def guard():
            calls["count"] += 1
            yield
            if calls["count"] == 2:
                raise WorkCancelled()

        response = SimpleNamespace(
            text='{"category":"UPDATES"}',
            usage_metadata=SimpleNamespace(
                prompt_token_count=12,
                candidates_token_count=3,
                total_token_count=15,
            ),
        )
        with self.assertRaises(WorkCancelled):
            self.cloud(
                response, self.recorder("cancel-after-request"),
                before_request=guard,
            )
        event = self.provider_events("gemini")[0]
        self.assertEqual(
            (event["outcome"], event["total_tokens"],
             event["count_method"]),
            ("cancelled", 15, "provider_reported"),
        )

    def test_local_classifier_returns_exact_tokenizer_input_count(self):
        model = object.__new__(MailMindModel)
        model.model_loaded = True
        model.model_version = "synthetic-three-class"
        model.training_scope = "private_user_approved_inbox"
        model.id2label = ID2LABEL
        token_ids = Mock()
        token_ids.numel.return_value = 7
        model.tokenizer = Mock(return_value={"input_ids": token_ids})
        model.model = Mock()
        torch = MagicMock()
        functional = MagicMock()
        torch.nn.functional = functional
        torch.argmax.return_value.item.return_value = 2
        functional.softmax.return_value[0][2].item.return_value = 0.9
        with patch.dict(
            "sys.modules",
            {"torch": torch, "torch.nn": torch.nn,
             "torch.nn.functional": functional},
        ):
            prediction, input_tokens = model.predict_with_usage(
                "Synthetic subject", "Synthetic body", account_id=A
            )
        self.assertEqual((prediction.category, input_tokens), ("UPDATES", 7))

    def test_document_embedding_batch_is_estimated_once(self):
        manager = AccountManager(self.settings)
        token, csrf = manager.open_session()
        context, _ = manager.session(token, csrf)
        manager.finish_auth(manager.begin_auth(context), (A, "{}"))
        context = manager.worker_context()
        for index in range(2):
            log_email_to_db(
                f"indexed-{index}", "Synthetic Sender",
                f"Synthetic subject {index}", "Synthetic body",
                Prediction(category="UPDATES", outcome="CLASSIFIED"),
                Prediction(category="UPDATES", outcome="CLASSIFIED"),
                account_id=A, db_path=self.settings.db_path,
            )
        collection = Mock()
        result = reconcile_search_index(
            manager, context, lambda: collection, limit=10,
            embedding_provider=lambda values: [[0.1, 0.2] for _ in values],
            bounded=False,
        )
        self.assertEqual(result, {"indexed": 2, "failed": 0})
        events = [
            row for row in self.provider_events("embedding")
            if row["operation"] == TokenOperation.DOCUMENT_EMBEDDING.value
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["metadata"]["document_count"], 2)
        self.assertEqual(events[0]["count_method"], "estimated")
        self.assertGreater(events[0]["total_tokens"], 0)

    def test_feedback_document_embedding_is_metered(self):
        manager = AccountManager(self.settings)
        token, csrf = manager.open_session()
        context, _ = manager.session(token, csrf)
        manager.finish_auth(manager.begin_auth(context), (A, "{}"))
        context = manager.worker_context()
        log_email_to_db(
            "feedback-document", "Synthetic Sender", "Feedback subject",
            "Feedback body",
            Prediction(category="UPDATES", outcome="CLASSIFIED"),
            Prediction(category="UPDATES", outcome="CLASSIFIED"),
            account_id=A, db_path=self.settings.db_path,
        )
        update_human_label(
            "feedback-document", "IMPORTANT", account_id=A,
            db_path=self.settings.db_path,
        )
        collection = Mock()
        result = reconcile_feedback(
            manager, context, lambda: collection, limit=10
        )
        self.assertEqual(result, {"indexed": 1, "failed": 0})
        events = [
            row for row in self.provider_events("embedding")
            if row["operation"] == TokenOperation.DOCUMENT_EMBEDDING.value
            and row["email_id"] == "feedback-document"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(
            (events[0]["count_method"], events[0]["outcome"]),
            ("estimated", "success"),
        )
        self.assertGreater(events[0]["total_tokens"], 0)

    def api_client(self, *, visible=True, search_collection=None):
        settings = Settings(
            data_dir=Path(self.temp.name),
            token_analytics_visible=visible,
        )
        model = MeteredModel()
        application = create_app(
            settings=settings,
            model_factory=Mock(return_value=model),
            vector_factory=Mock(return_value=Mock()),
            search_vector_factory=Mock(
                return_value=search_collection or Mock()
            ),
        )
        client = TestClient(application, base_url="http://localhost")
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        client.headers["Origin"] = "http://localhost:5173"
        csrf = client.post("/session").json()["csrf_token"]
        client.headers["X-CSRF-Token"] = csrf
        manager = application.state.accounts
        context, _ = manager.session(client.cookies.get("mailmind_session"))
        manager.finish_auth(manager.begin_auth(context), (A, "{}"))
        return client, application

    def test_manual_usage_is_visible_account_scoped_and_content_free(self):
        client, _application = self.api_client()
        secret = "SYNTHETIC_PRIVATE_PROMPT_92F1"
        prediction = client.post(
            "/predict", json={"subject": "Synthetic", "body": secret}
        )
        self.assertEqual(prediction.status_code, 200)
        record_token_usage(
            account_id=B, request_id="other-account-event",
            provider="local", model_version="synthetic-local-v1",
            operation="manual_prediction", input_tokens=999,
            output_tokens=0, total_tokens=999,
            count_method="tokenizer_counted", outcome="success",
            db_path=self.settings.db_path,
        )
        response = client.get("/analytics/tokens?window=month")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["account_id"], A)
        self.assertEqual(data["totals"]["event_count"], 1)
        self.assertEqual(data["totals"]["total_tokens"], 9)
        self.assertEqual(data["local_processed_tokens"], 9)
        serialized = json.dumps(data)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("request_id", serialized)
        self.assertNotIn("metadata", serialized)

    def test_semantic_query_is_metered_and_analytics_can_be_hidden(self):
        search = Mock()
        search.query.return_value = {
            "metadatas": [[]],
            "distances": [[]],
        }
        client, _application = self.api_client(search_collection=search)
        log_email_to_db(
            "searchable", "Synthetic Sender", "Needle subject",
            "Synthetic searchable body",
            Prediction(category="UPDATES", outcome="CLASSIFIED"),
            Prediction(category="UPDATES", outcome="CLASSIFIED"),
            account_id=A, db_path=self.settings.db_path,
        )
        response = client.get("/emails?search=Needle")
        self.assertEqual(response.status_code, 200)
        queries = [
            row for row in self.provider_events("embedding")
            if row["operation"] == TokenOperation.QUERY_EMBEDDING.value
        ]
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]["count_method"], "estimated")

        hidden_root = tempfile.TemporaryDirectory(
            prefix="mailmind-hidden-token-analytics-"
        )
        self.addCleanup(hidden_root.cleanup)
        hidden_settings = Settings(data_dir=Path(hidden_root.name))
        hidden_app = create_app(
            settings=hidden_settings,
            model_factory=Mock(return_value=MeteredModel()),
            vector_factory=Mock(return_value=Mock()),
        )
        with TestClient(hidden_app, base_url="http://localhost") as hidden:
            hidden.headers["Origin"] = "http://localhost:5173"
            csrf = hidden.post("/session").json()["csrf_token"]
            hidden.headers["X-CSRF-Token"] = csrf
            manager = hidden_app.state.accounts
            context, _ = manager.session(
                hidden.cookies.get("mailmind_session")
            )
            manager.finish_auth(manager.begin_auth(context), (A, "{}"))
            self.assertEqual(
                hidden.get("/analytics/tokens").status_code, 404
            )


if __name__ == "__main__":
    unittest.main()

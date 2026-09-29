"""Phase 0 contract and synthetic-fixture checks for integrated features."""
from datetime import datetime
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src.config import Settings
from src.intelligence_contract import (
    ACTION_STATUS_TRANSITIONS,
    CLOUD_BILLED_TOKEN_PROVIDERS,
    DATE_ONLY_REMINDER_LOCAL_HOUR,
    DEFAULT_TIMEZONE,
    EXACT_TIME_REMINDER_LEAD_MINUTES,
    LOCAL_PROCESSED_TOKEN_PROVIDERS,
    MAX_ACTIONS_PER_EMAIL,
    MAX_ACTION_DESCRIPTION_CHARS,
    MAX_ACTION_EVIDENCE_CHARS,
    MAX_ACTION_TITLE_CHARS,
    MAX_AUTOMATIC_DEADLINE_DAYS,
    MAX_EXPLANATION_SIGNALS,
    MAX_EXPLANATION_SUMMARY_CHARS,
    WEEK_START,
    ActionStatus,
    ActionType,
    AnalysisSource,
    ConfidenceBand,
    DuePrecision,
    ExplanationSignal,
    ReminderChannel,
    ReminderStatus,
    TokenCountMethod,
    TokenOperation,
    TokenOutcome,
    TokenProvider,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "intelligence_features.json"


class IntelligencePhaseZeroTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_action_wire_values_are_frozen(self):
        self.assertEqual({item.value for item in ActionType}, {
            "reply_required", "approval_required", "payment_required",
            "document_required", "meeting", "review_required",
            "follow_up_required", "general_task",
        })
        self.assertEqual({item.value for item in ActionStatus}, {
            "open", "completed", "dismissed", "snoozed",
        })
        self.assertEqual({item.value for item in DuePrecision}, {
            "exact_time", "date_only", "relative", "unknown",
        })
        self.assertEqual({item.value for item in ConfidenceBand}, {
            "low", "medium", "high",
        })

    def test_reminder_and_lifecycle_contract_is_frozen(self):
        self.assertEqual(set(ACTION_STATUS_TRANSITIONS), {item.value for item in ActionStatus})
        self.assertEqual(ACTION_STATUS_TRANSITIONS["completed"], {"open"})
        self.assertEqual(ACTION_STATUS_TRANSITIONS["dismissed"], {"open"})
        self.assertIn("open", ACTION_STATUS_TRANSITIONS["snoozed"])
        self.assertNotIn("open", ACTION_STATUS_TRANSITIONS["open"])
        self.assertEqual({item.value for item in ReminderChannel}, {"dashboard", "telegram"})
        self.assertEqual({item.value for item in ReminderStatus}, {
            "scheduled", "claimed", "delivered", "dismissed", "retry", "dead",
        })
        self.assertEqual(EXACT_TIME_REMINDER_LEAD_MINUTES, 30)
        self.assertEqual(DATE_ONLY_REMINDER_LOCAL_HOUR, 9)

    def test_analysis_and_token_taxonomies_are_frozen(self):
        self.assertEqual({item.value for item in AnalysisSource}, {
            "gemini", "groq", "local_heuristic", "system",
        })
        self.assertGreaterEqual(len(ExplanationSignal), 10)
        self.assertEqual({item.value for item in TokenProvider}, {
            "gemini", "groq", "local", "embedding",
        })
        self.assertEqual({item.value for item in TokenOperation}, {
            "classification_analysis", "local_shadow", "document_embedding",
            "query_embedding", "manual_prediction", "action_reanalysis",
        })
        self.assertEqual({item.value for item in TokenCountMethod}, {
            "provider_reported", "tokenizer_counted", "estimated", "unavailable",
        })
        self.assertEqual({item.value for item in TokenOutcome}, {
            "success", "failed", "timeout", "cancelled",
        })
        self.assertFalse(CLOUD_BILLED_TOKEN_PROVIDERS & LOCAL_PROCESSED_TOKEN_PROVIDERS)
        self.assertEqual(CLOUD_BILLED_TOKEN_PROVIDERS | LOCAL_PROCESSED_TOKEN_PROVIDERS,
                         {item.value for item in TokenProvider})

    def test_safe_rollout_defaults_are_integrated_into_settings(self):
        settings = Settings()
        self.assertFalse(settings.action_extraction_enabled)
        self.assertFalse(settings.action_reminders_enabled)
        self.assertTrue(settings.token_collection_enabled)
        self.assertFalse(settings.token_analytics_visible)
        self.assertFalse(settings.explanations_visible)
        self.assertEqual(settings.default_timezone, DEFAULT_TIMEZONE)
        self.assertEqual(settings.week_start, WEEK_START)

    def test_environment_flags_are_strict_and_dependencies_are_enforced(self):
        variables = {
            "MAILMIND_LOCAL_ONLY": "false",
            "MAILMIND_ACTION_EXTRACTION_ENABLED": "true",
            "MAILMIND_ACTION_REMINDERS_ENABLED": "true",
            "MAILMIND_TOKEN_COLLECTION_ENABLED": "true",
            "MAILMIND_TOKEN_ANALYTICS_VISIBLE": "true",
            "MAILMIND_EXPLANATIONS_VISIBLE": "true",
        }
        with patch.dict(os.environ, variables, clear=True):
            settings = Settings.from_environment()
        self.assertTrue(settings.action_extraction_enabled)
        self.assertTrue(settings.action_reminders_enabled)
        self.assertTrue(settings.token_analytics_visible)
        self.assertTrue(settings.explanations_visible)
        with self.assertRaisesRegex(ValueError, "Action reminders require"):
            Settings(action_reminders_enabled=True)
        with self.assertRaisesRegex(ValueError, "visibility requires"):
            Settings(token_collection_enabled=False, token_analytics_visible=True)
        with patch.dict(os.environ, {"MAILMIND_ACTION_EXTRACTION_ENABLED": "yes"}, clear=True):
            with self.assertRaisesRegex(ValueError, "must be true or false"):
                Settings.from_environment()

    def test_fixture_inventory_is_synthetic_and_covers_required_scenarios(self):
        scenarios = {item["scenario"] for item in self.fixtures}
        self.assertTrue({
            "approval", "deadline", "reply", "meeting", "ambiguous_date",
            "spam", "no_action_update", "prompt_injection", "provider_failure",
        }.issubset(scenarios))
        self.assertEqual(
            {action["type"] for item in self.fixtures for action in item["expected_actions"]},
            {item.value for item in ActionType},
        )
        for item in self.fixtures:
            with self.subTest(item=item["id"]):
                self.assertTrue(item["id"].startswith("synthetic-feature-"))
                self.assertIn("example.test", item["sender"])
                self.assertNotIn("@gmail.com", item["sender"].lower())
                self.assertIn(item["provider_outcome"], {"classified", "provider_timeout"})
                self.assertIn(item["expected_category"], {None, "IMPORTANT", "UPDATES", "SPAM"})

    def test_fixture_outputs_obey_bounded_contract(self):
        signals = {item.value for item in ExplanationSignal}
        action_types = {item.value for item in ActionType}
        due_precisions = {item.value for item in DuePrecision}
        confidences = {item.value for item in ConfidenceBand}
        sources = {item.value for item in AnalysisSource}
        for item in self.fixtures:
            with self.subTest(item=item["id"]):
                explanation = item["explanation"]
                self.assertIn(explanation["source"], sources)
                self.assertLessEqual(len(explanation["summary"]), MAX_EXPLANATION_SUMMARY_CHARS)
                self.assertLessEqual(len(explanation["signals"]), MAX_EXPLANATION_SIGNALS)
                self.assertTrue(set(explanation["signals"]).issubset(signals))
                self.assertLessEqual(len(item["expected_actions"]), MAX_ACTIONS_PER_EMAIL)
                for action in item["expected_actions"]:
                    self.assertIn(action["type"], action_types)
                    self.assertIn(action["due_precision"], due_precisions)
                    self.assertIn(action["confidence"], confidences)
                    self.assertLessEqual(len(action["evidence"]), MAX_ACTION_EVIDENCE_CHARS)
                    self.assertLessEqual(len(action.get("title", "")), MAX_ACTION_TITLE_CHARS)
                    self.assertLessEqual(len(action.get("description", "")), MAX_ACTION_DESCRIPTION_CHARS)

    def test_automatic_reminder_fixtures_are_conservative(self):
        for item in self.fixtures:
            received = datetime.fromisoformat(item["received_at"])
            for action in item["expected_actions"]:
                with self.subTest(item=item["id"], action=action["type"]):
                    if not action["automatic_reminder"]:
                        continue
                    self.assertEqual(action["confidence"], "high")
                    self.assertTrue(action["evidence"])
                    self.assertNotEqual(action["due_precision"], "unknown")
                    self.assertIsNotNone(action["due_at"])
                    due = datetime.fromisoformat(action["due_at"])
                    self.assertIsNotNone(received.tzinfo)
                    self.assertIsNotNone(due.tzinfo)
                    self.assertGreater(due, received)
                    self.assertLessEqual((due - received).days, MAX_AUTOMATIC_DEADLINE_DAYS)

    def test_hostile_and_failure_fixtures_cannot_create_actions(self):
        indexed = {item["scenario"]: item for item in self.fixtures}
        self.assertEqual(indexed["prompt_injection"]["expected_actions"], [])
        self.assertEqual(indexed["prompt_injection"]["expected_category"], "SPAM")
        failure = indexed["provider_failure"]
        self.assertIsNone(failure["expected_category"])
        self.assertEqual(failure["explanation"]["source"], "system")
        self.assertEqual(failure["expected_actions"], [])


if __name__ == "__main__":
    unittest.main()

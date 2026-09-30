import tempfile
import unittest
from pathlib import Path

from src.database import connection, initialize_database, utc_timestamp
from src.inbox_training import human_labelled_subset, load_user_approved_rows


class ApprovedInboxTrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "mail.db"
        initialize_database(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def mail(self, identity, prediction, *, subject=None, body=None,
             human_label=None, sender="sender@example.test"):
        with connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO email_logs(account_id,email_id,sender,subject,body,prediction,human_label,created_at)
                   VALUES ('legacy-unassigned',?,?,?,?,?,?,?)""",
                (identity, sender, subject or identity, body or ("body " + identity),
                 prediction, human_label, utc_timestamp()),
            )

    def attempt(self, identity, category, source):
        with connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO prediction_attempts(account_id,email_id,category,outcome,source,metadata,created_at)
                   VALUES ('legacy-unassigned',?,?,'CLASSIFIED',?,'{}',?)""",
                (identity, category, source, utc_timestamp()),
            )

    def test_human_feedback_overrides_approved_prediction(self):
        self.mail("one", "UPDATES", human_label="IMPORTANT")
        rows, summary = load_user_approved_rows(self.db_path)
        self.assertEqual(rows[0]["human_label"], "IMPORTANT")
        self.assertEqual(rows[0]["label_source"], "human")
        self.assertEqual(
            summary["label_policy"],
            "explicit_human_feedback_else_non_local_approved_prediction",
        )

    def test_local_model_predictions_never_become_training_labels(self):
        # ML-01: a local-only run stores the local model's own output as the
        # prediction. Training on it would teach the model its own mistakes.
        self.mail("local", "SPAM")
        self.attempt("local", "SPAM", "local")
        self.mail("cloud", "UPDATES")
        self.attempt("cloud", "UPDATES", "gemini")
        self.mail("corrected", "SPAM", human_label="IMPORTANT")
        self.attempt("corrected", "SPAM", "local")
        self.mail("legacy", "UPDATES")
        rows, summary = load_user_approved_rows(self.db_path)
        self.assertEqual(
            {row["subject"]: row["label_source"] for row in rows},
            {"cloud": "cloud", "corrected": "human", "legacy": "unrecorded"})
        self.assertEqual(summary["excluded_local_self_labels"], 1)
        self.assertEqual(summary["label_provenance"],
                         {"cloud": 1, "human": 1, "unrecorded": 1})

    def test_label_source_is_the_first_successful_decision(self):
        self.mail("one", "UPDATES")
        self.attempt("one", None, "gemini")
        self.attempt("one", "UPDATES", "groq")
        self.attempt("one", "SPAM", "local")
        rows, _ = load_user_approved_rows(self.db_path)
        self.assertEqual(rows[0]["label_source"], "cloud")

    def test_human_subset_keeps_predictions_aligned(self):
        rows = [{"label_source": "cloud", "human_label": "SPAM"},
                {"label_source": "human", "human_label": "IMPORTANT"},
                {"label_source": "human", "human_label": "UPDATES"}]
        truth, predicted = human_labelled_subset(
            rows, ["SPAM", "IMPORTANT", "SPAM"])
        self.assertEqual((truth, predicted),
                         (["IMPORTANT", "UPDATES"], ["IMPORTANT", "SPAM"]))

    def test_deduplicates_equal_labels_and_rejects_text_conflicts(self):
        self.mail("one", "SPAM", subject="same", body="same")
        self.mail("two", "SPAM", subject="same", body="same")
        self.mail("three", "UPDATES", subject="conflict", body="same")
        self.mail("four", "IMPORTANT", subject="conflict", body="same")
        rows, summary = load_user_approved_rows(self.db_path)
        self.assertEqual([row["human_label"] for row in rows], ["SPAM"])
        self.assertEqual(summary["duplicate_or_conflicting_rows_removed"], 3)
        self.assertEqual(summary["conflicting_text_groups_removed"], 1)

    def test_sender_is_part_of_model_input_and_template_grouping(self):
        self.mail("one", "IMPORTANT", subject="Invoice 1234", sender="A <a@vendor.test>")
        self.mail("two", "IMPORTANT", subject="Invoice 5678", sender="B <b@vendor.test>")
        rows, _ = load_user_approved_rows(self.db_path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["group_id"], rows[1]["group_id"])
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])


if __name__ == "__main__":
    unittest.main()

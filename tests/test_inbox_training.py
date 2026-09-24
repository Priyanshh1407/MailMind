import tempfile
import unittest
from pathlib import Path

from src.database import connection, initialize_database, utc_timestamp
from src.inbox_training import load_user_approved_rows


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

    def test_human_feedback_overrides_approved_prediction(self):
        self.mail("one", "UPDATES", human_label="IMPORTANT")
        rows, summary = load_user_approved_rows(self.db_path)
        self.assertEqual(rows[0]["human_label"], "IMPORTANT")
        self.assertEqual(
            summary["label_policy"],
            "explicit_human_feedback_else_user_approved_prediction",
        )

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

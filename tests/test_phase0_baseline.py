import ast
import base64
import contextlib
import io
import os
import re
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import ROOT, FakeGmail, fixtures, gmail_message, load_function
from src.setup_db import create_database
from src.db_utils import update_human_label
from src.email_client import get_unread_emails


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mailmind-phase0-")
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "emails.db")
        self.cases = {case["kind"]: case for case in fixtures()}
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        # Any accidental network use during these tests must fail immediately.
        self.addCleanup(patch.stopall)
        patch.object(socket, "socket", side_effect=AssertionError("Network forbidden in baseline tests")).start()
        patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden in baseline tests")).start()
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden in baseline tests")).start()

    def create_db(self):
        create_database(self.db_path)

    def table_names(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]

    def ingest(self, *kinds):
        return get_unread_emails(FakeGmail([self.cases[kind] for kind in kinds]), max_results=10).emails

    def unrelated_vote(self):
        from src import vector_db
        with patch.object(vector_db,'search_similar_emails',return_value=[{'label':'SPAM','distance':1.95}]):
            return vector_db.get_knn_prediction('Interview confirmation','Please confirm tomorrow')

    def mask(self, text):
        return load_function("scripts/sanitize_data.py", "mask_financial_pii", {"re": re})(text)

    def test_all_python_sources_parse(self):
        for folder in ("api", "src", "scripts"):
            for path in (ROOT / folder).glob("*.py"):
                with self.subTest(path=str(path.relative_to(ROOT))):
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_fixture_inventory_covers_required_cases(self):
        self.assertEqual(set(self.cases), {"plain_text", "nested_mime", "html_only", "encoded", "oversized", "malformed", "adversarial"})
        for case in self.cases.values():
            self.assertIn("@", case["sender"])
            self.assertIn("example.test", case["sender"])
            self.assertTrue(case["id"].startswith("synthetic-"))

    def test_encoded_and_oversized_fixtures_are_meaningful(self):
        encoded = gmail_message(self.cases["encoded"])["payload"]["body"]["data"]
        self.assertEqual(base64.urlsafe_b64decode(encoded).decode("iso-8859-1"), self.cases["encoded"]["body"])
        long_body = base64.urlsafe_b64decode(gmail_message(self.cases["oversized"])["payload"]["body"]["data"]).decode()
        self.assertGreater(len(long_body), 200)
        self.assertGreater(long_body.index("URGENT"), 200)

    def test_fresh_database_initialization(self):
        self.create_db()
        self.assertIn("email_logs", self.table_names())

    def test_plain_text_ingestion(self):
        rows = self.ingest("plain_text")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body_snippet"], self.cases["plain_text"]["body"])

    def test_empty_retrieval_abstains(self):
        from src.retrieval_policy import RetrievalPolicy, vote
        fn = load_function("src/vector_db.py", "get_knn_prediction", {"search_similar_emails": lambda *args, **kwargs: [], "RetrievalPolicy":RetrievalPolicy, "vote":vote})
        self.assertIsNone(fn("No precedents", "Synthetic input"))

    # Characterizations keep unexpected errors from masquerading as known bugs.
    # Retire/update each characterization and remove expectedFailure when fixed.
    def test_characterize_unrelated_neighbor(self):
        self.assertIsNone(self.unrelated_vote())

    def test_finding_1_unrelated_neighbor_must_abstain(self):
        self.assertIsNone(self.unrelated_vote())

    def test_database_initialization_is_repeatable(self):
        Path(self.db_path).touch()
        self.create_db()
        original = self.table_names()
        self.create_db()
        self.assertEqual(self.table_names(), original)

    def test_finding_9_existing_empty_database_must_initialize(self):
        Path(self.db_path).touch()
        self.create_db()
        self.assertIn("email_logs", self.table_names())

    def test_nested_mime_uses_plain_body_without_warnings(self):
        row = self.ingest('nested_mime')[0]
        self.assertEqual(row['body_kind'],'plain')
        self.assertEqual(row['parse_warnings'],[])

    def test_finding_10_nested_mime_must_preserve_body(self):
        self.assertEqual(self.ingest("nested_mime")[0]["body_snippet"], self.cases["nested_mime"]["body"])

    def test_characterize_currency_redaction(self):
        self.assertEqual(self.mask("Balance Rs. 5000"), "Balance [AMOUNT]")

    def test_finding_35_currency_must_be_fully_redacted(self):
        self.assertEqual(self.mask("Balance Rs. 5000"), "Balance [AMOUNT]")

    def test_characterize_address_redaction(self):
        self.assertEqual(self.mask("person@example.test"), "[EMAIL]")

    def test_finding_35_address_must_not_be_partially_redacted(self):
        self.assertNotIn(".test", self.mask("person@example.test"))

    def update_missing_email(self):
        self.create_db()
        return update_human_label("synthetic-missing", "IMPORTANT", db_path=self.db_path)

    def test_characterize_missing_email_update(self):
        with self.assertRaises(LookupError):
            self.update_missing_email()
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM email_logs").fetchone()[0], 0)

    def test_finding_21_missing_email_must_be_rejected(self):
        # Provisional service contract: raise a typed lookup failure; Phase 4
        # maps the final chosen exception to HTTP 404 and updates this check.
        with self.assertRaises(LookupError):
            self.update_missing_email()

"""The clean-release snapshot carries everything the test suite needs."""
import importlib
import tempfile
import unittest
from pathlib import Path

from scripts import check_clean_release

# Scripts whose committed report a reproducibility test compares against.
REPORT_SCRIPTS = ('evaluate_cloud', 'evaluate_risk', 'evaluate_ledger', 'evaluate_agent',
                  'evaluate_follow_ups', 'evaluate_brief', 'redteam')


class SnapshotTests(unittest.TestCase):
    def test_every_committed_report_is_in_the_snapshot(self):
        root = check_clean_release.ROOT
        reports = [importlib.import_module(f'scripts.{name}').REPORT for name in REPORT_SCRIPTS]
        reports.append(root / 'docs' / 'RESILIENCE.md')
        with tempfile.TemporaryDirectory() as directory:
            check_clean_release.snapshot(directory)
            for report in reports:
                with self.subTest(report=report.name):
                    copied = Path(directory) / report.relative_to(root)
                    self.assertTrue(copied.is_file(), f'{report.name} is missing from the clean snapshot')
                    self.assertEqual(copied.read_bytes(), report.read_bytes())


if __name__ == '__main__':
    unittest.main()

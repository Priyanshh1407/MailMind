"""Small offline workload checks; performance numbers are not pass thresholds."""
import unittest
from scripts.measure_local_scale import percentile, scenario
from src.config import Settings


class ScaleTests(unittest.TestCase):
    def test_percentiles_and_empty_sample(self):
        self.assertIsNone(percentile([], .95))
        self.assertEqual(percentile([9, 1, 5, 2], .5), 2)
        self.assertEqual(percentile([9, 1, 5, 2], .95), 9)

    def test_invalid_workload_does_not_create_storage(self):
        for size in [0, -1, 2001, True]:
            with self.assertRaises(ValueError): scenario(size)
        with self.assertRaises(ValueError): scenario(1, -1)

    def test_real_saved_backlog_is_bounded_and_idempotent(self):
        result = scenario(21, reader=False)
        self.assertEqual(result['completed'], 21)
        self.assertEqual([c['completed'] for c in result['cycles']], [20, 1])
        self.assertEqual(result['prediction_attempts'], 21)
        self.assertEqual(result['api']['emails']['samples'], 0)
        self.assertGreaterEqual(result['scheduled_drain_estimate_seconds'], Settings().poll_interval_seconds)
        self.assertTrue(result['all_passed'])

import unittest
from unittest.mock import patch

import datetime
import subprocess

from scripts.rotalog_daily_pipeline import collection_window, run_pipeline


class DailyPipelineTests(unittest.TestCase):
    def test_collection_window_matches_normal_reconciliation_windows(self):
        self.assertEqual(
            (datetime.date(2026, 10, 27), datetime.date(2026, 11, 2)),
            collection_window(datetime.date(2026, 11, 3)),
        )
        self.assertEqual(
            (datetime.date(2026, 10, 9), datetime.date(2026, 11, 7)),
            collection_window(datetime.date(2026, 11, 8)),
        )
        self.assertEqual(
            (datetime.date(2026, 9, 11), datetime.date(2026, 11, 9)),
            collection_window(datetime.date(2026, 11, 10)),
        )

    def test_normal_reconciliation_runs_even_if_unique_collection_fails(self):
        with patch("scripts.rotalog_daily_pipeline.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
        ]) as run:
            self.assertEqual(1, run_pipeline(datetime.date(2026, 11, 3)))

        self.assertEqual(2, run.call_count)
        unique_command = run.call_args_list[0].args[0]
        normal_command = run.call_args_list[1].args[0]
        self.assertIn("-m", unique_command)
        self.assertIn("coleta_unica", unique_command)
        self.assertIn("--refazer", unique_command)
        self.assertIn("2026-10-27", unique_command)
        self.assertIn("2026-11-02", unique_command)
        self.assertIn("--reconciliar-agendado", normal_command)

    def test_normal_failure_takes_precedence_over_unique_status(self):
        with patch("scripts.rotalog_daily_pipeline.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 3),
            subprocess.CompletedProcess([], 2),
        ]):
            self.assertEqual(2, run_pipeline())


if __name__ == "__main__":
    unittest.main()

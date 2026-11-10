from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DayExperimentDeploymentTemplateTests(unittest.TestCase):
    def test_units_are_independent_and_use_required_schedule(self):
        preopen = (ROOT / "deploy" / "taiwan-stock-day-experiment-preopen.timer").read_text(encoding="utf-8")
        postclose = (ROOT / "deploy" / "taiwan-stock-day-experiment-postclose.timer").read_text(encoding="utf-8")
        service = (ROOT / "deploy" / "taiwan-stock-day-experiment-postclose.service").read_text(encoding="utf-8")
        self.assertIn("08:05:00 Asia/Taipei", preopen)
        self.assertIn("16:00:00 Asia/Taipei", postclose)
        self.assertIn("Persistent=true", preopen)
        self.assertIn("RestartSec=15min", service)
        self.assertIn("DAY_EXPERIMENT_ROOT=/opt/taiwan-stock-day-experiment", service)
        self.assertNotIn("taiwan_stock_analysis_platform", service)

    def test_entrypoints_keep_pending_and_append_only_paths(self):
        preopen = (ROOT / "deploy" / "day-experiment-preopen.sh").read_text(encoding="utf-8")
        postclose = (ROOT / "deploy" / "day-experiment-postclose.sh").read_text(encoding="utf-8")
        self.assertIn("build_futures_day_live_snapshot.py", preopen)
        self.assertIn("run_futures_day_forward_prediction.py", preopen)
        self.assertIn("exit 75", postclose)
        self.assertIn("postclose-events.jsonl", postclose)
        self.assertIn("--evaluate-only", postclose)

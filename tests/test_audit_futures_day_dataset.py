from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research import audit_futures_day_dataset as audit
from research import build_futures_day_dataset as day

TAIPEI = ZoneInfo("Asia/Taipei")


class DayAuditTests(unittest.TestCase):
    def test_audit_keeps_flat_out_of_binary_candidates(self):
        item = date(2026, 9, 15)
        cutoff = datetime.combine(item, time(8, 15), TAIPEI)
        row = {"prediction_date": item, "prediction_at": cutoff, "feature_pit_status": "passed_0815",
               "max_feature_available_at": cutoff - timedelta(minutes=1), "direction_class": "flat",
               day.BINARY_COLUMN: None, "target_available_at": cutoff + timedelta(hours=7),
               "contract_selection_evidence_status": "unverified_default_rule_availability",
               "live_readiness": "not_assessed_no_0815_live_snapshot"}
        report = audit.audit([row])
        self.assertEqual(report["flat_excluded_from_binary"], 1)
        self.assertEqual(report["binary_training_candidates"], 0)
        self.assertEqual(report["live_readiness"], {"not_assessed_no_0815_live_snapshot": 1})

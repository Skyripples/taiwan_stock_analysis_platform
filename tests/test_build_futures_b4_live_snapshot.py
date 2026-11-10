from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import build_futures_b4_live_snapshot as builder
from research.run_futures_b4_forward_prediction import normalize_snapshot, snapshot_status


TAIPEI = ZoneInfo("Asia/Taipei")


def daily_row(day: date, *, contract: str = "TX202611", fetched_at: datetime | None = None) -> dict:
    return {
        "trade_date": day, "product": "TX", "session": "regular", "contract_rank": "front", "contract": contract,
        "open": 100.0, "high": 103.0, "low": 99.0, "close": 101.0, "volume": 1000,
        "open_interest": 5000, "days_to_expiry": 12, "is_expiry_week": False,
        "available_at": datetime.combine(day, datetime.min.time(), TAIPEI).replace(hour=15, minute=30).isoformat(),
        "fetched_at": (fetched_at or datetime.combine(day, datetime.min.time(), TAIPEI).replace(hour=16)).isoformat(),
        "_source_path": f"fixture/{day}.jsonl.gz", "_source_sha256": f"hash-{day}",
    }


class B4LiveSnapshotTests(unittest.TestCase):
    def test_snapshot_is_runner_compatible_and_uses_prior_source_only(self):
        sessions = [date(2026, 1, 2) + timedelta(days=index) for index in range(7)]
        day = sessions[-1]
        rows = [daily_row(item) for item in sessions[:-1]]
        generated = datetime(2026, 1, 8, 8, 20, tzinfo=TAIPEI)
        with patch.object(builder, "is_taifex_trading_day", return_value=True), \
             patch.object(builder, "get_taifex_trading_days_in_range", return_value=(sessions, [])):
            snapshot = builder.build_snapshot(day, rows, generated_at=generated, mode="live")
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["contract"], "TX202611")
        self.assertNotIn(day.isoformat(), [item["source_date"] for item in snapshot["sources"]])
        normalized = normalize_snapshot(snapshot)
        self.assertEqual(snapshot_status(normalized, "live"), ("timely", None))

    def test_missing_anchor_emits_unavailable_not_stale_substitute(self):
        sessions = [date(2026, 1, 2) + timedelta(days=index) for index in range(7)]
        with patch.object(builder, "is_taifex_trading_day", return_value=True), \
             patch.object(builder, "get_taifex_trading_days_in_range", return_value=(sessions, [])):
            snapshot = builder.build_snapshot(sessions[-1], [], generated_at=datetime(2026, 1, 8, 8, tzinfo=TAIPEI), mode="live")
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("missing_tx_front_regular", snapshot["unavailable_reason"])
        normalized = normalize_snapshot(snapshot)
        self.assertEqual(snapshot_status(normalized, "live")[0], "unavailable")

    def test_late_source_snapshot_is_marked_late_by_runner(self):
        sessions = [date(2026, 1, 2) + timedelta(days=index) for index in range(7)]
        day = sessions[-1]
        rows = [daily_row(item) for item in sessions[:-1]]
        generated = datetime(2026, 1, 8, 8, 31, tzinfo=TAIPEI)
        with patch.object(builder, "is_taifex_trading_day", return_value=True), \
             patch.object(builder, "get_taifex_trading_days_in_range", return_value=(sessions, [])):
            snapshot = builder.build_snapshot(day, rows, generated_at=generated, mode="live")
        self.assertEqual(snapshot_status(normalize_snapshot(snapshot), "live")[0], "late")

    def test_missing_acquisition_evidence_is_unavailable_not_backdated(self):
        sessions = [date(2026, 1, 2) + timedelta(days=index) for index in range(7)]
        rows = [daily_row(item) for item in sessions[:-1]]
        del rows[0]["fetched_at"]
        with patch.object(builder, "is_taifex_trading_day", return_value=True), \
             patch.object(builder, "get_taifex_trading_days_in_range", return_value=(sessions, [])):
            snapshot = builder.build_snapshot(sessions[-1], rows, generated_at=datetime(2026, 1, 8, 8, tzinfo=TAIPEI), mode="replay")
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("source_timestamp", snapshot["unavailable_reason"])

    def test_write_is_create_only_and_never_exposes_a_partial_final_file(self):
        snapshot = builder.unavailable_snapshot(date(2026, 1, 8), datetime(2026, 1, 8, 8, tzinfo=TAIPEI), "live", "fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = builder.write_snapshot(snapshot, root)
            self.assertTrue(path.exists())
            with self.assertRaises(FileExistsError):
                builder.write_snapshot(snapshot, root)

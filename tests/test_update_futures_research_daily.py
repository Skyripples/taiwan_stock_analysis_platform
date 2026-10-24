from __future__ import annotations

import argparse
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import update_futures_research_daily as module


def options(*, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(lookback_days=7, end=date(2026, 10, 20), delay=0, dry_run=dry_run)


class FuturesResearchDailyOrchestrationTests(unittest.TestCase):
    def test_stage_order_and_normal_skip_continue(self):
        calls: list[str] = []

        def stage(name: str, status: str):
            def handler(_options):
                calls.append(name)
                return module.StageResult(status, {"stage": name})
            return handler

        result = module.run(options(), stages=[
            ("tx", stage("tx", "OK")), ("institutional", stage("institutional", "SKIP")),
            ("pit", stage("pit", "OK")), ("audit", stage("audit", "OK")),
        ])
        self.assertEqual(calls, ["tx", "institutional", "pit", "audit"])
        self.assertEqual([item["status"] for item in result["stages"]], ["OK", "SKIP", "OK", "OK"])

    def test_trailing_tx_skip_does_not_block_later_pipeline_stages(self):
        tx_result = {
            "failures": [], "staging_rows": 0,
            "skipped_not_yet_published": [{"date": "2026-10-23", "reason": "not_yet_published"}],
        }
        calls: list[str] = []
        def later(_options):
            calls.append("later")
            return module.StageResult("OK", {})
        with patch.object(module.tx_updater, "run_staging_only", return_value=tx_result):
            result = module.run(options(), stages=[("tx", module.stage_tx), ("later", later)])
        self.assertEqual([row["status"] for row in result["stages"]], ["SKIP", "OK"])
        self.assertEqual(calls, ["later"])

    def test_research_build_end_is_capped_at_latest_actual_tx_staging_date(self):
        future_options = argparse.Namespace(lookback_days=7, end=date(2026, 10, 24), delay=0, dry_run=False)
        with patch.object(module.tx_updater, "latest_completed_trading_day", return_value=date(2026, 10, 23)), \
             patch.object(module.tx_updater, "tx_staging_dates", return_value={date(2026, 10, 20), date(2026, 10, 22)}):
            self.assertEqual(module.research_end(future_options), date(2026, 10, 22))

    def test_required_failure_stops_following_dependencies(self):
        calls: list[str] = []

        def fail(_options):
            calls.append("global")
            raise RuntimeError("fixture failure")

        def never(_options):
            calls.append("pit")
            return module.StageResult("OK", {})

        result = module.run(options(), stages=[("global", fail), ("pit", never)])
        self.assertEqual(calls, ["global"])
        self.assertEqual(result["stages"][-1]["status"], "FAIL")

    def test_dry_run_does_not_invoke_any_stage_or_historical_backfill(self):
        called = []

        def source(_options):
            called.append(True)
            return module.StageResult("OK", {})

        result = module.run(options(dry_run=True), stages=[("source", source)])
        self.assertEqual(called, [])
        self.assertEqual(result["stages"], [{"name": "source", "status": "SKIP", "reason": "dry_run"}])

    def test_same_day_rerun_keeps_the_canonical_key_idempotent(self):
        canonical_dates: set[date] = set()

        def tx_sync(_options):
            before = len(canonical_dates)
            canonical_dates.add(date(2026, 10, 20))
            return module.StageResult("OK", {"new_dates": len(canonical_dates) - before})

        first = module.run(options(), stages=[("tx", tx_sync)])
        second = module.run(options(), stages=[("tx", tx_sync)])
        self.assertEqual(first["stages"][0]["new_dates"], 1)
        self.assertEqual(second["stages"][0]["new_dates"], 0)
        self.assertEqual(canonical_dates, {date(2026, 10, 20)})

    def test_global_stage_is_bounded_incremental_not_a_historical_backfill(self):
        captured = {}

        def execute(command, **_kwargs):
            captured["command"] = command
            return type("Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

        result = module.stage_global(options(), execute=execute)
        command = captured["command"]
        self.assertEqual(result.status, "OK")
        self.assertIn("--resume", command)
        self.assertIn("--storage", command)
        self.assertNotIn("--all", command)
        self.assertLessEqual(date.fromisoformat(command[command.index("--end") + 1]) - date.fromisoformat(command[command.index("--start") + 1]), module.timedelta(days=14))
        self.assertEqual(sum(item == "--symbol" for item in command), len(module.GLOBAL_SYMBOLS))

    def test_audit_detects_duplicate_and_pit_violation(self):
        day = date(2026, 10, 20)
        feature = {"prediction_date": day, "prediction_at": datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc),
                   "max_feature_available_at": datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)}
        research = [{"prediction_date": day, "target_trading_date": None, "next_trading_day_regular_return": None}]
        with self.assertRaisesRegex(ValueError, "PIT available_at violations"):
            module.audit_rows([feature], research)
        with self.assertRaisesRegex(ValueError, "Duplicate research"):
            module.audit_rows([], research + research)

    def test_audit_accepts_null_latest_target(self):
        day = date(2026, 10, 20)
        at = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
        audit = module.audit_rows(
            [{"prediction_date": day, "prediction_at": at, "max_feature_available_at": at}],
            [{"prediction_date": day, "target_trading_date": None, "next_trading_day_regular_return": None}],
        )
        self.assertEqual(audit["research_duplicates"], 0)
        self.assertTrue(audit["latest_target_is_null"])


if __name__ == "__main__":
    unittest.main()

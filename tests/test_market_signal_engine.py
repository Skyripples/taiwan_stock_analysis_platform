from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analysis.market_signal_engine import MarketSignalEngine


def payload(record):
    return {"data": {"records": [record]}}


class MarketSignalEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "config" / "factor_config.json").read_text(encoding="utf-8"))

    def sources(self):
        return {
            "factor_config": copy.deepcopy(self.config),
            "taiwan_market_overview": payload({"trade_date": "2026-09-01", "taiex": {"change_percent": 0.1}, "tpex": {"change_percent": 0.1}, "advancing": 500, "declining": 500}),
            "institutional_investors": payload({"trade_date": "2026-09-01", "foreign_and_mainland_investors": {"net": 0}}),
            "foreign_futures_position": payload({"trade_date": "2026-09-01", "net_position": {"open_interest": 0}}),
            "night_futures": payload({"trade_date": "2026-09-02", "change_percent": 0}),
            "tsm_adr": payload({"trade_date": "2026-09-01", "change_percent": 0}),
            "sox_index": payload({"trade_date": "2026-09-01", "change_percent": 0}),
            "nasdaq_index": payload({"trade_date": "2026-09-01", "change_percent": 0}),
            "sp500_index": payload({"trade_date": "2026-09-01", "change_percent": 0}),
            "vix_index": payload({"trade_date": "2026-09-01", "change_percent": 0}),
            "kospi_index": payload({"trade_date": "2026-09-01", "change_percent": 0}),
        }

    def test_neutral_band_is_config_driven(self):
        sources = self.sources()
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        self.assertEqual(result["rules"]["taiex_change"]["score"], 0)
        sources["factor_config"]["rules"]["taiex_change"]["thresholds"]["bullish_min"] = 0.05
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        self.assertEqual(result["rules"]["taiex_change"]["score"], 1)

    def test_missing_and_stale_rules_are_excluded(self):
        sources = self.sources()
        sources.pop("sox_index")
        sources["kospi_index"] = payload({"trade_date": "2026-08-01", "change_percent": 2})
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        self.assertFalse(result["rules"]["sox_index"]["available"])
        self.assertTrue(result["rules"]["kospi_index"]["stale"])
        self.assertEqual(result["coverage"]["available_rules"], result["coverage"]["enabled_rules"] - 2)
        self.assertEqual(result["coverage"]["percentage"], 85)

    def test_friday_target_date_night_futures_is_fresh(self):
        sources = self.sources()
        sources["taiwan_market_overview"]["data"]["records"][0]["trade_date"] = "2026-09-04"
        sources["night_futures"]["data"]["records"][0]["trade_date"] = "2026-09-07"
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        rule = result["rules"]["night_futures"]
        self.assertTrue(rule["available"])
        self.assertFalse(rule["stale"])

    def test_feature_date_night_futures_is_fresh(self):
        sources = self.sources()
        sources["taiwan_market_overview"]["data"]["records"][0]["trade_date"] = "2026-09-04"
        sources["night_futures"]["data"]["records"][0]["trade_date"] = "2026-09-04"
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        self.assertTrue(result["rules"]["night_futures"]["available"])

    def test_night_futures_after_target_date_is_stale(self):
        sources = self.sources()
        sources["taiwan_market_overview"]["data"]["records"][0]["trade_date"] = "2026-09-04"
        sources["night_futures"]["data"]["records"][0]["trade_date"] = "2026-09-08"
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        rule = result["rules"]["night_futures"]
        self.assertFalse(rule["available"])
        self.assertTrue(rule["stale"])

    def test_non_night_rule_future_tolerance_is_not_relaxed(self):
        sources = self.sources()
        sources["taiwan_market_overview"]["data"]["records"][0]["trade_date"] = "2026-09-04"
        sources["institutional_investors"]["data"]["records"][0]["trade_date"] = "2026-09-07"
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        rule = result["rules"]["foreign_cash_flow"]
        self.assertFalse(rule["available"])
        self.assertTrue(rule["stale"])

    def test_module_aggregation_prevents_duplicate_votes(self):
        sources = self.sources()
        for key in ("tsm_adr", "sox_index", "nasdaq_index", "sp500_index"):
            sources[key]["data"]["records"][0]["change_percent"] = 3
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        module = result["modules"]["us_tech"]
        self.assertEqual(module["score"], 100)
        self.assertEqual(module["max_score"], 100)
        self.assertEqual(result["market_score"]["max_score"], 100)

    def test_total_score_normalization(self):
        sources = self.sources()
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        self.assertEqual(result["market_score"]["percentage"], 50)
        self.assertEqual(result["market_score"]["status"], "Neutral")

    def test_module_weight_is_config_driven(self):
        sources = self.sources()
        sources["taiwan_market_overview"]["data"]["records"][0]["taiex"]["change_percent"] = 2
        sources["factor_config"]["modules"]["taiwan_market"]["weight"] = 30
        sources["factor_config"]["modules"]["risk_global"]["weight"] = 10
        result = MarketSignalEngine(ROOT / "data" / "market").analyze(sources)
        self.assertEqual(result["market_score"]["max_score"], 100)
        self.assertGreater(result["market_score"]["percentage"], 50)

    def test_weights_must_total_one_hundred(self):
        sources = self.sources()
        sources["factor_config"]["modules"]["taiwan_market"]["weight"] = 19
        with self.assertRaisesRegex(ValueError, "Module weights must total 100"):
            MarketSignalEngine(ROOT / "data" / "market").analyze(sources)

    def test_legacy_signals_keep_existing_widget_contract(self):
        sources = self.sources()
        sources["night_futures"]["data"]["records"][0]["change"] = -120
        sources["tsm_adr"]["data"]["records"][0]["change"] = 1.25
        engine = MarketSignalEngine(ROOT / "data" / "market")
        exported = engine.export(engine.analyze(sources), updated_at="2026-09-01T00:00:00Z")
        self.assertEqual(exported["signals"]["night_futures"]["value"], -120)
        self.assertEqual(exported["signals"]["night_futures"]["status"], "bearish")
        self.assertEqual(exported["signals"]["tsm_adr"]["value"], 1.25)


if __name__ == "__main__":
    unittest.main()
